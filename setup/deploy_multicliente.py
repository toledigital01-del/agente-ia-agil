#!/usr/bin/env python3
"""
deploy_multicliente.py — Publica o agente no formato multi-cliente na VPS.

Estrutura criada no servidor:

    /opt/agente/                  ← O PROGRAMA, um só, compartilhado
        client_config.py
        agent_core.py
        sessions.py
        agent.py
        watcher.py         (modo Evolution API)
        webhook_server.py  (modo Meta Cloud API)

    /opt/clientes/<cliente>/      ← A "cápsula" de cada cliente
        config.json
        dados.sqlite
        watcher_state.json
        watcher.log / webhook.log

Cada cliente roda como um serviço systemd isolado:
  - Modo watcher : agente@<cliente>.service
  - Modo webhook : agente-webhook@<cliente>.service

Uso:
  python3 setup/deploy_multicliente.py --print-only
  python3 setup/deploy_multicliente.py --deploy --host IP --user root --key ~/.ssh/id_rsa
  python3 setup/deploy_multicliente.py --deploy --host IP --user root --key ~/.ssh/id_rsa \\
          --cliente agil-persianas --config ~/.meu-agente/config.json
"""

import argparse
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REPO        = Path(__file__).resolve().parent.parent
PROGRAM_DIR = "/opt/agente"
CLIENTS_DIR = "/opt/clientes"

# ── Unidades systemd ──────────────────────────────────────────────────────────
# ⚠️ CORRIGIDO 2026-08-11 (ver seção 47 do SKILL.md): estes arquivos usam o
# mecanismo NATIVO de template do systemd (`%i` = o texto depois do "@" no
# nome usado em `systemctl start agente@<isso-aqui>`), em vez de gravar o
# nome do cliente dentro do conteúdo do arquivo. São genéricos de verdade
# agora -- o MESMO arquivo em /etc/systemd/system/agente@.service atende
# qualquer cliente, presente ou futuro, sem nunca precisar ser reescrito.
#
# Antes disso, cada `--deploy --cliente X` sobrescrevia o arquivo inteiro
# com o nome de X gravado dentro -- ou seja, instalar um SEGUNDO cliente
# quebrava silenciosamente a definição de serviço do primeiro (o processo
# dele continuava rodando até o próximo restart, mas com o nome errado
# gravado, pronto pra reiniciar como o cliente errado). Nunca chegou a
# acontecer de verdade porque só existiu um cliente (Ágil Persianas) até
# aqui -- corrigido antes do segundo cliente entrar, não depois de quebrar.

UNIT_WATCHER = """\
[Unit]
Description=Agente de Vendas WhatsApp (Evolution) - %i
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
Environment=AGENTE_CLIENTE=%i
Environment=AGENTE_CLIENTES_DIR=/opt/clientes
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/opt/agente
ExecStart=/usr/bin/python3 /opt/agente/watcher.py
Restart=always
RestartSec=5
StandardOutput=append:/opt/clientes/%i/watcher.log
StandardError=append:/opt/clientes/%i/watcher.log

[Install]
WantedBy=multi-user.target
"""

UNIT_WEBHOOK = """\
[Unit]
Description=Agente de Vendas WhatsApp (Meta Webhook) - %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=AGENTE_CLIENTE=%i
Environment=AGENTE_CLIENTES_DIR=/opt/clientes
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/opt/agente
ExecStart=/usr/bin/python3 /opt/agente/webhook_server.py
Restart=always
RestartSec=5
StandardOutput=append:/opt/clientes/%i/webhook.log
StandardError=append:/opt/clientes/%i/webhook.log

[Install]
WantedBy=multi-user.target
"""


def fix_imports(content: str) -> str:
    content = content.replace(
        'sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))',
        'sys.path.insert(0, str(Path(__file__).parent))'
    )
    content = content.replace("agent_core_template", "agent_core")
    content = content.replace("sessions_template", "sessions")
    return content


def build_program_files() -> dict:
    """Lê todos os templates e retorna um dict nome→conteúdo pronto para copiar."""
    shared   = REPO / "templates" / "shared"
    whatsapp = REPO / "templates" / "whatsapp"

    files = {
        "client_config.py":  (shared / "client_config_template.py").read_text(encoding="utf-8"),
        "agent_core.py":     fix_imports((shared / "agent_core_template.py").read_text(encoding="utf-8")),
        "sessions.py":       fix_imports((shared / "sessions_template.py").read_text(encoding="utf-8")),
        "agent.py":          fix_imports((whatsapp / "agent_template.py").read_text(encoding="utf-8")),
        "watcher.py":        fix_imports((whatsapp / "watcher_template.py").read_text(encoding="utf-8")),
        "webhook_server.py": fix_imports((whatsapp / "webhook_server_template.py").read_text(encoding="utf-8")),
    }
    return files


def deploy_via_ssh(host: str, user: str, key_path: str,
                   files: dict, cliente: str, config_path: str, modo: str):
    """Faz o deploy dos arquivos na VPS via SSH/SFTP usando paramiko."""
    try:
        import paramiko
    except ImportError:
        print("❌ Instale o paramiko: pip install paramiko")
        sys.exit(1)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, key_filename=key_path)
    sftp = ssh.open_sftp()

    def ssh_run(cmd: str):
        _, stdout, stderr = ssh.exec_command(cmd)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        if out:
            print(f"    {out}")
        if err:
            print(f"    ⚠️  {err}")

    # 1. Criar diretórios
    print(f"  Criando {PROGRAM_DIR} e {CLIENTS_DIR}/{cliente}...")
    ssh_run(f"mkdir -p {PROGRAM_DIR} {CLIENTS_DIR}/{cliente}")

    # 2. Enviar arquivos do programa
    print(f"  Enviando {len(files)} arquivos para {PROGRAM_DIR}...")
    for name, content in files.items():
        remote_path = f"{PROGRAM_DIR}/{name}"
        with sftp.open(remote_path, "w") as f:
            f.write(content)
        print(f"    ✅ {name}")

    # 3. Enviar config.json do cliente
    if config_path:
        cfg_local = Path(config_path).expanduser()
        if cfg_local.exists():
            remote_cfg = f"{CLIENTS_DIR}/{cliente}/config.json"
            sftp.put(str(cfg_local), remote_cfg)
            print(f"  ✅ config.json -> {remote_cfg}")
        else:
            print(f"  ⚠️  config.json não encontrado em {config_path}")

    # 4. Instalar dependências
    if modo == "webhook":
        print("  Instalando fastapi e uvicorn...")
        ssh_run("pip3 install -q fastapi uvicorn")

    # 5. Instalar e ativar serviço systemd
    # A unidade é genérica (usa %i, ver comentário acima de UNIT_WATCHER) --
    # gravar ela de novo a cada deploy é seguro e idempotente, o conteúdo é
    # sempre o mesmo texto pra qualquer cliente. É `systemctl start/enable
    # agente@<cliente>` que faz o systemd substituir %i por <cliente>
    # sozinho, não este script.
    service_name = f"agente-webhook@{cliente}" if modo == "webhook" else f"agente@{cliente}"
    unit_content = UNIT_WEBHOOK if modo == "webhook" else UNIT_WATCHER
    unit_template_name = "agente-webhook@.service" if modo == "webhook" else "agente@.service"
    unit_path = f"/etc/systemd/system/{unit_template_name}"

    with sftp.open(unit_path, "w") as f:
        f.write(unit_content)
    print(f"  ✅ Unidade systemd (genérica, serve qualquer cliente): {unit_path}")

    ssh_run("systemctl daemon-reload")
    ssh_run(f"systemctl enable {service_name}")
    ssh_run(f"systemctl restart {service_name}")

    result_cmd = f"systemctl is-active {service_name}"
    _, out, _ = ssh.exec_command(result_cmd)
    status = out.read().decode().strip()
    if status == "active":
        print(f"  ✅ Serviço {service_name} ativo!")
    else:
        print(f"  ⚠️  Serviço com status: {status}")
        print(f"     Verifique: journalctl -u {service_name} -n 30")

    sftp.close()
    ssh.close()


def main():
    parser = argparse.ArgumentParser(description="Publica o agente multi-cliente na VPS")
    parser.add_argument("--print-only", action="store_true",
                        help="Mostra os arquivos sem fazer deploy")
    parser.add_argument("--deploy", action="store_true",
                        help="Faz o deploy via SSH")
    parser.add_argument("--modo", choices=["watcher", "webhook"], default="watcher",
                        help="watcher = Evolution API | webhook = Meta Cloud API")
    parser.add_argument("--host",    help="IP ou hostname da VPS")
    parser.add_argument("--user",    default="root", help="Usuário SSH (padrão: root)")
    parser.add_argument("--key",     help="Caminho para a chave SSH privada")
    parser.add_argument("--cliente", help="Nome do cliente (ex: agil-persianas)")
    parser.add_argument("--config",  default="~/.meu-agente/config.json",
                        help="config.json do cliente a enviar")
    args = parser.parse_args()

    files = build_program_files()

    print("=" * 60)
    print(f"Arquivos do programa compartilhado ({PROGRAM_DIR}):")
    for name, content in files.items():
        print(f"  {name:25s} {len(content):>8,} bytes")
    print("=" * 60)

    if args.print_only:
        print("\nUnidade systemd — modo watcher (genérica, %i = nome do cliente usado em 'systemctl start agente@<cliente>'):")
        print(UNIT_WATCHER)
        print("\nUnidade systemd — modo webhook:")
        print(UNIT_WEBHOOK)
        return

    if args.deploy:
        if not args.host:
            print("❌ Informe o IP da VPS: --host IP")
            sys.exit(1)
        if not args.key:
            print("❌ Informe a chave SSH: --key ~/.ssh/id_rsa")
            sys.exit(1)
        if not args.cliente:
            print("❌ Informe o nome do cliente: --cliente agil-persianas")
            sys.exit(1)

        print(f"\nDeployando para {args.user}@{args.host} (modo: {args.modo})...")
        deploy_via_ssh(
            host=args.host,
            user=args.user,
            key_path=args.key,
            files=files,
            cliente=args.cliente,
            config_path=args.config,
            modo=args.modo,
        )
        print("\n✅ Deploy concluído!")
        return

    # Sem --deploy: mostrar instruções manuais
    print(f"""
Para fazer deploy manual na VPS:

  1. Copiar arquivos para {PROGRAM_DIR}:
       scp agent.py agent_core.py sessions.py client_config.py \\
           watcher.py webhook_server.py root@IP:{PROGRAM_DIR}/

  2. Copiar config.json do cliente:
       scp ~/.meu-agente/config.json root@IP:{CLIENTS_DIR}/<cliente>/

  3. Instalar serviço (na VPS):
       # modo watcher
       python3 setup/deploy_multicliente.py --deploy \\
         --host IP --key ~/.ssh/id_rsa --cliente <cliente> --modo watcher

       # modo webhook
       python3 setup/deploy_multicliente.py --deploy \\
         --host IP --key ~/.ssh/id_rsa --cliente <cliente> --modo webhook
""")


if __name__ == "__main__":
    main()
