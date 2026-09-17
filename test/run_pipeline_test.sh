#!/usr/bin/env bash
#
# run_pipeline_test.sh — valida capture_camera.sh + clip_generator.py juntos,
# de ponta a ponta, SEM precisar de câmera real. Usa uma fonte sintética
# (ffmpeg lavfi testsrc) no lugar do RTSP.
#
# O que isso prova: que o pipeline "buffer contínuo -> corte dos últimos N
# segundos -> arquivo final em disco" funciona a nível de código. A troca
# pro RTSP real é só trocar a input_url (ver capture_camera.sh).
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

QUADRA_ID="test-quadra1"
WORKDIR="/tmp/replay-pipeline-test"
BUFFER_ROOT="${WORKDIR}/buffer"
OUTPUT_DIR="${WORKDIR}/output"
SEGMENT_TIME=2
CLIP_DURATION=35
WARMUP_SECONDS=45   # > CLIP_DURATION + folga, pra garantir buffer suficiente

rm -rf "$WORKDIR"
mkdir -p "$BUFFER_ROOT" "$OUTPUT_DIR"

echo "== 1) Subindo captura sintética (simula a câmera) =="
"$ROOT/capture/capture_camera.sh" \
    "$QUADRA_ID" \
    "lavfi:testsrc=size=640x480:rate=15" \
    "$BUFFER_ROOT" \
    "$SEGMENT_TIME" &
CAPTURE_PID=$!

cleanup() {
    kill "$CAPTURE_PID" 2>/dev/null || true
    wait "$CAPTURE_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "== 2) Esperando ${WARMUP_SECONDS}s pro buffer encher (>= ${CLIP_DURATION}s de vídeo) =="
sleep "$WARMUP_SECONDS"

echo "== 3) Segmentos no buffer agora: =="
ls -la "${BUFFER_ROOT}/${QUADRA_ID}"

echo "== 4) Simulando o trigger do botão: gerando clipe dos últimos ${CLIP_DURATION}s =="
CLIP_PATH=$(python3 "$ROOT/clipper/clip_generator.py" \
    "$QUADRA_ID" "$BUFFER_ROOT" "$OUTPUT_DIR" --duration "$CLIP_DURATION")

echo "Clipe gerado em: $CLIP_PATH"

echo "== 5) Validando o arquivo final com ffprobe =="
ffprobe -v error -select_streams v:0 \
    -show_entries stream=duration,width,height,codec_name \
    -of default=noprint_wrappers=1 "$CLIP_PATH"

echo "== OK: pipeline de captura + corte validado sem precisar de câmera real =="
