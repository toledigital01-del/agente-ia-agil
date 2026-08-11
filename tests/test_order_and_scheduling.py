#!/usr/bin/env python3
"""Testes de regressão pros módulos de pós-venda (status de pedido) e
agendamento, adicionados em 2026-08-11 (ver seção 40/41 do SKILL.md).

Cobre:
- is_order_status_request / is_scheduling_request (agent_core_template.py)
- _has_datetime_hint / _infer_appointment_tipo (agent_template.py)

Mesma técnica dos outros testes desta pasta: extrai as funções puras direto
do arquivo fonte via marcador de texto, sem importar o módulo inteiro (que
depende de client_config/sessions, só existentes numa implantação real).
"""
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_CORE_PATH = REPO_ROOT / "templates" / "shared" / "agent_core_template.py"
AGENT_TEMPLATE_PATH = REPO_ROOT / "templates" / "whatsapp" / "agent_template.py"


def carregar_agent_core():
    src = AGENT_CORE_PATH.read_text(encoding="utf-8")
    inicio = src.index("ORDER_STATUS_KEYWORDS = [")
    fim = src.index("def format_checkout_message")
    trecho = src[inicio:fim]
    namespace = {"re": re}
    exec(compile(trecho, str(AGENT_CORE_PATH), "exec"), namespace)
    return namespace


def carregar_agent_template():
    src = AGENT_TEMPLATE_PATH.read_text(encoding="utf-8")
    inicio = src.index("_DIAS_SEMANA = [")
    fim = src.index("def handle_message")
    trecho = src[inicio:fim]
    namespace = {"re": re}
    exec(compile(trecho, str(AGENT_TEMPLATE_PATH), "exec"), namespace)
    return namespace


CORE = carregar_agent_core()
TEMPLATE = carregar_agent_template()
is_order_status_request = CORE["is_order_status_request"]
is_scheduling_request = CORE["is_scheduling_request"]
_has_datetime_hint = TEMPLATE["_has_datetime_hint"]
_infer_appointment_tipo = TEMPLATE["_infer_appointment_tipo"]


# ── is_order_status_request: (nome, texto, esperado) ────────────────────────
CASOS_ORDER_STATUS = [
    ("pergunta direta", "cadê meu pedido?", True),
    ("status explícito", "qual o status do meu pedido", True),
    ("previsão de entrega", "qual a previsão de entrega?", True),
    ("rastreio", "me manda o código de rastreio", True),
    ("já foi enviado", "já foi enviado o meu pedido?", True),
    ("mensagem de compra nova, não pós-venda", "quero comprar uma persiana", False),
    ("saudação normal", "oi, bom dia", False),
    ("vazio", "", False),
]

# ── is_scheduling_request: (nome, texto, esperado) ───────────────────────────
CASOS_SCHEDULING = [
    ("pedido direto de agendar", "quero agendar uma visita", True),
    ("marcar horário", "posso marcar um horário pra instalação?", True),
    ("disponibilidade", "vocês têm disponibilidade essa semana?", True),
    ("mensagem de compra, não agendamento", "quero comprar uma persiana", False),
    ("saudação normal", "oi, tudo bem?", False),
    ("vazio", "", False),
]

# ── _has_datetime_hint: (nome, texto, esperado) ──────────────────────────────
CASOS_DATETIME_HINT = [
    ("dia da semana", "pode ser quinta de manhã?", True),
    ("amanhã", "dá pra ser amanhã à tarde?", True),
    ("hora com h", "às 14h fica bom", True),
    ("hora com dois pontos", "podia ser 9:30", True),
    ("data numérica", "que tal dia 20/08", True),
    ("sem nenhuma referência de data/hora", "quero agendar uma visita", False),
    ("vazio", "", False),
]

# ── _infer_appointment_tipo: (nome, texto, esperado) ─────────────────────────
CASOS_TIPO = [
    ("instalação", "quero marcar a instalação pra quinta", "instalação"),
    ("visita técnica", "posso agendar uma visita técnica?", "visita técnica"),
    ("demonstração", "queria ver uma demonstração do produto", "demonstração"),
    ("genérico sem palavra-chave", "quero marcar um horário", "atendimento"),
]


def rodar_casos(nome_grupo, funcao, casos, aridade=2):
    total = len(casos)
    falhas = []
    for caso in casos:
        if aridade == 2:
            nome, texto, esperado = caso
            resultado = funcao(texto)
        else:
            nome, texto, esperado = caso
            resultado = funcao(texto)
        if resultado != esperado:
            falhas.append((nome, texto, esperado, resultado))

    print(f"\n{nome_grupo}: {total - len(falhas)}/{total} passaram")
    for nome, texto, esperado, resultado in falhas:
        print(f"  ❌ {nome!r} — texto={texto!r} esperado={esperado!r} obtido={resultado!r}")
    return len(falhas) == 0


def main():
    ok = True
    ok &= rodar_casos("is_order_status_request", is_order_status_request, CASOS_ORDER_STATUS)
    ok &= rodar_casos("is_scheduling_request", is_scheduling_request, CASOS_SCHEDULING)
    ok &= rodar_casos("_has_datetime_hint", _has_datetime_hint, CASOS_DATETIME_HINT)
    ok &= rodar_casos("_infer_appointment_tipo", _infer_appointment_tipo, CASOS_TIPO)

    print("\n" + ("✅ TUDO OK" if ok else "❌ ALGUM TESTE FALHOU"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
