#!/usr/bin/env python3
"""Teste de integração de ponta a ponta pros 5 módulos novos (pós-venda,
agendamento, FAQ, NPS, indicação) -- roda as funções REAIS de produção
(handle_message, handle_owner_command, process_payment_followups) contra um
banco SQLite temporário, só mockando as bordas externas (Evolution API,
Asaas, chamada de IA). Nenhuma mensagem real de WhatsApp, nenhum real de
verdade gasto.
"""
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
SHARED_DIR = REPO_ROOT / "templates" / "shared"
WHATSAPP_DIR = REPO_ROOT / "templates" / "whatsapp"

# ── 1. Ambiente temporário isolado (nunca toca no banco/config reais) ───────
TMP_ROOT = Path(tempfile.mkdtemp(prefix="agente_integration_test_"))
CLIENT_NAME = "teste-integracao"
os.environ["AGENTE_CLIENTES_DIR"] = str(TMP_ROOT)
os.environ["AGENTE_CLIENTE"] = CLIENT_NAME
CLIENT_DIR = TMP_ROOT / CLIENT_NAME
CLIENT_DIR.mkdir(parents=True, exist_ok=True)

OWNER_PHONE = "5599999999"
FAKE_CONFIG = {
    "ai_provider": "anthropic",
    "ai_model": "claude-fake-model",
    "ai_api_key": "fake-key",
    "system_prompt": "Você é um vendedor de persianas sob medida.",
    "evolution_api_key": "fake-evolution-key",
    "instance_name": "teste-instancia",
    "owner_phone": OWNER_PHONE,
    "checkout_link": "https://exemplo.com/checkout",
    "product_name": "Persianas Teste",
    "referral_benefit_text": "15% de desconto na próxima compra",
    # de propósito SEM asaas_api_key -- garante que nenhuma chamada real
    # pra Asaas de produção seria feita mesmo se algum branch inesperado
    # tentasse gerar um link de pagamento de verdade.
}
(CLIENT_DIR / "config.json").write_text(json.dumps(FAKE_CONFIG), encoding="utf-8")

# FAQ precisa existir ANTES de importar agent_core_template, porque o
# carregamento acontece em tempo de importação (ver seção 42 do SKILL.md).
FAQ_ITEMS = [
    {"pergunta": "Vocês entregam em todo o Brasil?", "resposta": "Sim, entregamos pra todo o Brasil via transportadora."},
]
(CLIENT_DIR / "faq.json").write_text(json.dumps(FAQ_ITEMS, ensure_ascii=False), encoding="utf-8")

sys.path.insert(0, str(SHARED_DIR))
sys.path.insert(0, str(WHATSAPP_DIR))

# Os arquivos reais chamam "import client_config" (nome que só existe depois
# do deploy renomear client_config_template.py) -- registra o alias antes de
# qualquer outro import, igual o deploy real faz em disco.
import client_config_template as client_config
sys.modules["client_config"] = client_config

import sessions_template as sessions
sessions.init_db()

import agent_core_template
import agent_template

# watcher_template.py importa "from agent import ..." e faz "import sessions"
# dentro das funções -- em produção esses arquivos são renomeados no deploy
# (agent_template.py -> agent.py, sessions_template.py -> sessions.py, ver
# deploy/deploy_vps.py). Pra rodar o arquivo de template direto sem renomear
# nada em disco, registramos os aliases que o deploy real cria.
sys.modules["agent"] = agent_template
sys.modules["sessions"] = sessions

import watcher_template as watcher

# ── 2. Mocks das bordas externas ─────────────────────────────────────────────
sent_messages = []  # lista de (phone, texto) -- tudo que "seria enviado" no WhatsApp


def fake_send_whatsapp(phone, message):
    sent_messages.append((phone, message))
    return True


watcher.send_whatsapp = fake_send_whatsapp

asaas_status = {"value": "PENDING"}


def fake_check_asaas(payment_link_id):
    return asaas_status["value"]


watcher.check_asaas_payment_status = fake_check_asaas

# Evita a busca de preço AO VIVO no site real da Fácil Persianas (rede
# externa) -- cai pro fallback de tabela estática, suficiente pra este teste
# (não estamos testando precificação aqui, isso já é validado em produção).
agent_template.buscar_preco_ao_vivo = lambda *a, **k: None

ai_calls = []  # captura as mensagens que a IA "real" teria recebido


def fake_call_ai(messages, max_tokens=4096):
    ai_calls.append(messages)
    return "Combinado! Muito obrigado. 😊"


agent_template.call_ai = fake_call_ai

# ── 3. Cenários ───────────────────────────────────────────────────────────
resultados = []


def check(nome, condicao, detalhe=""):
    resultados.append((nome, bool(condicao), detalhe))
    marca = "✅" if condicao else "❌"
    print(f"{marca} {nome}" + (f" -- {detalhe}" if detalhe and not condicao else ""))


print("\n=== Cenário A: pagamento confirmado cria pedido + oferece indicação ===")
maria_phone = "5599990001"
maria_lead_id = sessions.create_lead(maria_phone, name="Maria")
sessions.save_metadata(maria_lead_id, "width", "1.20")
sessions.save_metadata(maria_lead_id, "height", "1.50")
sessions.save_metadata(maria_lead_id, "checkout_id", "chk_teste_1")
sessions.save_metadata(maria_lead_id, "checkout_sent_at", str(int(time.time()) - 100))
sessions.mark_checkout_sent(maria_lead_id)

asaas_status["value"] = "PAID"
sent_messages.clear()
watcher.process_payment_followups()

pedido_maria = sessions.get_latest_order(maria_lead_id)
check("pedido criado automaticamente ao confirmar pagamento", pedido_maria is not None)
check("status inicial do pedido é 'em preparo'", pedido_maria and pedido_maria["status"] == "em preparo")
check("cliente recebeu confirmação de pagamento", any(maria_phone == p and "Confirmamos" in m for p, m in sent_messages))
check("cliente recebeu oferta de indicação citando o próprio número", any(maria_phone == p and maria_phone in m and "indicado" in m for p, m in sent_messages))

print("\n=== Cenário B: dono marca 'entregue' -> NPS dispara ===")
sent_messages.clear()
watcher.handle_owner_command(OWNER_PHONE, f"pedido {maria_phone} entregue")

pedido_maria = sessions.get_latest_order(maria_lead_id)
nps_pendente = sessions.get_pending_nps(maria_lead_id)
check("status do pedido virou 'entregue'", pedido_maria and pedido_maria["status"] == "entregue")
check("pesquisa NPS foi criada", nps_pendente is not None)
check("cliente recebeu aviso de status + pergunta de nota", len([m for p, m in sent_messages if p == maria_phone]) == 2)

print("\n=== Cenário C: cliente responde a pesquisa NPS ===")
ai_calls.clear()
resposta, media, forcar_texto, owner_notifs = agent_template.handle_message(maria_phone, "Maria", "9, adorei o atendimento!")

nps_pendente_depois = sessions.get_pending_nps(maria_lead_id)
ultima_msg_ia = ai_calls[-1] if ai_calls else []
tem_injecao_nps = any("nota 9" in (m.get("content") or "") for m in ultima_msg_ia if m.get("role") == "system")
check("NPS deixou de estar pendente após resposta", nps_pendente_depois is None)
check("nota 9 foi injetada no contexto da IA corretamente", tem_injecao_nps)
check("IA respondeu normalmente (resposta não vazia)", bool(resposta))

print("\n=== Cenário D: novo lead indicado por Maria -> compra -> conversão ===")
joao_phone = "5599990002"
ai_calls.clear()
agent_template.handle_message(joao_phone, "João", f"oi, fui indicado por {maria_phone}, quero uma persiana de 1x1.5")
joao_lead_id = f"whatsapp_{joao_phone}"

referral = sessions.get_referral_by_referred_phone(joao_phone)
check("indicação registrada (João indicado por Maria)", referral is not None and referral["referrer_phone"] == maria_phone)

sessions.save_metadata(joao_lead_id, "checkout_id", "chk_teste_2")
sessions.save_metadata(joao_lead_id, "checkout_sent_at", str(int(time.time()) - 50))
sessions.mark_checkout_sent(joao_lead_id)
sent_messages.clear()
asaas_status["value"] = "PAID"
watcher.process_payment_followups()

referral_depois = sessions.get_referral_by_referred_phone(joao_phone)  # None esperado -- só retorna pendente
check("indicação convertida após pagamento de João", referral_depois is None)
check("Maria (quem indicou) foi avisada da conversão", any(maria_phone == p and "indicou" in m and "15%" in m for p, m in sent_messages))

print("\n=== Cenário E: pedido de agendamento -> confirmação do dono ===")
carlos_phone = "5599990003"
ai_calls.clear()
resposta, media, forcar_texto, owner_notifs = agent_template.handle_message(
    carlos_phone, "Carlos", "quero marcar uma visita técnica quinta de manhã, pode ser?"
)
carlos_lead_id = f"whatsapp_{carlos_phone}"

pendente_agendamento = sessions.get_pending_appointment_by_lead(carlos_lead_id)
check("agendamento pendente foi criado", pendente_agendamento is not None)
check("tipo inferido corretamente como 'visita técnica'", pendente_agendamento and pendente_agendamento["tipo"] == "visita técnica")
check("dono foi notificado sobre o pedido de agendamento", len(owner_notifs) == 1 and carlos_phone in owner_notifs[0])

sent_messages.clear()
watcher.handle_owner_command(OWNER_PHONE, f"confirmar agendamento {carlos_phone}")
pendente_depois = sessions.get_pending_appointment_by_lead(carlos_lead_id)
check("agendamento deixou de estar pendente após confirmação", pendente_depois is None)
check("cliente foi avisado da confirmação", any(carlos_phone == p and "confirmado" in m.lower() for p, m in sent_messages))

print("\n=== Cenário F: FAQ injetado no prompt da IA ===")
check("pergunta do faq.json está no SYSTEM_PROMPT", "entregam em todo o Brasil" in agent_core_template.SYSTEM_PROMPT)
check("instrução de não inventar está no bloco de FAQ", "nunca invente" in agent_core_template.SYSTEM_PROMPT.lower())

# ── 4. Resumo ─────────────────────────────────────────────────────────────
total = len(resultados)
passou = sum(1 for _, ok, _ in resultados if ok)
print(f"\n{'=' * 60}")
print(f"RESULTADO: {passou}/{total} verificações passaram")
if passou < total:
    print("\nFalhas:")
    for nome, ok, detalhe in resultados:
        if not ok:
            print(f"  ❌ {nome}" + (f" -- {detalhe}" if detalhe else ""))
print(f"{'=' * 60}")

sys.exit(0 if passou == total else 1)
