"""
agent_core_template.py — Núcleo da lógica de IA (reutilizável para todos os módulos)

Use este template como base para criar:
  - ~/meu-agente/agent.py (módulo WhatsApp)
  - FastAPI handler /chat (módulo Widget, v1.1+)

Substitua {{placeholders}} com dados do usuário durante setup.
"""

import difflib
import json
import re
import urllib.request
import urllib.error

# Configurações ({{placeholders}} preenchidos durante setup)
# Configurações do cliente — carregadas em tempo de execução (multi-cliente).
# Antes ficavam "coladas" aqui via {{placeholders}}; agora vêm do config.json
# da pasta do cliente, então o mesmo código atende vários clientes.
import client_config

AI_PROVIDER = client_config.require("ai_provider")   # "openai" | "gemini" | "anthropic"
AI_MODEL = client_config.require("ai_model")
AI_API_KEY = client_config.require("ai_api_key")

CHECKOUT_LINK = client_config.get("checkout_link", "")
SYSTEM_PROMPT = client_config.require("system_prompt")  # Prompt BANT gerado automaticamente

# ── Módulo FAQ / base de conhecimento (added 2026-08-11) ────────────────────
# Diferente de pós-venda/agendamento, FAQ não precisa de detector de intenção
# nem de lógica de código — a própria IA já é boa o suficiente pra reconhecer
# quando uma pergunta bate com uma entrada da lista e responder com as
# próprias palavras. Por isso o mecanismo é só: se o cliente tiver um
# faq.json na pasta dele, ele entra direto no SYSTEM_PROMPT em tempo de
# execução (não precisa regenerar o prompt inteiro pra atualizar uma
# resposta — só editar o arquivo e reiniciar o serviço). Formato esperado do
# arquivo: lista de {"pergunta": "...", "resposta": "..."}.
FAQ_PATH = client_config.CLIENT_DIR / "faq.json"


def _load_faq() -> list:
    try:
        return json.loads(FAQ_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _format_faq_block(faq_items: list) -> str:
    if not faq_items:
        return ""
    linhas = [
        "",
        "── Perguntas frequentes (use estas respostas quando o assunto bater; "
        "nunca invente política, prazo ou garantia que não esteja aqui) ──",
    ]
    for item in faq_items:
        pergunta = (item or {}).get("pergunta", "").strip()
        resposta = (item or {}).get("resposta", "").strip()
        if pergunta and resposta:
            linhas.append(f"P: {pergunta}\nR: {resposta}")
    return "\n".join(linhas) if len(linhas) > 2 else ""


_faq_block = _format_faq_block(_load_faq())
if _faq_block:
    SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + _faq_block

# Constantes
SESSION_TTL = 1800  # 30 minutos


def call_ai(messages: list, max_tokens: int = 4096) -> str:
    """
    Chama IA baseado no provider configurado.

    Args:
        messages: Lista de mensagens [{"role": "user", "content": "..."}, ...]
        max_tokens: Máximo de tokens na resposta

    Returns:
        String com resposta da IA
    """

    if AI_PROVIDER == "openai":
        return call_openai(messages, max_tokens)
    elif AI_PROVIDER == "gemini":
        return call_gemini(messages, max_tokens)
    elif AI_PROVIDER == "anthropic":
        return call_anthropic(messages, max_tokens)
    else:
        raise ValueError(f"Provider desconhecido: {AI_PROVIDER}")


def call_openai(messages: list, max_tokens: int) -> str:
    """Chama OpenAI API (gpt-5.4-mini)."""
    url = "https://api.openai.com/v1/chat/completions"

    data = {
        "model": AI_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "max_completion_tokens": max_tokens,  # NÃO usar max_tokens com gpt-5.4-mini!
        "temperature": 0.7
    }

    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read())
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        return f"Erro OpenAI: {e.reason}"


def call_gemini(messages: list, max_tokens: int) -> str:
    """Chama Google Gemini (endpoint OpenAI-compatible)."""
    url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

    data = {
        "model": AI_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "max_tokens": max_tokens,
        "temperature": 0.7
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AI_API_KEY}"
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read())
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        return f"Erro Gemini: {e.reason}"


def call_anthropic(messages: list, max_tokens: int) -> str:
    """Chama Anthropic Claude (formato próprio).

    A API da Anthropic só aceita roles "user"/"assistant" na lista de mensagens
    (diferente de OpenAI/Gemini) — instruções injetadas com role "system" no meio
    da conversa precisam ser incorporadas ao parâmetro "system" separado.
    """
    url = "https://api.anthropic.com/v1/messages"

    system_parts = [SYSTEM_PROMPT]
    clean_messages = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(m.get("content", ""))
        else:
            clean_messages.append(m)

    data = {
        "model": AI_MODEL,
        "max_tokens": max_tokens,
        "system": "\n\n".join(system_parts),
        "messages": clean_messages
    }

    headers = {
        "x-api-key": AI_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode(),
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.loads(response.read())
            return result["content"][0]["text"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"[ERRO Anthropic] {e.code} {e.reason}: {detail}")
        return "Desculpe, tive um probleminha técnico agora. Pode repetir sua última mensagem, por favor? 🙏"


def is_purchase_intent(message: str, conversation: list = None) -> bool:
    """
    Detecta se o lead tem intenção real de fechamento/compra (para envio de link).
    """
    if not message:
        return False

    message_lower = message.lower()
    
    # Palavras-chave de alto interesse de fechamento (solicitação explícita de link ou pagamento)
    closing_keywords = [
        "manda o link", "me manda o link", "enviar o link", "envia o link", 
        "passa o link", "link de pagamento", "link para pagar", "como faço para comprar",
        "quero comprar", "quero fechar", "gerar o link", "onde eu pago", "link de compra",
        "link do checkout", "passa o pix", "me manda o pix", "chave pix", "pagar no pix",
        "comprar agora", "fechar pedido", "fechar o pedido", "fazer o pagamento"
    ]

    # Verificar se o cliente solicitou diretamente o link ou fechamento
    if any(kw in message_lower for kw in closing_keywords):
        return True

    return False


def is_handoff_request(message: str) -> bool:
    """
    Detecta se o lead está pedindo explicitamente para falar com um atendente humano
    (em vez de continuar com a IA). Usado para pausar as respostas automáticas do
    agente e notificar o dono do negócio pra assumir a conversa manualmente.
    """
    if not message:
        return False

    message_lower = message.lower()

    handoff_phrases = [
        "falar com atendente", "falar com um atendente", "falar com uma pessoa",
        "falar com humano", "falar com um humano", "quero um atendente",
        "atendente humano", "atendimento humano", "não quero falar com robô",
        "nao quero falar com robo", "não quero falar com um robô",
        "quero falar com alguém de verdade", "quero falar com alguem de verdade",
        "me transfere pra um atendente", "me transfere para um atendente",
        "quero um vendedor", "falar com vendedor", "falar com um vendedor",
        "isso é um robô", "isso e um robo", "você é um robô", "voce e um robo",
        "quero suporte humano", "falar com gerente", "falar com o responsável",
        "falar com o responsavel", "chama o dono", "quero falar com o dono"
    ]

    return any(kw in message_lower for kw in handoff_phrases)


def is_toldo_request(message: str) -> bool:
    """
    Detecta se o lead está perguntando sobre Toldo — produto sem preço
    calculado automaticamente (sem tabela de referência de mercado). Usado
    pra acionar o mesmo encaminhamento humano do is_handoff_request, já que a
    IA não deve inventar preço de toldo.
    """
    if not message:
        return False

    message_lower = message.lower()
    return "toldo" in message_lower


# ── Detecção de cliente travado/frustrado (added 2026-08-10) ────────────────
# Até aqui só escalava pra humano em pedido EXPLÍCITO ("falar com atendente").
# Um cliente repetindo a mesma dúvida, ou hesitando várias vezes sem sair do
# lugar, virava venda perdida silenciosa -- a IA continuava tentando do
# mesmo jeito, sem ninguém perceber que aquele lead precisava de ajuda
# humana. Dois sinais independentes, cada um soma no mesmo contador
# (frustration_signal_count em check_human_handoff/watcher.py) -- só escala
# depois de FRUSTRATION_THRESHOLD sinais, pra não pausar a IA por causa de
# uma mensagem isolada/brincadeira.

FRUSTRATION_KEYWORDS = [
    "não entendi", "nao entendi", "não entendo", "nao entendo",
    "já disse", "ja disse", "já falei", "ja falei", "já te falei", "ja te falei",
    "não é isso", "nao e isso", "não é isso que eu", "nao e isso que eu",
    "não foi isso", "nao foi isso", "não é o que eu perguntei", "nao e o que eu perguntei",
    "de novo?", "outra vez?", "pela segunda vez", "pela terceira vez",
    "confuso", "confusa", "muito confuso", "muito confusa", "complicado demais",
    "não ajuda", "nao ajuda", "isso não resolve", "isso nao resolve",
    "não estou entendendo", "nao estou entendendo",
    "cansei disso", "cansado disso", "cansada disso", "que saco", "que chato",
    "isso é um robô", "isso e um robo", "só fala isso", "so fala isso",
    "resposta automática", "resposta automatica", "não responde minha pergunta",
    "nao responde minha pergunta", "tá difícil", "ta dificil", "muito difícil isso",
]


def is_frustration_keyword(message: str) -> bool:
    """Detecta linguagem explícita de frustração/confusão do cliente (ex:
    "não entendi", "já falei isso"). Conta como 1 sinal pro contador de
    frustração -- não escala sozinho, precisa somar com outro sinal."""
    if not message:
        return False
    message_lower = message.lower()

    if any(kw in message_lower for kw in FRUSTRATION_KEYWORDS):
        return True

    # "???"/"??" -- confusão/impaciência (mas não conta "?" único, que é
    # pergunta normal) e mensagem toda em maiúsculas (tipo "gritando") só
    # quando tem tamanho real, pra não pegar sigla curta tipo "CEP" ou "OK".
    if "??" in message_lower:
        return True
    letras = re.sub(r"[^a-zA-ZÀ-ÿ]", "", message)
    if len(letras) >= 15 and letras.isupper():
        return True

    return False


def is_repeated_question(message: str, previous_user_messages: list) -> bool:
    """Detecta se a mensagem atual é muito parecida com alguma das últimas
    mensagens do próprio cliente -- pega tanto "perguntou a mesma coisa de
    novo" quanto "hesitou várias vezes" (ex: "não sei" repetido é, por
    definição, muito similar a si mesmo). Ignora mensagens curtas (menos de
    8 caracteres) pra não disparar em respostas genéricas tipo "oi"/"sim"/"ok",
    que são naturalmente parecidas entre si sem indicar nada de errado."""
    if not message or len(message.strip()) < 8:
        return False
    if not previous_user_messages:
        return False

    atual = re.sub(r"\s+", " ", message.strip().lower())

    for anterior in previous_user_messages:
        if not anterior or len(anterior.strip()) < 8:
            continue
        comparar = re.sub(r"\s+", " ", anterior.strip().lower())
        if difflib.SequenceMatcher(None, atual, comparar).ratio() >= 0.6:
            return True

    return False


# ── Pós-venda: detecção de dúvida sobre pedido já feito (added 2026-08-11) ──

ORDER_STATUS_KEYWORDS = [
    "meu pedido", "o meu pedido", "status do pedido", "status do meu pedido",
    "cadê meu pedido", "cade meu pedido", "cadê o meu pedido", "cade o meu pedido",
    "onde está meu pedido", "onde esta meu pedido", "quando chega", "quando que chega",
    "previsão de entrega", "previsao de entrega", "rastreio", "código de rastreio",
    "codigo de rastreio", "já foi enviado", "ja foi enviado", "foi despachado",
    "já saiu pra entrega", "ja saiu pra entrega", "acompanhar pedido", "acompanhar o pedido",
]


def is_order_status_request(message: str) -> bool:
    """Detecta se o lead está perguntando sobre o andamento de uma compra já
    feita (pós-venda) — diferente de is_purchase_intent, que é sobre iniciar
    uma compra nova. Usado pra injetar o status real do pedido no contexto
    da IA em vez de deixar ela inventar ou dizer que não sabe."""
    if not message:
        return False
    message_lower = message.lower()
    return any(kw in message_lower for kw in ORDER_STATUS_KEYWORDS)


# ── Agendamento: detecção de intenção de marcar horário (added 2026-08-11) ──

SCHEDULING_KEYWORDS = [
    "agendar", "marcar uma visita", "marcar visita", "marcar um horário",
    "marcar horário", "marcar horario", "quero agendar", "quero marcar",
    "tem horário disponível", "tem horario disponivel", "que horas vocês atendem",
    "que horas voces atendem", "disponibilidade", "posso passar aí", "posso passar ai",
    "marcar uma instalação", "marcar instalação", "marcar instalacao",
]


def is_scheduling_request(message: str) -> bool:
    """Detecta se o lead está tentando marcar um horário (visita técnica,
    instalação, demonstração, etc.) — não confirma sozinho, só sinaliza pra
    handle_message() tentar extrair data/hora e abrir um pedido de
    agendamento pendente de confirmação humana."""
    if not message:
        return False
    message_lower = message.lower()
    return any(kw in message_lower for kw in SCHEDULING_KEYWORDS)


def format_checkout_message(url: str = CHECKOUT_LINK) -> str:
    """Formata mensagem com link de checkout."""
    return f"""Perfeito! Passei tudo aqui. Deixa eu enviar nosso checkout pra você:

{url}

Qualquer dúvida depois da compra, eu fico por aqui! 💪"""
