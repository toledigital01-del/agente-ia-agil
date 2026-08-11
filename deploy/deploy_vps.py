#!/usr/bin/env python3
"""Renderiza os templates deste repositório e implanta em /opt/agente/ na VPS.

Roda DIRETO NA VPS (depois de um `git pull` no clone deste repositório).
Substitui o fluxo antigo de "patch script com match exato de string" usado
manualmente a cada correção -- ver seção 5 do SKILL.md (deploy).

Uso na VPS:
    cd /opt/agente-repo && git pull
    python3 /opt/agente-repo/deploy/deploy_vps.py

Este script:
1. Renderiza os templates (aplica os replaces de import necessários --
   documentado abaixo, o único arquivo que precisa é agent_template.py).
2. Confere sintaxe (py_compile) de TODOS os arquivos renderizados ANTES de
   tocar em /opt/agente/ -- nunca aplica nada que não compile.
3. Faz backup do estado atual de /opt/agente/*.py (só 1 backup rotativo,
   não acumula dezenas de arquivos .bak-* como o fluxo manual antigo).
4. Copia os arquivos renderizados pra /opt/agente/.
5. Descobre TODOS os clientes instalados (uma pasta por cliente em
   /opt/clientes/) e reinicia o serviço systemd de cada um -- código
   compartilhado, então uma atualização vale pra todo mundo que roda nele
   (ver seção 47 do SKILL.md: até 2026-08-11 isso reiniciava só um nome de
   serviço fixo, e um segundo cliente ficaria sem reiniciar depois de um
   deploy sem ninguém perceber).

Se algo falhar em qualquer etapa, para imediatamente SEM aplicar nada
parcial e sem reiniciar nenhum serviço -- a versão anterior continua rodando.
"""
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = Path("/opt/agente")
CLIENTS_DIR = Path("/opt/clientes")

# (arquivo de origem, replaces de import a aplicar, nome de destino)
# Só agent_template.py precisa de replace -- os outros já importam pelos
# nomes finais (confirmado 2026-08-10, comparando prod x local byte a byte).
ARQUIVOS = [
    (
        "templates/whatsapp/agent_template.py",
        [
            (
                'sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))',
                "sys.path.insert(0, str(Path(__file__).parent))",
            ),
            ("agent_core_template", "agent_core"),
            ("sessions_template", "sessions"),
        ],
        "agent.py",
    ),
    ("templates/whatsapp/watcher_template.py", [], "watcher.py"),
    ("templates/whatsapp/webhook_server_template.py", [], "webhook_server.py"),
    ("templates/shared/agent_core_template.py", [], "agent_core.py"),
    ("templates/shared/sessions_template.py", [], "sessions.py"),
    ("templates/shared/client_config_template.py", [], "client_config.py"),
]


def renderizar():
    renderizados = {}
    for origem, replaces, destino in ARQUIVOS:
        caminho = REPO_ROOT / origem
        texto = caminho.read_text(encoding="utf-8")
        for velho, novo in replaces:
            if velho not in texto:
                print(f"ERRO: replace esperado não encontrado em {origem}: {velho!r}")
                sys.exit(1)
            texto = texto.replace(velho, novo)
        renderizados[destino] = texto
    return renderizados


def checar_sintaxe(renderizados):
    tmp_dir = Path("/tmp/deploy_vps_check")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    for destino, texto in renderizados.items():
        (tmp_dir / destino).write_text(texto, encoding="utf-8")
    for destino in renderizados:
        resultado = subprocess.run(
            [sys.executable, "-m", "py_compile", str(tmp_dir / destino)],
            capture_output=True,
            text=True,
        )
        if resultado.returncode != 0:
            print(f"ERRO DE SINTAXE em {destino}, abortando sem tocar em produção:")
            print(resultado.stderr)
            sys.exit(1)
    shutil.rmtree(tmp_dir)
    print("Sintaxe OK em todos os arquivos.")


def fazer_backup():
    backup_dir = DEPLOY_DIR / "_backup_anterior"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir()
    for _, _, destino in ARQUIVOS:
        alvo = DEPLOY_DIR / destino
        if alvo.exists():
            shutil.copy2(alvo, backup_dir / destino)
    print(f"Backup do estado anterior salvo em {backup_dir} (sobrescreve o backup do deploy passado).")


def aplicar(renderizados):
    for destino, texto in renderizados.items():
        (DEPLOY_DIR / destino).write_text(texto, encoding="utf-8")
    print("Arquivos copiados para", DEPLOY_DIR)


def descobrir_clientes() -> list:
    """Lista os clientes instalados nesta VPS (uma pasta com config.json em
    /opt/clientes/) -- é assim que sabemos quais serviços systemd existem
    sem precisar de uma lista mantida na mão em algum lugar."""
    if not CLIENTS_DIR.exists():
        return []
    return sorted(
        p.name for p in CLIENTS_DIR.iterdir()
        if p.is_dir() and (p / "config.json").exists()
    )


def reiniciar_servicos():
    clientes = descobrir_clientes()
    if not clientes:
        print(f"⚠️  Nenhum cliente encontrado em {CLIENTS_DIR} -- nada pra reiniciar.")
        return
    print(f"Clientes encontrados: {', '.join(clientes)}")
    for cliente in clientes:
        service_name = f"agente@{cliente}.service"
        subprocess.run(["systemctl", "restart", service_name], check=True)
        resultado = subprocess.run(
            ["systemctl", "is-active", service_name], capture_output=True, text=True
        )
        status = resultado.stdout.strip()
        print(f"Status de {service_name}: {status}")
        if status != "active":
            print(f"⚠️  {service_name} não ficou ativo -- verifique com: journalctl -u {service_name}")


def main():
    print("== Renderizando templates ==")
    renderizados = renderizar()
    print("== Checando sintaxe ==")
    checar_sintaxe(renderizados)
    print("== Backup ==")
    fazer_backup()
    print("== Aplicando ==")
    aplicar(renderizados)
    print("== Reiniciando serviços (todos os clientes instalados) ==")
    reiniciar_servicos()
    print("== Deploy concluído ==")


if __name__ == "__main__":
    main()
