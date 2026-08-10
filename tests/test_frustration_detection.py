#!/usr/bin/env python3
"""Testes de regressão pra detecção de cliente travado/frustrado
(is_frustration_keyword, is_repeated_question em agent_core_template.py) e
pra lógica de contador/escalação em check_human_handoff() (watcher_template.py).

Pedido do cliente (2026-08-10): antes só escalava pra atendente humano em
pedido EXPLÍCITO. Cliente repetindo a mesma dúvida, ou hesitando várias
vezes, virava venda perdida silenciosa -- ver seção 39 do SKILL.md.

Extrai as funções direto do arquivo fonte (agent_core_template.py importa
client_config no topo e falha fora de uma implantação real) -- as duas
funções testadas aqui são puras (só usam `re`/`difflib`), não precisam
dessa dependência.
"""
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = REPO_ROOT / "templates" / "shared" / "agent_core_template.py"


def carregar_funcoes():
    src = SOURCE_PATH.read_text(encoding="utf-8")
    inicio = src.index("FRUSTRATION_KEYWORDS = [")
    fim = src.index("def format_checkout_message")
    trecho = src[inicio:fim]

    import difflib
    namespace = {"re": re, "difflib": difflib}
    exec(compile(trecho, str(SOURCE_PATH), "exec"), namespace)
    return namespace


FUNCOES = carregar_funcoes()
is_frustration_keyword = FUNCOES["is_frustration_keyword"]
is_repeated_question = FUNCOES["is_repeated_question"]


# ── is_frustration_keyword: (nome, texto, esperado) ─────────────────────────
CASOS_KEYWORD = [
    ("frase clássica de confusão", "não entendi nada disso", True),
    ("repetição explícita", "já falei isso antes, várias vezes", True),
    ("negação de resposta", "isso não é o que eu perguntei", True),
    ("dupla interrogação", "mas isso não resolve??", True),
    ("maiúsculas longas (gritando)", "ISSO NAO TA FAZENDO SENTIDO NENHUM", True),
    ("cansaço", "cansei disso, muito complicado", True),
    ("robô/automação", "acho que isso é resposta automática", True),
    ("mensagem normal e neutra", "Quero uma persiana pro quarto", False),
    ("pergunta normal com 1 interrogação", "Qual a cor disponível?", False),
    ("sigla curta em maiúsculas", "CEP 88085250", False),
    ("confirmação simples", "Sim, pode ser essa", False),
    ("vazio", "", False),
]

# ── is_repeated_question: (nome, mensagem atual, mensagens anteriores, esperado) ──
CASOS_REPETICAO = [
    (
        "mesma pergunta quase idêntica",
        "quanto custa a persiana pro meu quarto?",
        ["oi, tudo bem?", "quanto custa a persiana do meu quarto?"],
        True,
    ),
    (
        "hesitação repetida (não sei x2)",
        "não sei, qualquer uma",
        ["não sei, qualquer uma"],
        True,
    ),
    (
        "hesitação repetida com variação leve",
        "acho que não sei ainda",
        ["não sei ainda"],
        True,
    ),
    (
        "mensagens genuinamente diferentes",
        "prefiro a cor branca",
        ["quero pro quarto", "1,20 de largura por 1,50 de altura"],
        False,
    ),
    (
        "respostas curtas genéricas não contam (abaixo do limite de tamanho)",
        "sim",
        ["sim", "sim", "sim"],
        False,
    ),
    (
        "sem histórico anterior",
        "quanto custa a persiana pro meu quarto?",
        [],
        False,
    ),
    (
        "mensagem atual curta não conta mesmo com histórico longo repetido",
        "oi",
        ["quanto custa a persiana pro meu quarto?"],
        False,
    ),
]


def rodar_testes():
    falhas = []

    for nome, texto, esperado in CASOS_KEYWORD:
        obtido = is_frustration_keyword(texto)
        if obtido != esperado:
            falhas.append(f"[is_frustration_keyword: {nome}] texto={texto!r} esperado={esperado} obtido={obtido}")

    for nome, atual, anteriores, esperado in CASOS_REPETICAO:
        obtido = is_repeated_question(atual, anteriores)
        if obtido != esperado:
            falhas.append(
                f"[is_repeated_question: {nome}] atual={atual!r} anteriores={anteriores!r} "
                f"esperado={esperado} obtido={obtido}"
            )

    # --- Lógica de contador/escalação (réplica simplificada do bloco em
    # check_human_handoff(), sem tocar em DB/rede) ---
    FRUSTRATION_THRESHOLD = 2

    def simular_check(historico_sinais, texto, anteriores):
        """historico_sinais: contador atual (simula sessions.get_metadata).
        Retorna (escalou: bool, novo_contador: int)."""
        sinal = is_repeated_question(texto, anteriores) or is_frustration_keyword(texto)
        if not sinal:
            return False, historico_sinais
        novo = historico_sinais + 1
        if novo >= FRUSTRATION_THRESHOLD:
            return True, 0  # escalou e reseta
        return False, novo

    # 1 sinal isolado não escala
    escalou, contador = simular_check(0, "não entendi nada disso", [])
    if escalou or contador != 1:
        falhas.append(f"[contador] 1 sinal isolado nao deveria escalar -- escalou={escalou} contador={contador}")

    # 2 sinais seguidos escalam
    escalou, contador = simular_check(1, "confuso, muito confuso mesmo", [])
    if not escalou or contador != 0:
        falhas.append(f"[contador] 2o sinal deveria escalar e resetar -- escalou={escalou} contador={contador}")

    # mensagem normal não soma nada ao contador
    sinal = is_repeated_question("quero cor branca", ["oi", "quanto custa?"]) or is_frustration_keyword(
        "quero cor branca"
    )
    if sinal:
        falhas.append("[contador] mensagem normal nao deveria contar como sinal de frustracao")

    total = len(CASOS_KEYWORD) + len(CASOS_REPETICAO) + 3
    if falhas:
        print(f"❌ {len(falhas)}/{total} verificações falharam:\n")
        for f in falhas:
            print(f)
        return False

    print(f"✅ {total}/{total} verificações passaram.")
    return True


if __name__ == "__main__":
    ok = rodar_testes()
    sys.exit(0 if ok else 1)
