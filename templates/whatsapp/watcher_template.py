#!/usr/bin/env python3
"""
watcher.py — Monitora WhatsApp via Evolution API e ativa agente de vendas

Poll a cada 3s na Evolution API local buscando mensagens novas.
Filtra grupos, mensagens próprias e formato LID.
Chama agent.handle_message() e envia resposta via Evolution API.

Execução:
  python3 watcher.py                    ← roda indefinidamente
  launchctl load ~/Library/LaunchAgents/com.meuagente.watcher.plist  ← auto-start macOS

Anti-ban: este número já foi banido pelo WhatsApp uma vez. Todo envio passa por
atraso humanizado + indicador "digitando..." + espaçamento anti-rajada, e há um
aquecimento gradual de volume diário nos primeiros dias após reconectar (ver
constantes MIN_SECONDS_BETWEEN_SENDS / TYPING_* / WARMUP_DAILY_LIMITS abaixo).
Isso reduz o risco, mas não elimina — o WhatsApp pode banir por outros motivos
(denúncias de usuários, IP de datacenter, etc). Para eliminar o risco de vez, a
alternativa é migrar para a Meta Cloud API oficial (webhook_server.py).
"""

import json
import random
import re
import threading
import time
import logging
import signal
import sys
import traceback
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent))
import client_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(client_config.LOG_FILE, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
from agent import handle_message, is_trigger, is_handoff_request

# ── Configuração (preenchida pelo setup) ──────────────────────────────────────
EVOLUTION_URL = client_config.get("evolution_url", "http://localhost:8080")
EVOLUTION_API_KEY = client_config.require("evolution_api_key")
INSTANCE_NAME = client_config.require("instance_name")
OWNER_PHONE = client_config.get("owner_phone", "")  # Recebe alertas de atendimento humano
POLL_INTERVAL = 3  # segundos

# Watchdog de conexão: a Evolution API pode entrar em "estado zumbi" — o
# connectionState continua dizendo "open" (isso vem do banco), mas o socket do
# WhatsApp morreu e nenhuma mensagem nova chega. Sem isso o agente fica mudo por
# horas sem nenhum erro no log (aconteceu em 2026-07-28: 8h fora do ar).
HEALTH_CHECK_EVERY = 100      # iterações (~5 min com POLL_INTERVAL=3)
HEALTH_FAIL_THRESHOLD = 2     # falhas seguidas antes de reiniciar a instância
HEALTH_RESTART_COOLDOWN = 600 # segundos mínimos entre dois restarts automáticos

# Alerta de áudio ElevenLabs quebrado: sem isso, uma falha sistêmica (chave
# inválida/revogada, créditos esgotados) passa despercebida por dias, porque
# o agente sempre cai pro fallback de texto sem erro visível pro cliente final
# — foi exatamente o que aconteceu em 2026-08-06/10 (seção 35 do SKILL.md),
# 4 dias de áudio quebrado só percebidos porque o cliente reparou na mão.
ELEVEN_FAIL_ALERT_THRESHOLD = 3     # falhas seguidas antes de avisar o dono
ELEVEN_ALERT_COOLDOWN = 3600        # segundos mínimos entre dois alertas (1h)

STATE_FILE = client_config.STATE_FILE
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# Anti-ban: esse número já foi banido pelo WhatsApp uma vez. Respostas instantâneas
# e em rajada são o principal sinal que o WhatsApp usa pra distinguir bot de gente —
# então toda mensagem passa por um atraso "digitando..." proporcional ao tamanho do
# texto, e há um espaçamento mínimo entre quaisquer dois envios (mesmo pra leads
# diferentes). Além disso, o número aquece gradualmente: o limite diário de
# mensagens cresce nos primeiros dias após reconectar (sem nunca bloquear leads —
# só avisa o dono se o volume ficar incomum pro estágio de aquecimento atual).
MIN_SECONDS_BETWEEN_SENDS = 2.5   # espaçamento mínimo entre dois envios quaisquer
TYPING_CHARS_PER_SECOND = 14      # velocidade de "digitação" simulada
TYPING_MIN_SECONDS = 1.5
TYPING_MAX_SECONDS = 9.0
WARMUP_DAILY_LIMITS = {0: 20, 3: 50, 7: 100, 14: 250}  # dias-desde-reconexão -> limite/dia (soft)

# Link dentro de uma resposta em áudio precisa chegar TAMBÉM por escrito — o
# cliente não consegue clicar num link falado. Ver seção 23 do SKILL.md.
# Cobre "www.algo" mesmo sem "http(s)://" na frente (2026-08-10, pedido do
# cliente: a IA às vezes escreve/fala o link como "www.asaas.com/..." sem o
# protocolo, e esse formato passava batido tanto aqui quanto na limpeza do
# texto pro TTS em preprocess_text_for_tts — as duas usam essa mesma regex
# agora, pra nunca mais divergir uma da outra).
URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


# ── Evolution API ─────────────────────────────────────────────────────────────

def evolution_request(endpoint: str, method: str = "GET", data: dict = None, timeout: int = 10) -> dict:
    url = f"{EVOLUTION_URL}{endpoint}"
    headers = {
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json"
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data else None,
        headers=headers,
        method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Evolution API erro: HTTP {e.code} — {body[:300]}")
        return {}
    except Exception as e:
        logger.error(f"Evolution API erro: {e}")
        return {}


def fetch_messages(count: int = 20) -> list:
    """Busca últimas mensagens da instância."""
    result = evolution_request(
        f"/chat/findMessages/{INSTANCE_NAME}",
        method="POST",
        data={"count": count}
    )
    # Evolution API v2: {"messages": {"records": [...], "total": N}}
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "messages" in result:
        messages_data = result["messages"]
        if isinstance(messages_data, dict):
            return messages_data.get("records", [])
        if isinstance(messages_data, list):
            return messages_data
    return []


_send_lock = threading.Lock()
_last_send_at = 0.0


def _respect_send_spacing():
    """Garante um espaçamento mínimo entre dois envios quaisquer, mesmo que sejam
    pra leads diferentes — evita rajadas de mensagens que soam como bot."""
    global _last_send_at
    with _send_lock:
        wait = MIN_SECONDS_BETWEEN_SENDS - (time.time() - _last_send_at)
        if wait > 0:
            time.sleep(wait)
        _last_send_at = time.time()


def send_presence(phone: str, presence: str = "composing", duration_ms: int = 1200):
    """Mostra 'digitando...' (ou 'gravando áudio...') pro lead antes de responder.
    Best-effort: nunca deve travar o envio da mensagem se o endpoint falhar."""
    try:
        evolution_request(
            f"/chat/sendPresence/{INSTANCE_NAME}",
            method="POST",
            data={"number": phone, "presence": presence, "delay": duration_ms}
        )
    except Exception:
        pass


def humanized_typing_delay(message: str) -> float:
    """Atraso proporcional ao tamanho da mensagem (+ variação aleatória) pra imitar
    o tempo que uma pessoa levaria digitando essa resposta."""
    base = len(message) / TYPING_CHARS_PER_SECOND
    jittered = base * random.uniform(0.85, 1.3)
    return max(TYPING_MIN_SECONDS, min(TYPING_MAX_SECONDS, jittered))


ANTIBAN_STATE_FILE = STATE_FILE.parent / "antiban_state.json"
_antiban_lock = threading.Lock()


def _load_antiban_state() -> dict:
    if ANTIBAN_STATE_FILE.exists():
        try:
            return json.loads(ANTIBAN_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_antiban_state(state: dict):
    ANTIBAN_STATE_FILE.write_text(json.dumps(state, indent=2))


def _track_daily_send(phone: str):
    """Atualiza o contador diário de envios/contatos novos e avisa o dono (uma vez
    por dia) se o volume passar do limite de aquecimento esperado pro estágio atual
    do número. Nunca bloqueia o envio — só dá visibilidade pro dono agir.

    Usa um arquivo de estado próprio (separado do STATE_FILE de seen_ids) porque
    esta função roda no meio do processamento de uma mensagem, enquanto o loop
    principal ainda segura sua própria cópia em memória do state de seen_ids —
    compartilhar o mesmo arquivo faria o save_state() do loop principal sobrescrever
    e perder essas estatísticas."""
    should_alert = False
    messages_sent = 0
    limit = 20
    days_since = 0

    with _antiban_lock:
        state = _load_antiban_state()
        today = datetime.now().strftime("%Y-%m-%d")
        daily = state.setdefault("daily_stats", {})
        if daily.get("date") != today:
            daily["date"] = today
            daily["messages_sent"] = 0
            daily["contacts"] = []
            daily["warned"] = False

        daily["messages_sent"] = daily.get("messages_sent", 0) + 1
        if phone not in daily.get("contacts", []):
            daily.setdefault("contacts", []).append(phone)

        first_connected = state.get("first_connected_date")
        if not first_connected:
            state["first_connected_date"] = today
            first_connected = today

        try:
            days_since = (datetime.strptime(today, "%Y-%m-%d") -
                          datetime.strptime(first_connected, "%Y-%m-%d")).days
        except Exception:
            days_since = 999

        for threshold in sorted(WARMUP_DAILY_LIMITS):
            if days_since >= threshold:
                limit = WARMUP_DAILY_LIMITS[threshold]

        messages_sent = daily["messages_sent"]
        if messages_sent > limit and not daily.get("warned"):
            daily["warned"] = True
            should_alert = True

        _save_antiban_state(state)

    # Enviado fora do lock: send_whatsapp() chama esta mesma função pra registrar
    # o próprio alerta, e o lock (não reentrante) travaria nessa segunda chamada.
    if should_alert:
        logger.warning(
            f"⚠️  Volume diário ({messages_sent}) passou do limite de "
            f"aquecimento esperado ({limit}) pro dia {days_since} desde a reconexão."
        )
        if OWNER_PHONE:
            send_whatsapp(
                OWNER_PHONE,
                f"⚠️ O agente já enviou {messages_sent} mensagens hoje, acima do "
                f"ritmo recomendado de aquecimento ({limit}/dia) pro número reconectado há "
                f"{days_since} dia(s). Não bloqueei nenhum lead, mas se isso não for tráfego "
                "real vale investigar — volume alto demais é o que costuma levar a bloqueios."
            )


def send_whatsapp(phone: str, message: str) -> bool:
    """Envia mensagem via Evolution API com atraso humanizado, indicador de
    'digitando...' e espaçamento anti-rajada (o número já foi banido uma vez por
    comportamento de bot — ver constantes de anti-ban no topo do arquivo)."""
    delay = humanized_typing_delay(message)
    send_presence(phone, "composing", int(delay * 1000))
    time.sleep(delay)
    _respect_send_spacing()

    result = evolution_request(
        f"/message/sendText/{INSTANCE_NAME}",
        method="POST",
        data={"number": phone, "text": message}
    )
    success = bool(result.get("key") or result.get("id"))
    if success:
        logger.info(f"📤 Enviado para {phone}")
        _track_daily_send(phone)
    else:
        logger.error(f"❌ Falha ao enviar para {phone}: {result}")
    return success


# ── Funções de Escrita Numérica por Extenso (Tratamento para ElevenLabs) ──────

def number_to_words(n: int) -> str:
    """Converte um número inteiro de até 999.999 em palavras em português."""
    if n == 0:
        return "zero"
        
    units = ["", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove"]
    teens = ["dez", "onze", "doze", "treze", "quatorze", "quinze", "dezesseis", "dezessete", "dezoito", "dezenove"]
    tens = ["", "dez", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta", "oitenta", "noventa"]
    hundreds = ["", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos", "seiscentos", "setecentos", "oitocentos", "novecentos"]
    
    if n == 100:
        return "cem"
        
    words = []
    
    # Milhares
    thousands = n // 1000
    if thousands > 0:
        if thousands == 1:
            words.append("mil")
        else:
            words.append(number_to_words(thousands) + " mil")
        n = n % 1000
        if n > 0:
            if n < 100 or n % 100 == 0:
                words.append("e")
                
    # Centenas
    if n > 0:
        h = n // 100
        if h > 0:
            words.append(hundreds[h])
            n = n % 100
            if n > 0:
                words.append("e")
                
    # Dezenas e Unidades
    if n > 0:
        if 10 <= n < 20:
            words.append(teens[n - 10])
        else:
            t = n // 10
            u = n % 10
            if t > 0:
                words.append(tens[t])
                if u > 0:
                    words.append("e")
            if u > 0:
                words.append(units[u])
                
    return " ".join(words)


def price_to_words(price_str: str) -> str:
    """Converte uma string contendo valor monetário (R$ ou BRL) em reais escritos por extenso."""
    price_str = price_str.replace("R$", "").replace("BRL", "").replace("Brl", "").replace("brl", "").replace(" ", "")
    
    if "," in price_str:
        parts = price_str.split(",")
        reais_str = "".join(filter(str.isdigit, parts[0]))
        cents_str = "".join(filter(str.isdigit, parts[1]))[:2]
    elif "." in price_str and len(price_str.split(".")[-1]) == 2:
        parts = price_str.split(".")
        reais_str = "".join(filter(str.isdigit, parts[0]))
        cents_str = "".join(filter(str.isdigit, parts[1]))
    else:
        reais_str = "".join(filter(str.isdigit, price_str))
        cents_str = "0"
        
    try:
        reais = int(reais_str) if reais_str else 0
        cents = int(cents_str) if cents_str else 0
        if len(cents_str) == 1 and cents_str != "0":
            cents = cents * 10
    except Exception:
        return price_str
        
    reais_word = "real" if reais == 1 else "reais"
    cents_word = "centavo" if cents == 1 else "centavos"
    
    result = []
    if reais > 0:
        result.append(f"{number_to_words(reais)} {reais_word}")
    if cents > 0:
        if reais > 0:
            result.append("e")
        result.append(f"{number_to_words(cents)} {cents_word}")
        
    if not result:
        return "zero reais"
        
    return " ".join(result)


_UF_PARA_ESTADO = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia",
    "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás",
    "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais",
    "PA": "Pará", "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí",
    "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul",
    "RO": "Rondônia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo",
    "SE": "Sergipe", "TO": "Tocantins",
}


def preprocess_text_for_tts(text: str) -> str:
    """Prepara o texto para ser falado de forma perfeita, pausada e sem erros de números ou símbolos."""
    import re

    # Nunca falar um link em voz alta (fica soletrando "h t t p dois pontos..."
    # ou "www ponto asaas ponto com") — o link já vai por escrito em mensagem
    # separada (ver send_whatsapp_audio no loop principal). Usa o mesmo
    # URL_PATTERN do módulo (cobre "www.algo" mesmo sem "http(s)://" na
    # frente) pra nunca divergir da regex que decide o que reenviar por
    # texto — ver seção 37 do SKILL.md, 2026-08-10.
    text = URL_PATTERN.sub("", text)

    # Remover formatação Markdown — a IA escreve **negrito**, listas com "- " e
    # às vezes cabeçalhos "#", pensado pro texto do WhatsApp. Os símbolos soltos
    # confundem o TTS (2026-08-03: causa real de erros de pronúncia — ver seção
    # 25 do SKILL.md, ex: "Cidade: Florianópolis/SC" saiu como "Sassicado").
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)          # **negrito**
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)  # *itálico*
    text = re.sub(r"^[ \t]*[-•]\s+", "", text, flags=re.MULTILINE)  # marcador de lista
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)      # cabeçalho markdown

    # Emoji — a IA às vezes decora a mensagem de texto com emoji (💡, ✅, 📏
    # etc.), e sem remover isso o TTS tenta "ler" o glifo, o que soa mal ou
    # gera silêncio/ruído. Auditoria proativa (2026-08-05, pedido do cliente
    # depois do bug do "mm") -- nenhum caso reproduzido ainda com emoji real,
    # mas é o mesmo princípio do Markdown acima: símbolo decorativo bom pro
    # texto do WhatsApp, ruim pra voz. Cobre os blocos Unicode de emoji mais
    # comuns + variation selector/ZWJ (usados em emoji combinados).
    _EMOJI_PATTERN = (
        "[\U0001F300-\U0001FAFF"  # emoji principais (pictogramas, símbolos, etc.)
        "☀-➿"           # símbolos diversos e dingbats (inclui ⚠ aviso)
        "←-⇿"           # setas (inclui →)
        "⬀-⯿"           # mais símbolos e setas
        "️"                  # variation selector-16 (estiliza emoji)
        "‍"                  # zero-width joiner (junta emoji combinados)
        "]"
    )
    text = re.sub(_EMOJI_PATTERN, "", text)

    # Sigla de estado (UF) sozinha ou colada com barra/hífen costuma sair
    # errado ou embolada com a palavra anterior — troca pelo nome por extenso
    # (ex: "Florianópolis/SC" -> "Florianópolis, Santa Catarina").
    def _uf_replacer(m):
        estado = _UF_PARA_ESTADO.get(m.group(1).upper())
        return f", {estado}" if estado else m.group(0)

    text = re.sub(r"[/\-,]\s*([A-Z]{2})\b(?!\w)", _uf_replacer, text)

    # CEP (8 dígitos, com ou sem hífen) — ler dígito por dígito é o jeito mais
    # claro e confiável pro TTS. Sem isso o modelo tenta "compor" um número
    # grande de 5 dígitos e embola (cliente relatou erro em "88085-250",
    # 2026-08-03 — seção 26 do SKILL.md).
    _DIGITOS_PT = ["zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove"]

    def _falar_digitos(m):
        digitos = re.sub(r"\D", "", m.group(0))
        return ", ".join(_DIGITOS_PT[int(d)] for d in digitos)

    text = re.sub(r"\b\d{5}-\d{3}\b", _falar_digitos, text)
    text = re.sub(r"(?<=CEP )\d{8}\b", _falar_digitos, text, flags=re.IGNORECASE)

    # "nº" / "n°" (abreviação de "número", com o símbolo de ordinal) confundia
    # o TTS — expande por extenso (ex: "nº 193" -> "número 193").
    text = re.sub(r"\bn[º°]\s*", "número ", text, flags=re.IGNORECASE)

    # "6x" (parcelas) -> "seis vezes" — sem isso o modelo tenta "adivinhar" a
    # pronúncia de "6x" sozinho e engasga bem na palavra "vezes" (reportado
    # pelo cliente, 2026-08-03 — seção 29 do SKILL.md). Só converte quando NÃO
    # vier seguido de outro dígito, pra não mexer em medida tipo "10x20".
    def _vezes_replacer(m):
        return f"{number_to_words(int(m.group(1)))} vezes"

    text = re.sub(r"\b(\d+)x\b(?!\d)", _vezes_replacer, text, flags=re.IGNORECASE)

    # "16mm"/"25mm"/"50mm" (largura de lâmina da Persiana Horizontal Alumínio,
    # usado no prompt de configuração) -- mesma classe de bug de "nº"/"6x"/"m²":
    # número colado numa abreviação que o TTS não sabe ler. Confirmado ao vivo
    # (cliente, 2026-08-03): "Horizontal Alumínio 20mm motorizada" saiu
    # transcrito como "20 com make merit motorizada" -- ruído sem sentido.
    def _mm_replacer(m):
        n = int(m.group(1))
        unidade = "milímetro" if n == 1 else "milímetros"
        return f"{number_to_words(n)} {unidade}"

    text = re.sub(r"\b(\d+)\s*mm\b", _mm_replacer, text, flags=re.IGNORECASE)

    # "cartão" (no contexto de parcelamento, ex: "sem juros no cartão") vira
    # "cartal"/"cartel" nessa voz -- bug real, confirmado ouvindo o áudio
    # gerado (cliente, 2026-08-03). Testei à exaustão pra achar outro jeito
    # (estabilidade 0.55/0.7/0.85, velocidade 1.05/1.0/0.9, reordenar a frase,
    # trocar "vezes" por "parcelas", separar em duas frases) e nada resolveu
    # -- só a troca de palavra funcionou (9/9 testes limpos). "no crédito" é
    # sinônimo natural e comum em PDV/checkout no Brasil pra "parcelado no
    # cartão de crédito". Único uso de "cartão" no sistema é forma de
    # pagamento (nunca aparece em outro contexto no prompt) -- troca segura.
    text = re.sub(r"\bcartão de crédito\b", "crédito", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcartão\b", "crédito", text, flags=re.IGNORECASE)

    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    # Substituir símbolos indesejados comuns no chat de medidas
    text = text.replace("+", " e ").replace(" x ", " por ").replace(" X ", " por ")
    
    # 1. Substituir preços por extenso em português (formatos R$ 147,39 ou 147,39 BRL)
    pattern_price_rs = r"R\$\s*(\d+(?:[\.,]\d+)*)"
    text = re.sub(pattern_price_rs, lambda m: price_to_words(m.group(0)), text)
    
    pattern_price_brl = r"(\d+(?:[\.,]\d+)*)\s*(?:BRL|Brl|brl)\b"
    text = re.sub(pattern_price_brl, lambda m: price_to_words(m.group(0)), text)
    
    # 2a. Converter medidas com unidade (ex: "1,50 metros" → "um metro e cinquenta centímetros")
    def measure_replacer(match):
        int_part = int(match.group(1))
        dec_part = int(match.group(2))
        base = f"{number_to_words(int_part)} metro"
        if int_part != 1:
            base += "s"
        if dec_part > 0:
            base += f" e {number_to_words(dec_part)} centímetros"
        return base

    text = re.sub(
        r"\b(\d+)[.,](\d{2})\s*metros?\b",
        measure_replacer,
        text,
        flags=re.IGNORECASE
    )

    # 2b. Converter decimais soltos restantes (ex: preços sem R$ como "294,39")
    def decimal_replacer(match):
        int_part = int(match.group(1))
        dec_part = int(match.group(2))
        return f"{number_to_words(int_part)} e {number_to_words(dec_part)}"

    text = re.sub(r"\b(\d+)[.,](\d{2})\b", decimal_replacer, text)

    # "m²" (metro quadrado, usado no preço de Tela Mosquiteira/Toldo) confundia
    # o TTS igual "nº" -- mesma classe de bug (símbolo colado numa letra que o
    # modelo não sabe ler). Precisa rodar DEPOIS dos blocos 2a/2b acima: se
    # "m²" virasse "metros quadrados" antes, o measure_replacer ia confundir
    # com medida linear (ex: "1,80 m²" viraria "um metro e oitenta
    # centímetros", errado -- área não é a mesma coisa que comprimento).
    # "o m²" (preço por unidade, ex: "R$ 180,00 o m²") fala-se no singular --
    # "o metro quadrado" -- igual quando alguém fala "o quilo" ou "o litro".
    # Já uma quantidade (ex: "1,80 m²") fica no plural, tratado pela regra
    # seguinte.
    text = re.sub(r"\bo\s+m[²2]\b", "o metro quadrado", text, flags=re.IGNORECASE)
    text = re.sub(r"\bm[²2]\b", "metros quadrados", text, flags=re.IGNORECASE)

    # (Removido em 2026-08-02: inserir "..." depois de toda vírgula/ponto criava
    # uma pausa forçada atrás de cada frase, deixando a fala truncada e robótica
    # — cliente reclamou que soava "fala, pausa, fala, pausa". A pontuação normal
    # já é suficiente pro modelo v2 pausar de forma natural.)

    return text


def add_tone_tags_for_v3(text: str) -> str:
    """Intercala tags de tom ([friendly]/[warmly]) do modelo eleven_v3 a cada frase.
    Sem isso o v3 soa "lendo um texto" — cliente aprovou essa versão em 2026-08-02
    comparando com a versão sem tags. IMPORTANTE (2026-08-03): NÃO quebrar por
    linha além de .!? — tentei isso na seção 25 do SKILL.md pra dar pausa em
    listas/endereço, mas piorou tudo (mais devagar, mais engasgo, entonação
    pior) porque cada tag reseta o "estado emocional" do modelo v3 — quebrar
    o texto em fragmentos demais vira o oposto do problema que a gente
    tinha resolvido antes (fala truncada). Revertido — só a pontuação normal
    (.!?) deve gerar uma nova tag."""
    import re
    tags = ["[friendly]", "[warmly]"]
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    return " ".join(f"{tags[i % len(tags)]} {part}" for i, part in enumerate(parts))


_eleven_consecutive_failures = 0
_eleven_last_alert_at = 0


def _registrar_falha_audio(motivo: str):
    """Conta falhas consecutivas de áudio ElevenLabs e avisa o dono (no máximo
    1x por hora) quando cruza ELEVEN_FAIL_ALERT_THRESHOLD -- ver seção 35/36
    do SKILL.md. Sem isso, um problema de chave/crédito só é percebido quando
    o cliente reclama, potencialmente dias depois."""
    global _eleven_consecutive_failures, _eleven_last_alert_at
    _eleven_consecutive_failures += 1
    if _eleven_consecutive_failures < ELEVEN_FAIL_ALERT_THRESHOLD:
        return
    agora = int(time.time())
    if agora - _eleven_last_alert_at < ELEVEN_ALERT_COOLDOWN:
        return
    _eleven_last_alert_at = agora
    if OWNER_PHONE:
        send_whatsapp(
            OWNER_PHONE,
            f"🚨 O agente não está conseguindo gerar áudio há {_eleven_consecutive_failures} tentativas "
            f"seguidas (ElevenLabs). Motivo mais recente: {motivo}\n\n"
            "Provável causa: chave de API inválida/expirada ou créditos esgotados. "
            "Verifique em elevenlabs.io → Settings → API Keys. As respostas continuam chegando por "
            "texto normalmente enquanto isso não for resolvido."
        )
        logger.error(f"🚨 Alerta de áudio quebrado enviado ao dono ({_eleven_consecutive_failures} falhas seguidas).")


def _registrar_sucesso_audio():
    """Reseta o contador de falhas e avisa o dono se o áudio tinha acabado de
    voltar a funcionar depois de ter cruzado o limite de alerta."""
    global _eleven_consecutive_failures
    if _eleven_consecutive_failures >= ELEVEN_FAIL_ALERT_THRESHOLD and OWNER_PHONE:
        send_whatsapp(OWNER_PHONE, "✅ O áudio do agente voltou a funcionar normalmente.")
    _eleven_consecutive_failures = 0


def send_whatsapp_audio_elevenlabs(phone: str, message: str) -> bool:
    """Converte texto para áudio usando ElevenLabs API (retorna False se não houver chave ou se falhar)."""
    import base64
    import tempfile
    import os
    
    try:
        # 1. Carregar chave e voz do config.json do cliente (client_config, não
        # o caminho antigo ~/.meu-agente — ver seção 20 do SKILL.md, 2026-08-02)
        eleven_key = client_config.get("elevenlabs_api_key", "")
        # Usar voz padrão "Rachel" (21m00Tcm4TlvDq8ikWAM) se não houver outra configurada
        voice_id = client_config.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")

        if not eleven_key:
            return False  # Sem chave, força fallback para gTTS sem erro

        # Pré-processar o texto para expandir preços por extenso
        message_clean = preprocess_text_for_tts(message)

        # 2. Chamar ElevenLabs API
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        payload = {
            "text": message_clean,
            # REVERTIDO pra eleven_multilingual_v2 em 2026-08-03 (seção 27 do
            # SKILL.md) — o eleven_v3 soava mais natural em conversa comum,
            # mas se mostrou NÃO CONFIÁVEL pra ler conteúdo estruturado/numérico
            # (endereço, CEP, valores): em teste real, o v3 chegou a OMITIR o
            # endereço inteiro de uma resposta (confirmado via transcrição —
            # foi direto de "Claro!" pra "Está tudo certinho?", pulando rua,
            # bairro, CEP). O v2 leu a mesma frase completa e correta. Errar
            # ou sumir com um endereço/CEP é muito pior que soar um pouco mais
            # "lido" — confiabilidade vem antes de naturalidade aqui.
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                # Estabilidade subida de 0.3 pra 0.55 e style baixado de 0.25 pra
                # 0.1 em 2026-08-03 (seção 28 do SKILL.md) — stability baixo dá
                # mais variação emocional, mas também mais chance de engasgo
                # aleatório (confirmado: mesma frase com preço "R$ 54,01" saiu
                # como "buasalhão" com stability 0.3, e saiu limpa em 3/3 testes
                # com stability 0.55). Cliente pediu confiabilidade acima de
                # naturalidade — esse é o equilíbrio ideal pra isso.
                "stability": 0.55,
                "similarity_boost": 0.75,
                "style": 0.1,
                "use_speaker_boost": True,
                "speed": 1.05,
            }
        }
        
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "xi-api-key": eleven_key,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
            method="POST"
        )
        
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            temp_path = f.name

        # Anti-ban: mostrar "gravando áudio..." já ANTES de chamar a ElevenLabs,
        # não depois — o v3 pode levar vários segundos pra gerar (ver seção 21 do
        # SKILL.md, 2026-08-02) e antes o lead ficava sem nenhum sinal visual
        # durante esse tempo real de espera. Agora o indicador cobre o tempo de
        # geração de verdade, em vez de um atraso artificial só no final.
        delay_alvo = humanized_typing_delay(message)
        send_presence(phone, "recording", int(delay_alvo * 1000))
        t_geracao_inicio = time.time()

        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                with open(temp_path, "wb") as f_out:
                    f_out.write(r.read())

            # 3. Converter para base64 e enviar via sendWhatsAppAudio
            with open(temp_path, "rb") as audio_file:
                audio_base64 = base64.b64encode(audio_file.read()).decode("utf-8")

            # Só completa o atraso humanizado se a geração real foi mais rápida
            # que o tempo que uma pessoa levaria — se já foi mais lenta (comum
            # com v3), não espera mais nada.
            restante = delay_alvo - (time.time() - t_geracao_inicio)
            if restante > 0:
                time.sleep(restante)
            _respect_send_spacing()

            # sendWhatsAppAudio (não sendMedia) é o endpoint que faz o áudio chegar
            # como mensagem de voz nativa (bolha redonda com onda sonora e mic),
            # igual quando uma pessoa grava e solta o dedo — sendMedia manda como
            # arquivo de áudio anexado, sem esse efeito. O campo "audio" tem que
            # ser base64 puro, SEM prefixo "data:audio/...;base64," — com prefixo
            # o Evolution rejeita com 400 "Owned media must be a url, base64...".
            result = evolution_request(
                f"/message/sendWhatsAppAudio/{INSTANCE_NAME}",
                method="POST",
                data={
                    "number": phone,
                    "audio": audio_base64,
                    "encoding": True
                },
                timeout=30
            )
            success = bool(result.get("key") or result.get("id"))
            if success:
                logger.info(f"📤 Áudio ElevenLabs enviado com sucesso para {phone}")
                _track_daily_send(phone)
                _registrar_sucesso_audio()
                return True
            else:
                logger.error(f"❌ Falha ao enviar áudio ElevenLabs para {phone}: {result}")
                _registrar_falha_audio(f"Evolution recusou o envio: {result}")
                return False
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    except urllib.error.HTTPError as e:
        # Corpo da resposta tem o motivo real (ex: chave inválida/revogada) --
        # sem isso o log só mostra "HTTP Error 400: Bad Request" e um erro de
        # autenticação parece indistinguível de um bug de texto/pronúncia.
        # Ver seção 35 do SKILL.md: 4 dias de áudio quebrado por chave errada,
        # diagnosticado tarde por falta desse detalhe no log.
        try:
            corpo = e.read().decode("utf-8", errors="replace")
        except Exception:
            corpo = "(sem corpo)"
        logger.error(f"Erro ao gerar/enviar áudio ElevenLabs para {phone}: HTTP {e.code} — {corpo}")
        _registrar_falha_audio(f"HTTP {e.code} — {corpo[:200]}")
        return False
    except Exception as e:
        logger.error(f"Erro ao gerar/enviar áudio ElevenLabs para {phone}: {e}")
        _registrar_falha_audio(str(e))
        return False


def send_whatsapp_audio(phone: str, message: str) -> bool:
    """Converte texto para áudio e envia como áudio do WhatsApp, utilizando EXCLUSIVAMENTE a ElevenLabs."""
    # Tentar com ElevenLabs. Se falhar ou não houver chave, retorna False para forçar o envio em texto!
    return send_whatsapp_audio_elevenlabs(phone, message)


# ── Envio de fotos/vídeos da biblioteca de mídia (seção 22, 2026-08-02) ──────

_MEDIATYPE_TO_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".mp4": "video/mp4",
}


def send_whatsapp_media(phone: str, file_path, mediatype: str) -> bool:
    """Envia uma foto ou vídeo real (arquivo local em MEDIA_DIR) via sendMedia
    da Evolution API. mediatype é "image" ou "video". Best-effort: nunca deve
    derrubar o resto do fluxo se o arquivo não existir ou o envio falhar."""
    import base64

    file_path = Path(file_path)
    if not file_path.exists():
        logger.error(f"❌ Arquivo de mídia não encontrado: {file_path}")
        return False

    mime = _MEDIATYPE_TO_MIME.get(file_path.suffix.lower(), "application/octet-stream")

    try:
        with open(file_path, "rb") as f:
            media_base64 = base64.b64encode(f.read()).decode("utf-8")

        delay = TYPING_MIN_SECONDS if mediatype == "image" else 3.0
        send_presence(phone, "composing", int(delay * 1000))
        time.sleep(delay)
        _respect_send_spacing()

        result = evolution_request(
            f"/message/sendMedia/{INSTANCE_NAME}",
            method="POST",
            data={
                "number": phone,
                "mediatype": mediatype,
                "mimetype": mime,
                "media": media_base64,
                "fileName": file_path.name,
            },
            timeout=45,
        )
        success = bool(result.get("key") or result.get("id"))
        if success:
            logger.info(f"📤 Mídia ({mediatype}) enviada com sucesso para {phone}: {file_path.name}")
            _track_daily_send(phone)
            return True
        logger.error(f"❌ Falha ao enviar mídia ({mediatype}) pra {phone}: {result}")
        return False
    except Exception as e:
        logger.error(f"Erro ao enviar mídia ({mediatype}) pra {phone}: {e}")
        return False


# ── Extração de mensagens ─────────────────────────────────────────────────────

def extract_message_data(msg) -> dict:
    """Extrai phone, nome e texto de uma mensagem da Evolution API."""
    if not isinstance(msg, dict):
        return {}

    key = msg.get("key", {})
    if not isinstance(key, dict):
        return {}

    # Ignorar mensagens enviadas por nós
    if key.get("fromMe", False):
        return {}

    remote_jid = key.get("remoteJid", "")

    # Ignorar grupos
    if "@g.us" in remote_jid:
        return {}

    # LID format (novo endereçamento WhatsApp para números fora dos contatos):
    # sempre preferir remoteJidAlt (número real) quando disponível, independente
    # do campo addressingMode — na prática ele nem sempre vem preenchido mesmo
    # quando remoteJid já está em formato @lid.
    if key.get("remoteJidAlt"):
        phone = key["remoteJidAlt"].replace("@s.whatsapp.net", "")
    elif "@lid" in remote_jid:
        # Sem remoteJidAlt não há número real utilizável — o ID @lid cru não é um
        # telefone válido pra Evolution API (sempre dá HTTP 400 ao tentar responder).
        # Ignorar em vez de ficar tentando enviar e falhando silenciosamente pro lead.
        return {}
    else:
        phone = remote_jid.replace("@s.whatsapp.net", "")

    push_name = msg.get("pushName", "Lead")

    # Extrair texto de diferentes formatos de mensagem
    message_content = msg.get("message", {})
    if not isinstance(message_content, dict):
        return {}

    is_audio = "audioMessage" in message_content

    text = (
        message_content.get("conversation") or
        (message_content.get("extendedTextMessage") or {}).get("text") or
        ""
    )

    return {
        "id": key.get("id", ""),
        "phone": phone,
        "name": push_name,
        "text": text.strip(),
        "is_audio": is_audio,
    }


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_ids": [], "last_run": None}


def save_state(state: dict):
    state["last_run"] = datetime.now().isoformat()
    # Manter apenas os últimos 500 IDs para não crescer indefinidamente
    if len(state["seen_ids"]) > 500:
        state["seen_ids"] = state["seen_ids"][-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Lembretes de Pagamento / Cobrança Ativa (Asaas) ──────────────────────────

def check_asaas_payment_status(payment_link_id: str) -> str:
    """
    Verifica se o link de pagamento Asaas foi pago.
    Retorna: 'PAID' (pago), 'PENDING' (pendente), ou 'NONE' (nenhuma tentativa/pago).
    """
    try:
        asaas_token = client_config.get("asaas_api_key", "")
        if not asaas_token:
            return "NONE"
            
        url = f"https://api.asaas.com/v3/payments?paymentLink={payment_link_id}"
        req = urllib.request.Request(
            url,
            headers={
                "Content-Type": "application/json",
                "access_token": asaas_token
            },
            method="GET"
        )
        
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read().decode("utf-8"))
            payments = res.get("data", [])
            if not payments:
                return "NONE"
                
            for p in payments:
                status = p.get("status", "")
                if status in ["RECEIVED", "CONFIRMED"]:
                    return "PAID"
                    
            return "PENDING"
    except Exception as e:
        logger.error(f"Erro ao verificar pagamento Asaas para o link {payment_link_id}: {e}")
        return "NONE"


def get_owner_jid() -> str:
    """Descobre o número da própria instância (usado como alvo da sonda de saúde)."""
    try:
        result = evolution_request("/instance/fetchInstances")
        instances = result if isinstance(result, list) else []
        for inst in instances:
            if inst.get("name") == INSTANCE_NAME or inst.get("instanceName") == INSTANCE_NAME:
                jid = inst.get("ownerJid", "")
                return jid.split("@")[0] if jid else ""
    except Exception as e:
        logger.warning(f"Não foi possível descobrir o ownerJid da instância: {e}")
    return ""


def is_evolution_socket_alive(owner_number: str) -> bool:
    """
    Testa se o socket do WhatsApp está realmente vivo.

    NÃO usa /instance/connectionState: esse endpoint lê o estado salvo no banco e
    responde "open" mesmo com o socket morto. /chat/fetchProfile força uma consulta
    real ao WhatsApp (leva ~1-2s e devolve dados frescos), então falha/trava quando
    a conexão caiu de verdade.
    """
    if not owner_number:
        return True  # sem alvo pra sondar, não dá pra afirmar que caiu

    url = f"{EVOLUTION_URL}/chat/fetchProfile/{INSTANCE_NAME}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"number": owner_number}).encode(),
        headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read())
            return bool(data.get("wuid"))
    except Exception as e:
        logger.warning(f"⚠️  Sonda de saúde da Evolution falhou: {e}")
        return False


def restart_evolution_instance() -> bool:
    """Reinicia a instância na Evolution API reaproveitando as credenciais salvas (sem QR)."""
    url = f"{EVOLUTION_URL}/instance/restart/{INSTANCE_NAME}"
    req = urllib.request.Request(
        url,
        data=b"",
        headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return True
    except Exception as e:
        logger.error(f"❌ Falha ao reiniciar a instância na Evolution API: {e}")
        return False


HUMAN_HANDOFF_TIMEOUT_SECONDS = 2 * 60 * 60  # 2 horas — ver seção 19 do SKILL.md (2026-08-02)


def check_human_handoff(phone: str, name: str, text: str) -> bool:
    """
    Verifica se a IA deve ficar de fora dessa mensagem porque o lead está em
    atendimento humano. Detecta pedidos novos de handoff (pausa a IA, avisa o
    cliente e notifica OWNER_PHONE).

    Reativação automática após HUMAN_HANDOFF_TIMEOUT_SECONDS (2h) desde o
    pedido de handoff — corrige o problema real de 2026-08-02: um lead ficou
    preso pra sempre nesse estado (mesmo digitando "resolvido") porque a
    única forma de reativar era o dono mandar "reativar NUMERO" pro próprio
    número do agente, e ele não lembrou/sabia do comando. Reativação manual
    (`reativar NUMERO`) continua funcionando a qualquer momento, antes do
    timeout.

    Retorna True se a mensagem foi interceptada (a IA NÃO deve responder).
    """
    import sessions
    lead_id = sessions.create_lead(phone, name=name)
    status = sessions.get_metadata(lead_id, "human_handoff", "0")

    if status == "1":
        handoff_at = sessions.get_metadata(lead_id, "human_handoff_at", "0")
        elapsed = int(time.time()) - int(handoff_at or "0")
        if elapsed >= HUMAN_HANDOFF_TIMEOUT_SECONDS:
            sessions.save_metadata(lead_id, "human_handoff", "0")
            logger.info(f"⏰ Handoff de {name} ({phone}) expirou após {elapsed // 60} min — IA reativada automaticamente.")
            # segue o fluxo normal abaixo (não retorna True) — a IA já responde essa mensagem
        else:
            logger.info(f"🙋 Mensagem de {name} ({phone}) ignorada pela IA — lead em atendimento humano.")
            return True

    if is_handoff_request(text):
        sessions.save_metadata(lead_id, "human_handoff", "1")
        sessions.save_metadata(lead_id, "human_handoff_at", str(int(time.time())))
        logger.info(f"🙋 {name} ({phone}) pediu atendimento humano. Pausando IA e notificando o responsável.")
        send_whatsapp(phone, "Claro! Só um momento que já vou te conectar com um de nossos atendentes. 🙋")
        if OWNER_PHONE:
            send_whatsapp(
                OWNER_PHONE,
                f"🔔 {name} ({phone}) pediu atendimento humano.\nÚltima mensagem: \"{text}\"\n\nA IA foi pausada para esse lead — responda direto por aqui no WhatsApp."
            )
        return True

    # Toldo NÃO aciona mais handoff humano (removido 2026-08-10, pedido do
    # cliente) -- agora tem preço médio de referência no system_prompt
    # (seção 24 do SKILL.md, "a partir de R$300/m², mínimo R$450/peça") e a
    # própria IA responde direto, igual qualquer outro produto. is_toldo_request
    # continua existindo em agent_core_template.py caso algum dia se queira
    # reativar um tratamento especial, mas não é mais chamado daqui.

    return False


def handle_owner_command(phone: str, text: str) -> bool:
    """
    Processa comandos enviados pelo dono diretamente para o número do agente.
    Retorna True se era um comando (mensagem não deve ir para a IA normal).

    Comandos disponíveis:
      reativar NUMERO  — reativa a IA para o lead com esse número
      pausar NUMERO    — pausa a IA para o lead com esse número
    """
    if not OWNER_PHONE or phone != OWNER_PHONE.replace("@s.whatsapp.net", ""):
        return False

    import sessions
    text_lower = text.strip().lower()

    if text_lower.startswith("reativar "):
        target = re.sub(r"\D", "", text_lower.replace("reativar ", "", 1))
        if not target:
            send_whatsapp(OWNER_PHONE, "⚠️ Formato: reativar 5585999999999")
            return True
        lead_id = f"whatsapp_{target}"
        sessions.save_metadata(lead_id, "human_handoff", "0")
        logger.info(f"🤖 IA reativada para {target} por comando do dono.")
        send_whatsapp(OWNER_PHONE, f"✅ IA reativada para {target}. O agente volta a responder normalmente.")
        return True

    if text_lower.startswith("pausar "):
        target = re.sub(r"\D", "", text_lower.replace("pausar ", "", 1))
        if not target:
            send_whatsapp(OWNER_PHONE, "⚠️ Formato: pausar 5585999999999")
            return True
        lead_id = f"whatsapp_{target}"
        sessions.save_metadata(lead_id, "human_handoff", "1")
        sessions.save_metadata(lead_id, "human_handoff_at", str(int(time.time())))
        logger.info(f"🙋 IA pausada para {target} por comando do dono.")
        send_whatsapp(OWNER_PHONE, f"✅ IA pausada para {target}. Você pode atender manualmente.")
        return True

    return False


def process_payment_followups():
    """Varre leads e processa lembretes de cobrança ativa de forma assíncrona/periódica.

    Anti-bloqueio: manda no máximo UM lembrete por chamada — como essa função já
    roda a cada ~10 minutos (ver watch()), isso garante um espaçamento bem maior
    que o mínimo de 30-90s entre mensagens automáticas pra leads diferentes, sem
    nunca travar o loop principal (que precisa continuar respondendo clientes em
    tempo real). O restante dos leads elegíveis é atendido no próximo ciclo.
    """
    try:
        import sessions

        leads_rows = sessions.get_leads_with_checkout()
        now_ts = int(time.time())

        for lead in leads_rows:
            lead_id = lead["id"]
            phone = lead["phone"]
            name = lead["name"]
            
            checkout_id = sessions.get_metadata(lead_id, "checkout_id")
            checkout_sent_at_str = sessions.get_metadata(lead_id, "checkout_sent_at")
            followup_status = sessions.get_metadata(lead_id, "followup_status", "0")

            if not checkout_id or not checkout_sent_at_str or followup_status == "PAID":
                continue

            # Não manda lembrete automático se um humano já assumiu essa conversa
            if sessions.get_metadata(lead_id, "human_handoff", "0") == "1":
                continue
                
            # 1. Verificar se já foi pago na Asaas
            payment_status = check_asaas_payment_status(checkout_id)
            if payment_status == "PAID":
                sessions.save_metadata(lead_id, "followup_status", "PAID")
                logger.info(f"🎉 Pagamento confirmado para o lead {name} ({phone})!")
                send_whatsapp(phone, f"Oba, {name}! 🎉 Confirmamos o recebimento do seu pagamento. O seu pedido de cortinas/persianas sob medida já foi encaminhado para o nosso setor de fabricação! Em breve te enviaremos o código de rastreamento por aqui. Qualquer dúvida, estou à disposição! 💪")
                return  # anti-bloqueio: só 1 envio por ciclo (ver docstring)
                
            # 2. Se não foi pago, calcular o tempo decorrido e enviar lembrete correspondente
            checkout_sent_at = int(checkout_sent_at_str)
            elapsed = now_ts - checkout_sent_at
            
            # Lembrete de Distração (Após 2 Horas / 7200 segundos)
            if elapsed >= 7200 and followup_status == "0":
                logger.info(f"⏳ Enviando lembrete de cobrança (2 horas) para {name} ({phone})")
                msg_2h = f"Olá, {name}! Vi que o seu link de checkout seguro para as persianas já está pronto, mas o pagamento ainda não foi confirmado. Ficou alguma dúvida ou precisa de ajuda para finalizar? 😊"
                # Só marca como enviado se o envio realmente funcionou — senão o lead
                # fica sem cobrança pra sempre caso a Evolution API falhe nesse instante
                # (o loop tenta de novo no próximo ciclo enquanto o status continuar "0").
                if send_whatsapp(phone, msg_2h):
                    sessions.save_metadata(lead_id, "followup_status", "1")
                    return  # anti-bloqueio: só 1 envio por ciclo (ver docstring)

            # Lembrete de Escassez / Fila da Fábrica (Após 24 Horas / 86400 segundos)
            elif elapsed >= 86400 and followup_status == "1":
                logger.info(f"⏳ Enviando lembrete de cobrança (24 horas) para {name} ({phone})")
                checkout_url = sessions.get_metadata(lead_id, "asaas_checkout_url", "agilcortinasepersianas.com.br/loja")
                msg_24h = f"Olá, {name}! Passando para lembrar que o lote de produção da nossa fábrica fecha hoje. Se você quiser garantir que as suas persianas entrem na fabricação desta semana para chegarem o quanto antes, basta finalizar o pagamento pelo link seguro: {checkout_url} 🚀"
                if send_whatsapp(phone, msg_24h):
                    sessions.save_metadata(lead_id, "followup_status", "2")
                    return  # anti-bloqueio: só 1 envio por ciclo (ver docstring)

    except Exception as e:
        logger.error(f"Erro no processamento de followups de cobrança: {e}\n{traceback.format_exc()}")


# Limites de silêncio pro lead que esfriou ANTES de chegar no checkout (não
# confundir com HEALTH_* / ELEVEN_* acima, que são de conexão/áudio, nem com
# os prazos de process_payment_followups, que são pra quem JÁ tem link mas
# não pagou). Prazos diferentes de propósito -- aqui ainda não houve
# compromisso nenhum do lead, só interesse demonstrado, então dá mais tempo
# antes do primeiro toque e um segundo toque bem mais espaçado (não é
# cobrança, é reengajamento, tom mais leve).
MIDFUNNEL_FIRST_NUDGE_SECONDS = 4 * 3600    # 4 horas
MIDFUNNEL_SECOND_NUDGE_SECONDS = 48 * 3600  # 48 horas


def process_midfunnel_followups():
    """Reengaja leads que deram medida (sinal real de interesse) mas
    sumiram ANTES de chegar no link de checkout -- o followup de cobrança
    (process_payment_followups) só cobre quem já tem link gerado; a maior
    parte da desistência normalmente acontece antes disso, no meio do
    funil, e até agora não tinha nenhum reengajamento automático pra esse
    caso (pedido do cliente, 2026-08-10 -- ver seção 38 do SKILL.md).

    Mesmo padrão anti-bloqueio de process_payment_followups: no máximo 1
    envio por chamada."""
    try:
        import sessions

        leads_rows = sessions.get_leads_sem_checkout()
        now_ts = int(time.time())

        for lead in leads_rows:
            lead_id = lead["id"]
            phone = lead["phone"]
            name = lead["name"] or "tudo bem"
            last_ts = lead["last_ts"]

            if not last_ts:
                continue

            # Só vale a pena reengajar quem deu um sinal real de interesse
            # (chegou a passar as medidas) -- evita mandar mensagem
            # automática pra alguém que só disse "oi" e sumiu, o que
            # incomoda mais do que ajuda.
            largura = sessions.get_metadata(lead_id, "width")
            altura = sessions.get_metadata(lead_id, "height")
            if not largura or not altura:
                continue

            if sessions.get_metadata(lead_id, "human_handoff", "0") == "1":
                continue

            status = sessions.get_metadata(lead_id, "midfunnel_followup_status", "0")
            if status == "2":
                continue  # já mandou os 2 toques dessa rodada de silêncio

            elapsed = now_ts - int(last_ts)

            if elapsed >= MIDFUNNEL_FIRST_NUDGE_SECONDS and status == "0":
                logger.info(f"💤 Reengajando lead esfriado (4h, sem checkout) {name} ({phone})")
                msg = (
                    f"Oi, {name}! Vi que a gente tava conversando sobre a sua persiana "
                    f"de {largura}m x {altura}m e ficou por aqui. Ficou alguma dúvida ou "
                    f"posso te ajudar a fechar o orçamento? 😊"
                )
                if send_whatsapp(phone, msg):
                    sessions.save_metadata(lead_id, "midfunnel_followup_status", "1")
                    return  # anti-bloqueio: só 1 envio por ciclo

            elif elapsed >= MIDFUNNEL_SECOND_NUDGE_SECONDS and status == "1":
                logger.info(f"💤 Reengajando lead esfriado (48h, sem checkout) {name} ({phone})")
                msg = (
                    f"Oi, {name}! Só passando pra saber se ainda faz sentido pra você — "
                    "se precisar de mais alguma informação ou quiser retomar o orçamento "
                    "da sua persiana, é só me chamar por aqui, tá bom? 🙂"
                )
                if send_whatsapp(phone, msg):
                    sessions.save_metadata(lead_id, "midfunnel_followup_status", "2")
                    return  # anti-bloqueio: só 1 envio por ciclo

    except Exception as e:
        logger.error(f"Erro no processamento de followups de meio de funil: {e}\n{traceback.format_exc()}")


# ── Transcrição de Áudio (Whisper) ───────────────────────────────────────────

def transcribe_audio_base64(base64_str: str) -> str:
    """Transcreve um áudio em base64 usando o Groq ou OpenAI Whisper API."""
    import base64
    import tempfile
    import os
    
    try:
        # Carregar chaves do ~/.config/watch/.env se disponíveis
        watch_env_path = Path.home() / ".config" / "watch" / ".env"
        groq_key = ""
        openai_key = ""
        
        if watch_env_path.exists():
            for line in watch_env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("GROQ_API_KEY="):
                    groq_key = line.split("=", 1)[1].strip()
                elif line.startswith("OPENAI_API_KEY="):
                    openai_key = line.split("=", 1)[1].strip()

        # O config.json do cliente (client_config) é o lugar canônico das chaves
        # deste projeto — o .env acima pertence a outra ferramenta e pode
        # simplesmente não existir no servidor (foi o que deixou a transcrição
        # de áudio muda no VPS até 2026-07-28).
        if not groq_key and not openai_key:
            groq_key = client_config.get("groq_api_key", "") or ""
            openai_key = client_config.get("openai_api_key", "") or ""


        # Configurar endpoints e modelo
        if groq_key:
            api_url = "https://api.groq.com/openai/v1/audio/transcriptions"
            api_key = groq_key
            model = "whisper-large-v3"
        elif openai_key:
            api_url = "https://api.openai.com/v1/audio/transcriptions"
            api_key = openai_key
            model = "whisper-1"
        else:
            # Se não houver no watch, só reaproveita a ai_api_key do config.json quando o
            # provider configurado for realmente "openai" — chaves Anthropic/Gemini não
            # funcionam no endpoint de transcrição da OpenAI e falhariam com 401 silencioso.
            if client_config.get("ai_provider") == "openai":
                openai_key = client_config.get("ai_api_key", "")
            if openai_key and len(openai_key) > 30:  # Evitar chaves curtas
                api_url = "https://api.openai.com/v1/audio/transcriptions"
                api_key = openai_key
                model = "whisper-1"
            else:
                logger.warning("Nenhuma chave de Whisper (Groq/OpenAI) disponível.")
                return ""
            
        audio_data = base64.b64decode(base64_str)
        
        # Fazer a requisição multipart POST manual com urllib
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        parts = []
        
        parts.append(f"--{boundary}")
        parts.append('Content-Disposition: form-data; name="model"')
        parts.append("")
        parts.append(model)
        
        parts.append(f"--{boundary}")
        parts.append('Content-Disposition: form-data; name="file"; filename="audio.mp3"')
        parts.append("Content-Type: audio/mpeg")
        parts.append("")
        
        body_bytes = b""
        for part in parts:
            body_bytes += part.encode("utf-8") + b"\r\n"
            
        body_bytes += audio_data + b"\r\n"
        body_bytes += f"--{boundary}--\r\n".encode("utf-8")
        
        req = urllib.request.Request(
            api_url,
            data=body_bytes,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode("utf-8"))
            return res.get("text", "")
    except Exception as e:
        logger.error(f"Erro ao transcrever áudio Whisper: {e}")
        return ""


# ── Loop principal ────────────────────────────────────────────────────────────

def watch():
    logger.info("🔍 Watcher iniciado")
    state = load_state()
    iteration_counter = 0
    first_run = True
    _stop = {"requested": False}

    def _handle_sigterm(signum, frame):
        logger.info("⏹️  SIGTERM recebido — encerrando após salvar estado...")
        _stop["requested"] = True

    signal.signal(signal.SIGTERM, _handle_sigterm)

    owner_number = get_owner_jid()
    if owner_number:
        logger.info(f"🩺 Watchdog de conexão ativo (sondando {owner_number} a cada ~{HEALTH_CHECK_EVERY * POLL_INTERVAL // 60} min).")
    else:
        logger.warning("🩺 Watchdog de conexão inativo — não foi possível descobrir o número da instância.")
    health_failures = 0
    last_restart_at = 0

    while True:
        try:
            # Manutenção periódica a cada 200 iterações (~10 minutos)
            if iteration_counter % 200 == 0:
                process_payment_followups()
                process_midfunnel_followups()
                import sessions as _sessions
                _sessions.cleanup_expired_sessions()

            # Watchdog: detecta o "estado zumbi" da Evolution (socket morto com
            # connectionState mentindo "open") e reconecta sozinho.
            if owner_number and iteration_counter > 0 and iteration_counter % HEALTH_CHECK_EVERY == 0:
                if is_evolution_socket_alive(owner_number):
                    if health_failures:
                        logger.info("🩺 Conexão com o WhatsApp normalizada.")
                    health_failures = 0
                else:
                    health_failures += 1
                    logger.warning(f"🩺 Sonda falhou ({health_failures}/{HEALTH_FAIL_THRESHOLD}).")
                    if health_failures >= HEALTH_FAIL_THRESHOLD:
                        if int(time.time()) - last_restart_at < HEALTH_RESTART_COOLDOWN:
                            logger.warning("🩺 Reinício automático adiado (cooldown ativo).")
                        else:
                            logger.error("🩺 Conexão do WhatsApp caiu silenciosamente — reiniciando a instância...")
                            last_restart_at = int(time.time())
                            health_failures = 0
                            if restart_evolution_instance():
                                time.sleep(15)
                                if is_evolution_socket_alive(owner_number):
                                    logger.info("✅ Instância reconectada automaticamente.")
                                    if OWNER_PHONE:
                                        send_whatsapp(OWNER_PHONE, "🩺 A conexão do WhatsApp do agente caiu e foi restabelecida automaticamente. Nenhuma ação necessária.")
                                else:
                                    logger.error("❌ Reinício não restabeleceu a conexão — pode ser necessário escanear o QR Code novamente.")
                                    if OWNER_PHONE:
                                        send_whatsapp(OWNER_PHONE, "🚨 A conexão do WhatsApp do agente caiu e o reinício automático NÃO resolveu. Provavelmente é preciso escanear o QR Code de novo — o agente está sem responder.")
            iteration_counter += 1

            messages = fetch_messages(count=20)

            # Ignorar mensagens históricas na primeira execução para evitar flood/travamento da API do Gemini
            if first_run:
                for msg in messages:
                    msg_data = extract_message_data(msg)
                    if msg_data and msg_data.get("id"):
                        if msg_data["id"] not in state["seen_ids"]:
                            state["seen_ids"].append(msg_data["id"])
                save_state(state)
                first_run = False
                logger.info("✅ Mensagens do histórico ignoradas com sucesso na inicialização.")
                time.sleep(POLL_INTERVAL)
                continue

            # Agrupar mensagens novas por lead — se um lead mandar várias mensagens
            # no mesmo ciclo de polling (rafada), processamos só a última e marcamos
            # todas como vistas. Isso evita N chamadas à IA por spam sem perder o
            # contexto (a última mensagem carrega a intenção final do lead).
            batch: dict[str, list] = {}
            for msg in messages:
                msg_data = extract_message_data(msg)
                if not msg_data or not msg_data.get("phone"):
                    continue
                if msg_data["id"] in state["seen_ids"]:
                    continue
                batch.setdefault(msg_data["phone"], []).append(msg_data)

            for phone, lead_msgs in batch.items():
                for m in lead_msgs:
                    state["seen_ids"].append(m["id"])

                if len(lead_msgs) > 1:
                    logger.debug(f"⏩ {phone}: {len(lead_msgs) - 1} mensagem(ns) agrupada(s) — processando só a última.")

                msg_data = lead_msgs[-1]
                msg_id = msg_data["id"]
                name = msg_data["name"]
                text = msg_data["text"]
                is_audio = msg_data.get("is_audio", False)

                # Se for mensagem de áudio, baixar e transcrever
                if is_audio:
                    logger.info(f"🎤 {name} ({phone}) enviou um áudio. Baixando e transcrevendo...")
                    media_res = evolution_request(
                        f"/chat/getBase64FromMediaMessage/{INSTANCE_NAME}",
                        method="POST",
                        data={
                            "message": {
                                "key": {
                                    "id": msg_id
                                }
                            },
                            "convertToMp3": True
                        }
                    )
                    base64_audio = media_res.get("base64")
                    if base64_audio:
                        transcribed_text = transcribe_audio_base64(base64_audio)
                        if transcribed_text:
                            text = transcribed_text
                            logger.info(f"🎤 Áudio Transcrito: {text}")
                        else:
                            logger.warning("❌ Falha ao transcrever o áudio.")
                            send_whatsapp(phone, "Desculpe, não consegui compreender o seu áudio. Você poderia digitar ou enviar novamente? 😊")
                            continue
                    else:
                        logger.error("❌ Falha ao obter base64 do áudio da Evolution API.")
                        continue

                # Se após processamento de áudio o texto estiver vazio, ignora
                if not text.strip():
                    continue

                logger.info(f"📩 {name} ({phone}): {text[:60]}")

                if handle_owner_command(phone, text):
                    continue

                if check_human_handoff(phone, name, text):
                    continue

                try:
                    response, media_requests, forcar_texto = handle_message(phone, name, text)
                    if response:
                        if is_audio and not forcar_texto:
                            logger.info(f"📤 Gerando e enviando resposta em áudio para {phone}...")
                            audio_success = send_whatsapp_audio(phone, response)
                            if not audio_success:
                                # Fallback para texto se falhar
                                send_whatsapp(phone, response)
                            else:
                                # Um link falado não dá pra clicar — se a resposta
                                # tinha algum (ex: link de pagamento), manda também
                                # por escrito logo em seguida, só com o(s) link(s).
                                urls = URL_PATTERN.findall(response)
                                if urls:
                                    send_whatsapp(phone, "🔗 " + "\n".join(urls))
                        else:
                            send_whatsapp(phone, response)

                        # Fotos/vídeos que a IA pediu pra mandar (tag [FOTO:.]/
                        # [VIDEO:.] na resposta) vão depois do texto, uma de cada vez.
                        for item in media_requests:
                            send_whatsapp_media(phone, item["path"], item["mediatype"])
                    else:
                        logger.debug("⏭️  Não é trigger — ignorado")
                except Exception as e:
                    logger.error(f"Erro ao processar mensagem: {e}\n{traceback.format_exc()}")

            save_state(state)

            if _stop["requested"]:
                break

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("⏹️  Watcher encerrado")
            save_state(state)
            break
        except Exception as e:
            logger.error(f"Erro no loop: {e}\n{traceback.format_exc()}")
            time.sleep(5)


if __name__ == "__main__":
    watch()
