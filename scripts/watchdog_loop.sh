#!/usr/bin/env bash
#
# watchdog_loop.sh — chama ensure_services.sh a cada INTERVAL_SECONDS,
# sem rodar a suíte de testes de novo (isso start.sh já fez uma vez na
# subida). É o que dá supervisão de verdade ao sistema rodando via
# start.sh: se a API, uma captura, a limpeza do buffer, a retenção local
# ou o worker do Lara caírem depois da subida inicial, este loop detecta
# (via o mesmo PID file) e sobe de novo sozinho, sem precisar de
# `./start.sh` manual.
#
# ENSURE_SERVICES_QUIET=1 evita repetir "já rodando" a cada passada — só
# aparece linha nova no log quando algo de fato precisou ser reiniciado
# ou falhou ao subir.
#
# Limitação conhecida: este próprio loop não tem supervisor (é bash
# puro, subido pelo start.sh) — se ELE morrer, nada o reinicia sozinho.
# A resposta completa pra isso são os units systemd em systemd/ (
# Restart=always no nível do sistema operacional, sem esse ponto único
# de falha) — usar este loop é a opção sem depender de systemd/root.
#
# Uso: ROOT=/caminho/do/repo bash watchdog_loop.sh [interval_seconds]
set -uo pipefail

ROOT="${ROOT:?ROOT precisa estar setado (diretório raiz do repositório)}"
INTERVAL="${1:-30}"

echo "[watchdog] no ar — checando serviços a cada ${INTERVAL}s" >&2

while true; do
    ENSURE_SERVICES_QUIET=1 ROOT="$ROOT" bash "$ROOT/scripts/ensure_services.sh"
    sleep "$INTERVAL"
done
