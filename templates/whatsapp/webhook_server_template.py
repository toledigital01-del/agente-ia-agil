#!/usr/bin/env python3
"""
webhook_server.py — Servidor webhook para Meta WhatsApp Cloud API

Substitui watcher.py quando usando a API oficial da Meta em vez
da Evolution API. A Meta chama este servidor a cada mensagem recebida —
sem polling, sem risco de ban por IP de datacenter.

Execução:
  python3 webhook_server.py
"""

import json
import re
import time
import logging
import sys
import threading
import traceback
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from pathlib import Path

try:
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import PlainTextResponse
    import uvicorn
except ImportError:
    print("❌ Dependências ausentes. Execute: pip install fastapi uvicorn")
    sys.exit(1)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent))
import client_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(client_config.LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

from agent import handle_message, is_handoff_request, is_frustration_keyword, is_repeated_question

FRUSTRATION_THRESHOLD = 2  # ver seção 39 do SKILL.md

# ── Configuração Meta Cloud API ────────────────────────────────────────────────

META_ACCESS_TOKEN   = client_config.require("meta_access_token")
META_PHONE_ID       = client_config.require("meta_phone_number_id")
META_VERIFY_TOKEN   = client_config.require("meta_verify_token")
OWNER_PHONE         = client_config.get("owner_phone", "")
META_BASE           = "https://graph.facebook.com/v20.0"


# ── Meta Graph API ─────────────────────────────────────────────────────────────

def _meta_request(endpoint: str, method: str = "GET", data: dict = None,
                  raw_body: bytes = None, content_type: str = "application/json") -> dict:
    url = f"{META_BASE}{endpoint}"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}

    if raw_body is not None:
        headers["Content-Type"] = content_type
        body = raw_body
    elif data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    else:
        body = None

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.error(f"Meta API {e.code}: {e.read().decode(errors='replace')}")
        return {}
    except Exception as e:
        logger.error(f"Meta API erro: {e}")
        return {}


def send_whatsapp(phone: str, message: str) -> bool:
    result = _meta_request(
        f"/{META_PHONE_ID}/messages",
        method="POST",
        data={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": phone,
            "type": "text",
            "text": {"body": message, "preview_url": False},
        },
    )
    success = bool(result.get("messages"))
    if success:
        logger.info(f"📤 Enviado para {phone}")
    else:
        logger.error(f"❌ Falha ao enviar para {phone}: {result}")
    return success


def _download_media(media_id: str) -> bytes:
    info = _meta_request(f"/{media_id}")
    url = info.get("url")
    if not url:
        return None
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {META_ACCESS_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        logger.error(f"Erro ao baixar mídia {media_id}: {e}")
        return None


def _upload_media(audio_bytes: bytes, mime_type: str = "audio/mpeg") -> str:
    boundary = "----WKFBoundary7MA4YWxk"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="messaging_product"\r\n\r\nwhatsapp\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="audio.mp3"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + audio_bytes + f"\r\n--{boundary}--\r\n".encode()

    result = _meta_request(
        f"/{META_PHONE_ID}/media",
        method="POST",
        raw_body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    return result.get("id")


def send_whatsapp_audio(phone: str, text: str) -> bool:
    try:
        p_conf = Path.home() / ".meu-agente" / "config.json"
        if not p_conf.exists():
            return False
        cfg = json.loads(p_conf.read_text(encoding="utf-8"))
        eleven_key = cfg.get("elevenlabs_api_key", "")
        voice_id = cfg.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")
        if not eleven_key:
            return False

        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            data=json.dumps({
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.85, "similarity_boost": 0.85},
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "xi-api-key": eleven_key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=40) as r:
            audio_bytes = r.read()

        media_id = _upload_media(audio_bytes)
        if not media_id:
            return False

        result = _meta_request(
            f"/{META_PHONE_ID}/messages",
            method="POST",
            data={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone,
                "type": "audio",
                "audio": {"id": media_id},
            },
        )
        success = bool(result.get("messages"))
        if success:
            logger.info(f"📤 Áudio enviado para {phone}")
        return success
    except Exception as e:
        logger.error(f"Erro ao enviar áudio para {phone}: {e}")
        return False


def _transcribe_audio(media_id: str) -> str:
    audio_bytes = _download_media(media_id)
    if not audio_bytes:
        return ""
    try:
        p_conf = Path.home() / ".meu-agente" / "config.json"
        cfg = json.loads(p_conf.read_text(encoding="utf-8")) if p_conf.exists() else {}
        groq_key = cfg.get("groq_api_key", "")
        openai_key = cfg.get("openai_api_key", "")
        if not groq_key and not openai_key and cfg.get("ai_provider") == "openai":
            openai_key = cfg.get("ai_api_key", "")

        if groq_key:
            api_url, api_key, model = "https://api.groq.com/openai/v1/audio/transcriptions", groq_key, "whisper-large-v3"
        elif openai_key:
            api_url, api_key, model = "https://api.openai.com/v1/audio/transcriptions", openai_key, "whisper-1"
        else:
            logger.warning("Sem chave Whisper para transcrição.")
            return ""

        boundary = "----WKFBoundaryAudio"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.ogg\"\r\n"
            f"Content-Type: audio/ogg\r\n\r\n"
        ).encode() + audio_bytes + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            api_url,
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("text", "")
    except Exception as e:
        logger.error(f"Erro na transcrição: {e}")
        return ""


# ── Handoff humano e comandos do dono ─────────────────────────────────────────

def handle_owner_command(phone: str, text: str) -> bool:
    if not OWNER_PHONE or phone != OWNER_PHONE:
        return False
    import sessions
    text_lower = text.strip().lower()

    if text_lower.startswith("reativar "):
        target = re.sub(r"\D", "", text_lower.replace("reativar ", "", 1))
        if not target:
            send_whatsapp(OWNER_PHONE, "⚠️ Formato: reativar 5585999999999")
            return True
        sessions.save_metadata(f"whatsapp_{target}", "human_handoff", "0")
        logger.info(f"🤖 IA reativada para {target}.")
        send_whatsapp(OWNER_PHONE, f"✅ IA reativada para {target}.")
        return True

    if text_lower.startswith("pausar "):
        target = re.sub(r"\D", "", text_lower.replace("pausar ", "", 1))
        if not target:
            send_whatsapp(OWNER_PHONE, "⚠️ Formato: pausar 5585999999999")
            return True
        sessions.save_metadata(f"whatsapp_{target}", "human_handoff", "1")
        sessions.save_metadata(f"whatsapp_{target}", "human_handoff_at", str(int(time.time())))
        logger.info(f"🙋 IA pausada para {target}.")
        send_whatsapp(OWNER_PHONE, f"✅ IA pausada para {target}.")
        return True

    # Pós-venda / agendamento — mesmos comandos de watcher_template.py
    # (mantidos em sincronia mesmo esse transporte não estando em produção
    # ainda, ver seção 22 do SKILL.md pro mesmo princípio já aplicado a mídia).
    if text_lower.startswith("pedido "):
        m = re.match(r"^pedido\s+(\d+)\s+(.+)$", text.strip(), re.IGNORECASE)
        if not m:
            send_whatsapp(OWNER_PHONE, "⚠️ Formato: pedido 5585999999999 enviado")
            return True
        target, novo_status = m.group(1), m.group(2).strip()
        order_id = sessions.update_order_status_by_lead(f"whatsapp_{target}", novo_status)
        if order_id is None:
            send_whatsapp(OWNER_PHONE, f"⚠️ Não encontrei nenhum pedido registrado pra {target}.")
            return True
        logger.info(f"📦 Pedido de {target} atualizado pra \"{novo_status}\".")
        send_whatsapp(OWNER_PHONE, f"✅ Pedido de {target} atualizado pra \"{novo_status}\".")
        send_whatsapp(target, f"Oi! Passando pra te avisar: seu pedido está \"{novo_status}\". 📦")
        if "entreg" in novo_status.lower():
            sessions.create_nps_request(f"whatsapp_{target}", target, context=f"pedido #{order_id}")
            send_whatsapp(
                target,
                "Antes de eu deixar você em paz 😄 — de 0 a 10, o quanto você recomendaria "
                "a gente pra um amigo? Pode responder só a nota, e se quiser comentar, fica à vontade!"
            )
        return True

    if text_lower.startswith("confirmar agendamento "):
        target = re.sub(r"\D", "", text_lower.replace("confirmar agendamento ", "", 1))
        if not target:
            send_whatsapp(OWNER_PHONE, "⚠️ Formato: confirmar agendamento 5585999999999")
            return True
        appt = sessions.get_pending_appointment_by_lead(f"whatsapp_{target}")
        appointment_id = sessions.update_appointment_status_by_lead(f"whatsapp_{target}", "confirmado")
        if appointment_id is None:
            send_whatsapp(OWNER_PHONE, f"⚠️ Não encontrei agendamento pendente pra {target}.")
            return True
        detalhe = f" ({appt['data_hora_texto']})" if appt else ""
        send_whatsapp(OWNER_PHONE, f"✅ Agendamento de {target} confirmado{detalhe}.")
        send_whatsapp(target, f"Oi! Seu agendamento foi confirmado{detalhe}. Te esperamos! 📅")
        return True

    if text_lower.startswith("cancelar agendamento "):
        target = re.sub(r"\D", "", text_lower.replace("cancelar agendamento ", "", 1))
        if not target:
            send_whatsapp(OWNER_PHONE, "⚠️ Formato: cancelar agendamento 5585999999999")
            return True
        appointment_id = sessions.update_appointment_status_by_lead(f"whatsapp_{target}", "cancelado")
        if appointment_id is None:
            send_whatsapp(OWNER_PHONE, f"⚠️ Não encontrei agendamento pendente pra {target}.")
            return True
        send_whatsapp(OWNER_PHONE, f"✅ Agendamento de {target} cancelado.")
        send_whatsapp(
            target,
            "Oi! Sobre o horário que você pediu pra agendar: esse horário específico não vai dar certo. "
            "Pode me passar outra data/horário que a gente vê a disponibilidade? 🙏"
        )
        return True

    return False


def check_human_handoff(phone: str, name: str, text: str) -> bool:
    import sessions
    lead_id = sessions.create_lead(phone, name=name)

    if sessions.get_metadata(lead_id, "human_handoff", "0") == "1":
        logger.info(f"🙋 {name} ({phone}) em atendimento humano — ignorando.")
        return True

    if is_handoff_request(text):
        sessions.save_metadata(lead_id, "human_handoff", "1")
        sessions.save_metadata(lead_id, "human_handoff_at", str(int(time.time())))
        send_whatsapp(phone, "Claro! Já vou te conectar com um atendente. 🙋")
        if OWNER_PHONE:
            send_whatsapp(OWNER_PHONE,
                f"🔔 {name} ({phone}) pediu atendimento humano.\n"
                f"Mensagem: \"{text}\"\n\nA IA foi pausada.")
        return True

    # Toldo NÃO aciona mais handoff humano (removido 2026-08-10) -- tem preço
    # médio de referência no system_prompt e a IA responde direto.

    # Cliente travado/frustrado (added 2026-08-10, ver seção 39 do SKILL.md
    # -- mesma lógica de watcher.py, mantida em sincronia aqui).
    historico = sessions.get_lead_history(lead_id, limit=8)
    mensagens_anteriores = [h["content"] for h in historico if h["role"] == "user"]
    sinal_repeticao = is_repeated_question(text, mensagens_anteriores)
    sinal_palavra = is_frustration_keyword(text)

    if sinal_repeticao or sinal_palavra:
        contador = int(sessions.get_metadata(lead_id, "frustration_signal_count", "0") or "0") + 1
        sessions.save_metadata(lead_id, "frustration_signal_count", str(contador))
        if contador >= FRUSTRATION_THRESHOLD:
            sessions.save_metadata(lead_id, "human_handoff", "1")
            sessions.save_metadata(lead_id, "human_handoff_at", str(int(time.time())))
            sessions.save_metadata(lead_id, "frustration_signal_count", "0")
            send_whatsapp(
                phone,
                "Percebi que talvez eu não tenha esclarecido direito sua dúvida — vou chamar um de nossos "
                "consultores pra te ajudar com mais atenção, só um instante! 🙋"
            )
            if OWNER_PHONE:
                ultimas = "\n".join(f"- {m}" for m in mensagens_anteriores[:3]) or "(sem histórico anterior)"
                send_whatsapp(OWNER_PHONE,
                    f"🧐 {name} ({phone}) parece travado(a)/frustrado(a) na conversa.\n"
                    f"Mensagem atual: \"{text}\"\nMensagens recentes dele:\n{ultimas}\n\n"
                    "A IA foi pausada — considere assumir manualmente.")
            return True

    return False


# ── Followups de pagamento (Asaas) ────────────────────────────────────────────

def _check_asaas_payment(payment_link_id: str) -> str:
    try:
        token = client_config.get("asaas_api_key", "")
        if not token:
            return "NONE"
        req = urllib.request.Request(
            f"https://api.asaas.com/v3/payments?paymentLink={payment_link_id}",
            headers={"Content-Type": "application/json", "access_token": token},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read()).get("data", [])
            for p in data:
                if p.get("status") in ("RECEIVED", "CONFIRMED"):
                    return "PAID"
            return "PENDING" if data else "NONE"
    except Exception as e:
        logger.error(f"Erro Asaas ({payment_link_id}): {e}")
        return "NONE"


def _process_payment_followups():
    import sessions
    now_ts = int(time.time())
    for lead in sessions.get_leads_with_checkout():
        lead_id, phone, name = lead["id"], lead["phone"], lead["name"]
        checkout_id       = sessions.get_metadata(lead_id, "checkout_id")
        checkout_sent_str = sessions.get_metadata(lead_id, "checkout_sent_at")
        followup_status   = sessions.get_metadata(lead_id, "followup_status", "0")

        if not checkout_id or not checkout_sent_str or followup_status == "PAID":
            continue
        if sessions.get_metadata(lead_id, "human_handoff", "0") == "1":
            continue

        if _check_asaas_payment(checkout_id) == "PAID":
            sessions.save_metadata(lead_id, "followup_status", "PAID")
            descricao = f"{sessions.get_metadata(lead_id, 'width', '?')}m x {sessions.get_metadata(lead_id, 'height', '?')}m"
            sessions.create_order(lead_id, phone, description=descricao, status="em preparo")
            logger.info(f"🎉 Pagamento confirmado — {name} ({phone})")
            send_whatsapp(phone,
                f"Oba, {name}! 🎉 Confirmamos seu pagamento. "
                "Seu pedido já foi encaminhado para fabricação!")

            try:
                referral = sessions.get_referral_by_referred_phone(phone)
                if referral:
                    sessions.mark_referral_converted(referral["id"])
                    beneficio = client_config.get("referral_benefit_text", "um desconto especial")
                    send_whatsapp(
                        referral["referrer_phone"],
                        f"🎉 Boa notícia! A pessoa que você indicou pra gente acabou de comprar. "
                        f"Você ganhou {beneficio} — é só chamar por aqui quando quiser usar!"
                    )
            except Exception as e:
                logger.error(f"Erro ao processar conversão de indicação para {phone}: {e}")

            beneficio = client_config.get("referral_benefit_text", "um desconto especial")
            send_whatsapp(
                phone,
                f"Ah, {name}, e uma coisa: se você indicar um amigo pra gente, é só ele "
                f"mencionar que foi indicado por {phone} quando chamar no WhatsApp — "
                f"vocês dois ganham {beneficio}! 🎁"
            )
            return

        elapsed = now_ts - int(checkout_sent_str)

        if elapsed >= 7200 and followup_status == "0":
            if send_whatsapp(phone,
                f"Olá, {name}! Vi que o link de checkout está pronto mas o pagamento "
                "ainda não foi confirmado. Ficou alguma dúvida? 😊"):
                sessions.save_metadata(lead_id, "followup_status", "1")
            return

        if elapsed >= 86400 and followup_status == "1":
            url = sessions.get_metadata(lead_id, "asaas_checkout_url", "")
            if send_whatsapp(phone,
                f"Olá, {name}! O lote de produção da nossa fábrica fecha hoje. "
                f"Garanta sua persiana finalizando agora: {url} 🚀"):
                sessions.save_metadata(lead_id, "followup_status", "2")
            return


# ── Background: manutenção periódica ──────────────────────────────────────────

def _maintenance_loop():
    import sessions
    while True:
        time.sleep(600)  # a cada 10 minutos
        try:
            _process_payment_followups()
        except Exception as e:
            logger.error(f"Erro nos followups: {e}\n{traceback.format_exc()}")
        try:
            sessions.cleanup_expired_sessions()
        except Exception as e:
            logger.error(f"Erro no cleanup: {e}")


# ── FastAPI ────────────────────────────────────────────────────────────────────

_seen_ids: set = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Webhook server iniciado (Meta Cloud API)")
    threading.Thread(target=_maintenance_loop, daemon=True).start()
    yield
    logger.info("⏹️  Webhook server encerrado")


app = FastAPI(lifespan=lifespan)


@app.get("/webhook")
async def verify_webhook(request: Request):
    """Meta chama este endpoint uma vez para verificar o servidor."""
    p = dict(request.query_params)
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == META_VERIFY_TOKEN:
        logger.info("✅ Webhook verificado pela Meta.")
        return PlainTextResponse(p.get("hub.challenge", ""))
    logger.warning("⚠️ Verificação com token inválido.")
    return Response(status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request):
    """Meta chama este endpoint a cada mensagem recebida."""
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=400)

    if data.get("object") != "whatsapp_business_account":
        return {"status": "ignored"}

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value    = change.get("value", {})
            messages = value.get("messages", [])
            contacts = value.get("contacts", [])

            name_map = {c["wa_id"]: c["profile"]["name"]
                        for c in contacts if "profile" in c}

            for msg in messages:
                msg_id = msg.get("id", "")
                if msg_id in _seen_ids:
                    continue
                _seen_ids.add(msg_id)
                if len(_seen_ids) > 2000:
                    for old in list(_seen_ids)[:1000]:
                        _seen_ids.discard(old)

                phone    = msg.get("from", "")
                name     = name_map.get(phone, "Lead")
                msg_type = msg.get("type", "")
                is_audio = msg_type == "audio"
                text     = ""

                if msg_type == "text":
                    text = msg.get("text", {}).get("body", "").strip()

                elif msg_type == "audio":
                    media_id = msg.get("audio", {}).get("id")
                    if media_id:
                        logger.info(f"🎤 {name} ({phone}) enviou áudio. Transcrevendo...")
                        text = _transcribe_audio(media_id)
                        if not text:
                            send_whatsapp(phone,
                                "Desculpe, não consegui entender o áudio. Pode digitar? 😊")
                            continue
                        logger.info(f"🎤 Transcrição: {text}")

                if not text:
                    continue

                # Processa em thread para não bloquear o webhook (Meta exige resposta < 20s)
                threading.Thread(
                    target=_process_message,
                    args=(phone, name, text, is_audio),
                    daemon=True,
                ).start()

    return {"status": "ok"}


def _process_message(phone: str, name: str, text: str, is_audio: bool):
    logger.info(f"📩 {name} ({phone}): {text[:60]}")

    if handle_owner_command(phone, text):
        return
    if check_human_handoff(phone, name, text):
        return

    try:
        response, media_requests, forcar_texto, owner_notifications = handle_message(phone, name, text)
        for aviso in owner_notifications:
            if OWNER_PHONE:
                send_whatsapp(OWNER_PHONE, aviso)
        if not response:
            return
        if is_audio and not forcar_texto:
            if not send_whatsapp_audio(phone, response):
                send_whatsapp(phone, response)
        else:
            send_whatsapp(phone, response)
        # TODO: envio de foto/vídeo (tags [FOTO:.]/[VIDEO:.]) ainda não
        # implementado no caminho Meta Cloud API — só no watcher.py (Evolution),
        # que é o que está em produção pra Ágil. Ver seção 22 do SKILL.md.
        if media_requests:
            logger.warning(f"⚠️  {len(media_requests)} mídia(s) pedida(s) pela IA, mas envio de mídia ainda não existe no webhook_server (Meta API) — ignorado.")
    except Exception as e:
        logger.error(f"Erro ao processar {phone}: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
