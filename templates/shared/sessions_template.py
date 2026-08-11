"""
sessions_template.py — Gerencia SQLite de sessões e conversas

Usa SQLite para persistência local segura. Sessões expiram após 30 min.
"""

import sqlite3
import json
import time
from pathlib import Path
from datetime import datetime

import client_config
DB_PATH = str(client_config.DB_PATH)  # banco isolado por cliente


def _db():
    """Context manager para conexão SQLite."""
    db_file = Path(DB_PATH).expanduser()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Cria tabelas se não existem."""
    conn = _db()
    cursor = conn.cursor()

    # Tabela de sessões (conversas ativas)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            messages_json TEXT NOT NULL,
            last_activity INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Tabela de leads (CRM)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,
            name TEXT,
            phone TEXT UNIQUE,
            email TEXT,
            source TEXT,
            first_msg TEXT,
            sent_checkout INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # Tabela de mensagens (histórico completo)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts INTEGER NOT NULL,
            FOREIGN KEY(lead_id) REFERENCES leads(id)
        )
    """)

    # Tabela de metadados da sessão (largura, altura, cep, etc.)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_metadata (
            session_id TEXT,
            key TEXT,
            value TEXT,
            PRIMARY KEY (session_id, key)
        )
    """)

    # Tabela de pedidos (módulo pós-venda) — um lead pode ter mais de um
    # pedido ao longo do tempo (compras diferentes), por isso é tabela
    # própria em vez de metadata. status é texto livre (não enum), pra não
    # travar em uma lista fixa que pode não bater com o vocabulário de todo
    # negócio ("em preparo" pra um, "em produção" pra outro).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT NOT NULL,
            phone TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pago',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(lead_id) REFERENCES leads(id)
        )
    """)

    # Tabela de agendamentos (módulo agendamento) — igual pedidos, um lead
    # pode agendar mais de uma vez. data_hora fica como texto livre (o mesmo
    # raciocínio de endereco em agent_template.py: texto de data/hora em
    # chat natural é irregular demais pra estruturar sem um parser de datas
    # dedicado) — quem confirma o horário real é o dono, manualmente.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT NOT NULL,
            phone TEXT NOT NULL,
            tipo TEXT,
            data_hora_texto TEXT,
            status TEXT NOT NULL DEFAULT 'pendente',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(lead_id) REFERENCES leads(id)
        )
    """)

    conn.commit()
    conn.close()


def load_session(session_id: str):
    """Carrega sessão ativa ou retorna None se expirada."""
    conn = _db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT messages_json, last_activity FROM sessions WHERE id = ?",
        (session_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    # Verificar expiração (30 min = 1800 segundos)
    if time.time() - row["last_activity"] > 1800:
        return None

    return json.loads(row["messages_json"])


def save_session(session_id: str, messages: list):
    """Salva ou atualiza sessão."""
    conn = _db()
    cursor = conn.cursor()
    now = int(time.time())

    cursor.execute(
        """
        INSERT INTO sessions (id, messages_json, last_activity, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            messages_json = ?,
            last_activity = ?
        """,
        (
            session_id,
            json.dumps(messages),
            now,
            datetime.now().isoformat(),
            json.dumps(messages),
            now
        )
    )
    conn.commit()
    conn.close()


def delete_session(session_id: str):
    """Deleta sessão expirada."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()


def cleanup_expired_sessions():
    """Remove todas as sessões expiradas."""
    conn = _db()
    cursor = conn.cursor()
    now = int(time.time())

    cursor.execute(
        "DELETE FROM sessions WHERE ? - last_activity > 1800",
        (now,)
    )
    conn.commit()
    conn.close()


def create_lead(phone: str, name: str = None, email: str = None, source: str = "whatsapp"):
    """Cria ou atualiza lead no CRM."""
    conn = _db()
    cursor = conn.cursor()
    now = datetime.now().isoformat()

    lead_id = f"{source}_{phone}"

    cursor.execute(
        """
        INSERT INTO leads (id, phone, name, email, source, first_msg, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = COALESCE(?, name),
            email = COALESCE(?, email),
            updated_at = ?
        """,
        (
            lead_id, phone, name, email, source, "", now, now,
            name, email, now
        )
    )
    conn.commit()
    conn.close()

    return lead_id


def add_message(lead_id: str, role: str, content: str):
    """Adiciona mensagem ao histórico."""
    conn = _db()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO messages (lead_id, role, content, ts) VALUES (?, ?, ?, ?)",
        (lead_id, role, content, int(time.time()))
    )
    conn.commit()
    conn.close()


def mark_checkout_sent(lead_id: str):
    """Marca que checkout foi enviado para o lead."""
    conn = _db()
    cursor = conn.cursor()

    cursor.execute(
        "UPDATE leads SET sent_checkout = 1, updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), lead_id)
    )
    conn.commit()
    conn.close()


def get_lead_history(lead_id: str, limit: int = 50):
    """Retorna histórico de conversas do lead."""
    conn = _db()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT role, content, ts FROM messages WHERE lead_id = ? ORDER BY ts DESC LIMIT ?",
        (lead_id, limit)
    )
    messages = cursor.fetchall()
    conn.close()

    return [
        {"role": m["role"], "content": m["content"], "ts": m["ts"]}
        for m in messages
    ]


def get_leads_with_checkout() -> list:
    """Retorna leads que já receberam link de checkout (para acompanhamento de pagamento)."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, phone, name FROM leads WHERE sent_checkout = 1")
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r["id"], "phone": r["phone"], "name": r["name"]} for r in rows]


def get_leads_sem_checkout() -> list:
    """Retorna leads que NUNCA chegaram a receber um link de checkout, junto
    com o timestamp da última mensagem de cada um -- usado pra reengajar quem
    esfriou no meio do funil (deu medida, cor etc. e sumiu antes do
    orçamento), diferente de get_leads_with_checkout() que é sobre cobrança
    de quem já tem link mas não pagou."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT leads.id AS id, leads.phone AS phone, leads.name AS name,
               MAX(messages.ts) AS last_ts
        FROM leads
        JOIN messages ON messages.lead_id = leads.id
        WHERE leads.sent_checkout = 0
        GROUP BY leads.id
        """
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": r["id"], "phone": r["phone"], "name": r["name"], "last_ts": r["last_ts"]}
        for r in rows
    ]


def get_stats():
    """Retorna estatísticas do CRM."""
    conn = _db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as total FROM leads")
    total_leads = cursor.fetchone()["total"]

    cursor.execute("SELECT COUNT(*) as sent FROM leads WHERE sent_checkout = 1")
    checkout_sent = cursor.fetchone()["sent"]

    cursor.execute(
        "SELECT COUNT(*) as today FROM leads WHERE created_at > datetime('now', '-1 day')"
    )
    leads_today = cursor.fetchone()["today"]

    conn.close()

    return {
        "total_leads": total_leads,
        "checkout_sent": checkout_sent,
        "leads_today": leads_today,
        "conversion_rate": f"{(checkout_sent / total_leads * 100):.1f}%" if total_leads > 0 else "0%"
    }


def save_metadata(session_id: str, key: str, value: str):
    """Salva um metadado para a sessão (ex: largura, altura, cep)."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO session_metadata (session_id, key, value)
        VALUES (?, ?, ?)
        ON CONFLICT(session_id, key) DO UPDATE SET value = ?
        """,
        (session_id, key, str(value), str(value))
    )
    conn.commit()
    conn.close()


def get_metadata(session_id: str, key: str, default=None):
    """Recupera um metadado da sessão."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT value FROM session_metadata WHERE session_id = ? AND key = ?",
        (session_id, key)
    )
    row = cursor.fetchone()
    conn.close()
    return row["value"] if row else default


# ── Pedidos (módulo pós-venda) ───────────────────────────────────────────────

def create_order(lead_id: str, phone: str, description: str = "", status: str = "pago") -> int:
    """Cria um pedido novo pro lead (ex: ao confirmar pagamento). Retorna o id."""
    conn = _db()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute(
        """
        INSERT INTO orders (lead_id, phone, description, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (lead_id, phone, description, status, now, now)
    )
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id


def update_order_status(order_id: int, status: str):
    """Atualiza o status de um pedido específico pelo id."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
        (status, datetime.now().isoformat(), order_id)
    )
    conn.commit()
    conn.close()


def update_order_status_by_lead(lead_id: str, status: str):
    """Atualiza o status do pedido MAIS RECENTE de um lead — usado pelo
    comando do dono via WhatsApp ("pedido NUMERO status"), que não sabe o id
    interno do pedido, só o telefone do cliente."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM orders WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1",
        (lead_id,)
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    order_id = row["id"]
    cursor.execute(
        "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
        (status, datetime.now().isoformat(), order_id)
    )
    conn.commit()
    conn.close()
    return order_id


def get_latest_order(lead_id: str):
    """Retorna o pedido mais recente do lead, ou None se ele nunca comprou."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, description, status, created_at, updated_at FROM orders "
        "WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1",
        (lead_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def get_orders_by_lead(lead_id: str) -> list:
    """Retorna todos os pedidos do lead, do mais recente pro mais antigo."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, description, status, created_at, updated_at FROM orders "
        "WHERE lead_id = ? ORDER BY created_at DESC",
        (lead_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Agendamentos (módulo agendamento) ────────────────────────────────────────

def create_appointment(lead_id: str, phone: str, tipo: str, data_hora_texto: str, status: str = "pendente") -> int:
    """Cria um pedido de agendamento (ainda não confirmado por um humano). Retorna o id."""
    conn = _db()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute(
        """
        INSERT INTO appointments (lead_id, phone, tipo, data_hora_texto, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (lead_id, phone, tipo, data_hora_texto, status, now, now)
    )
    appointment_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return appointment_id


def update_appointment_status(appointment_id: int, status: str):
    """Atualiza o status de um agendamento específico pelo id."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE appointments SET status = ?, updated_at = ? WHERE id = ?",
        (status, datetime.now().isoformat(), appointment_id)
    )
    conn.commit()
    conn.close()


def get_latest_appointment(lead_id: str):
    """Retorna o agendamento mais recente do lead, ou None."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, tipo, data_hora_texto, status, created_at, updated_at FROM appointments "
        "WHERE lead_id = ? ORDER BY created_at DESC LIMIT 1",
        (lead_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def get_pending_appointment_by_lead(lead_id: str):
    """Retorna o agendamento PENDENTE mais recente do lead (aguardando
    confirmação do dono), ou None se não houver nenhum em aberto — evita
    criar um segundo pedido de agendamento duplicado enquanto o primeiro
    ainda não foi confirmado/recusado."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, tipo, data_hora_texto, status, created_at, updated_at FROM appointments "
        "WHERE lead_id = ? AND status = 'pendente' ORDER BY created_at DESC LIMIT 1",
        (lead_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def update_appointment_status_by_lead(lead_id: str, status: str):
    """Atualiza o status do agendamento PENDENTE mais recente de um lead —
    usado pelo comando do dono via WhatsApp ("confirmar agendamento NUMERO")."""
    conn = _db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM appointments WHERE lead_id = ? AND status = 'pendente' "
        "ORDER BY created_at DESC LIMIT 1",
        (lead_id,)
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    appointment_id = row["id"]
    cursor.execute(
        "UPDATE appointments SET status = ?, updated_at = ? WHERE id = ?",
        (status, datetime.now().isoformat(), appointment_id)
    )
    conn.commit()
    conn.close()
    return appointment_id
