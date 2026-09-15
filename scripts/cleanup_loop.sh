#!/usr/bin/env bash
#
# cleanup_loop.sh — roda cleanup_segments.sh em loop, a cada
# INTERVAL_SECONDS. Existe pra manter o buffer com retenção fixa (últimos
# MAX_AGE_MIN minutos, nada mais) enquanto o cron real de produção ainda
# não está instalado (ver README). É isso que o start.sh usa.
#
# Uso: ./cleanup_loop.sh <buffer_root> [max_age_min] [interval_seconds]
#
set -uo pipefail   # sem -e: uma falha isolada do find não deve matar o loop

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BUFFER_ROOT="${1:?uso: cleanup_loop.sh <buffer_root> [max_age_min] [interval_seconds]}"
MAX_AGE_MIN="${2:-2}"
INTERVAL="${3:-30}"

echo "[cleanup_loop] buffer_root=${BUFFER_ROOT} max_age_min=${MAX_AGE_MIN} interval=${INTERVAL}s" >&2

while true; do
    "$HERE/cleanup_segments.sh" "$BUFFER_ROOT" "$MAX_AGE_MIN"
    sleep "$INTERVAL"
done
