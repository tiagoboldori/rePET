#!/usr/bin/env bash
# cleanup_segments.sh — remove segmentos brutos antigos do buffer.
# Rodar via cron a cada minuto. O clipe final (se algum foi gerado) já foi
# copiado pro disco persistente antes disso, então apagar aqui é seguro.
#
# Uso: ./cleanup_segments.sh [buffer_root] [max_age_min]
set -euo pipefail

BUFFER_ROOT="${1:-/var/replay}"
MAX_AGE_MIN="${2:-3}"

find "$BUFFER_ROOT" -mindepth 2 -maxdepth 2 -name "seg_*.mp4" -mmin "+${MAX_AGE_MIN}" -delete
