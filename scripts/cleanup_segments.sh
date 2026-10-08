#!/usr/bin/env bash
# cleanup_segments.sh — remove segmentos brutos do buffer mais velhos que
# MAX_AGE_MIN minutos. Roda via cron ou via scripts/cleanup_loop.sh.
# Não mexe em OUTPUT_DIR, só no buffer.
#
# Uso: ./cleanup_segments.sh [buffer_root] [max_age_min]
set -euo pipefail

BUFFER_ROOT="${1:-/var/replay}"
MAX_AGE_MIN="${2:-2}"

find "$BUFFER_ROOT" -mindepth 2 -maxdepth 2 -name "seg_*.mp4" -mmin "+${MAX_AGE_MIN}" -delete
