#!/usr/bin/env bash
#
# watchdog_loop.sh — chama ensure_services.sh a cada INTERVAL_SECONDS para
# reiniciar o que tiver caído (sem rodar os testes de novo).
#
# O próprio loop não tem supervisor; os units em systemd/ cobrem esse caso.
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
