#!/usr/bin/env python3
"""Testes de regressão pra process_midfunnel_followups() (watcher_template.py)
-- reengajamento de lead que esfria ANTES de chegar no checkout.

Reproduz com SQLite em memória em vez de importar o módulo real (que
depende de client_config/agent, não disponíveis fora de uma implantação
real) -- replica a mesma lógica de get_leads_sem_checkout() e dos dois
estágios de reengajamento.

Caso mais importante aqui: o bug real encontrado em produção em
2026-08-10 (ver seção 38 do SKILL.md) -- o 2º toque usava a última
mensagem do CLIENTE como base do prazo de 48h, em vez de quando o 1º
toque foi enviado. Como a última mensagem do cliente não muda enquanto
ele fica em silêncio, um lead que já estava sumido há muito mais que 48h
quando o 1º toque saiu recebia o 2º toque minutos depois do 1º, em vez de
48h depois -- aconteceu de verdade com um lead real (Fernando,
555199980089), dois toques com 4 minutos de diferença.
"""
import sqlite3
import sys
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

HORA = 3600
MIDFUNNEL_FIRST_NUDGE_SECONDS = 4 * HORA
MIDFUNNEL_SECOND_NUDGE_SECONDS = 48 * HORA


class BancoFalso:
    """SQLite em memória com o mesmo schema relevante de sessions_template.py."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        c = self.conn.cursor()
        c.execute("CREATE TABLE leads (id TEXT PRIMARY KEY, name TEXT, phone TEXT, sent_checkout INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, lead_id TEXT, ts INTEGER)")
        c.execute("CREATE TABLE session_metadata (session_id TEXT, key TEXT, value TEXT, PRIMARY KEY(session_id, key))")
        self.conn.commit()

    def add_lead(self, lead_id, name, phone, last_ts, sent_checkout=0, **metadata):
        c = self.conn.cursor()
        c.execute("INSERT INTO leads VALUES (?,?,?,?)", (lead_id, name, phone, sent_checkout))
        c.execute("INSERT INTO messages (lead_id, ts) VALUES (?,?)", (lead_id, last_ts))
        for k, v in metadata.items():
            c.execute("INSERT INTO session_metadata VALUES (?,?,?)", (lead_id, k, str(v)))
        self.conn.commit()

    def get_metadata(self, session_id, key, default=None):
        c = self.conn.cursor()
        c.execute("SELECT value FROM session_metadata WHERE session_id=? AND key=?", (session_id, key))
        row = c.fetchone()
        return row["value"] if row else default

    def save_metadata(self, session_id, key, value):
        c = self.conn.cursor()
        c.execute(
            "INSERT INTO session_metadata VALUES (?,?,?) ON CONFLICT(session_id,key) DO UPDATE SET value=?",
            (session_id, key, str(value), str(value)),
        )
        self.conn.commit()

    def get_leads_sem_checkout(self):
        c = self.conn.cursor()
        c.execute(
            """SELECT leads.id AS id, leads.phone AS phone, leads.name AS name, MAX(messages.ts) AS last_ts
               FROM leads JOIN messages ON messages.lead_id = leads.id
               WHERE leads.sent_checkout = 0 GROUP BY leads.id"""
        )
        return [dict(r) for r in c.fetchall()]


def rodar_um_ciclo(banco: BancoFalso, now_ts: int):
    """Réplica de process_midfunnel_followups() -- mesmo anti-bloqueio (1
    envio por ciclo), devolve (estagio, lead_id) do que disparou, ou None."""
    for lead in banco.get_leads_sem_checkout():
        lead_id = lead["id"]
        largura = banco.get_metadata(lead_id, "width")
        altura = banco.get_metadata(lead_id, "height")
        if not largura or not altura:
            continue
        if banco.get_metadata(lead_id, "human_handoff", "0") == "1":
            continue
        status = banco.get_metadata(lead_id, "midfunnel_followup_status", "0")
        if status == "2":
            continue

        elapsed_desde_ultima_msg = now_ts - int(lead["last_ts"])

        if elapsed_desde_ultima_msg >= MIDFUNNEL_FIRST_NUDGE_SECONDS and status == "0":
            banco.save_metadata(lead_id, "midfunnel_followup_status", "1")
            banco.save_metadata(lead_id, "midfunnel_followup_at", str(now_ts))
            return "estagio_1", lead_id

        elif status == "1":
            enviado_em_str = banco.get_metadata(lead_id, "midfunnel_followup_at")
            if not enviado_em_str:
                banco.save_metadata(lead_id, "midfunnel_followup_at", str(now_ts))
                continue
            if now_ts - int(enviado_em_str) < MIDFUNNEL_SECOND_NUDGE_SECONDS:
                continue
            banco.save_metadata(lead_id, "midfunnel_followup_status", "2")
            return "estagio_2", lead_id

    return None


def rodar_testes():
    falhas = []

    # --- Filtros básicos (sem medida, checkout já gerado, handoff humano) ---
    banco = BancoFalso()
    t0 = int(time.time())
    banco.add_lead("A_com_medida_5h", "Ana", "111", t0 - 5 * HORA, width="1.2", height="1.5")
    banco.add_lead("B_com_medida_1h", "Bruno", "222", t0 - 1 * HORA, width="1.0", height="1.0")
    banco.add_lead("C_sem_medida_10h", "Carla", "333", t0 - 10 * HORA)
    banco.add_lead("D_ja_tem_checkout", "Duda", "444", t0 - 10 * HORA, sent_checkout=1, width="1.0", height="1.0")
    banco.add_lead("F_handoff_10h", "Fabio", "666", t0 - 10 * HORA, width="1.0", height="1.0", human_handoff="1")

    disparo = rodar_um_ciclo(banco, t0)
    if disparo != ("estagio_1", "A_com_medida_5h"):
        falhas.append(f"esperava disparar estagio_1 pra A, obteve {disparo}")

    ids_candidatos = {l["id"] for l in banco.get_leads_sem_checkout()}
    if "D_ja_tem_checkout" in ids_candidatos:
        falhas.append("get_leads_sem_checkout() nao deveria incluir lead com checkout ja enviado")

    # --- O bug real: 2o toque nao pode disparar minutos depois do 1o, mesmo
    # que a ultima mensagem do cliente ja fosse bem mais antiga que 48h ---
    banco2 = BancoFalso()
    t0 = int(time.time())
    banco2.add_lead("G_sumiu_ha_5_dias", "Gustavo", "777", t0 - 120 * HORA, width="1.5", height="1.5")

    r1 = rodar_um_ciclo(banco2, t0)
    if r1 != ("estagio_1", "G_sumiu_ha_5_dias"):
        falhas.append(f"ciclo 1: esperava estagio_1, obteve {r1}")

    r2 = rodar_um_ciclo(banco2, t0 + 4 * 60)  # 4 minutos depois (ex: restart do serviço)
    if r2 is not None:
        falhas.append(f"BUG DE REGRESSÃO: 2o toque disparou so 4min depois do 1o (deveria esperar 48h) -- {r2}")

    r3 = rodar_um_ciclo(banco2, t0 + 47 * HORA)
    if r3 is not None:
        falhas.append(f"ciclo em +47h: nao deveria disparar ainda, obteve {r3}")

    r4 = rodar_um_ciclo(banco2, t0 + 49 * HORA)
    if r4 != ("estagio_2", "G_sumiu_ha_5_dias"):
        falhas.append(f"ciclo em +49h: esperava estagio_2, obteve {r4}")

    total = 6
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
