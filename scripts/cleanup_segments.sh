#!/usr/bin/env bash
# cleanup_segments.sh — remove segmentos brutos antigos do buffer, mantendo
# só os últimos MAX_AGE_MIN minutos de retroativo (default: 2min — dá folga
# suficiente pra cobrir CLIP_DURATION_SECONDS=35s + margem de seleção).
# Rodar via cron a cada minuto (produção) ou via scripts/cleanup_loop.sh
# (dev/bring-up, usado pelo start.sh). O clipe final (se algum foi gerado)
# já foi copiado pro disco persistente antes disso, então apagar aqui é
# seguro — não mexe em OUTPUT_DIR, só no buffer bruto.
#
# Uso: ./cleanup_segments.sh [buffer_root] [max_age_min]
set -euo pipefail

BUFFER_ROOT="${1:-/var/replay}"
MAX_AGE_MIN="${2:-2}"

find "$BUFFER_ROOT" -mindepth 2 -maxdepth 2 -name "seg_*.mp4" -mmin "+${MAX_AGE_MIN}" -delete
