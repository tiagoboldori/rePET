#!/usr/bin/env bash
#
# run_api_test.sh — testa o endpoint HTTP de verdade (POST /replay/{quadra_id}),
# com um servidor uvicorn real e uma câmera sintética real. Simula o que vai
# acontecer quando o Home Assistant chamar esse endpoint depois do botão.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

QUADRA_ID="loc1-quadra1"   # precisa existir em config/cameras.json
WORKDIR="/tmp/replay-api-test"
export BUFFER_ROOT="${WORKDIR}/buffer"
export OUTPUT_DIR="${WORKDIR}/output"
export CAMERAS_FILE="${ROOT}/config/cameras.json"
export CLIP_DURATION_SECONDS=45
export SEGMENT_TIME=5

rm -rf "$WORKDIR"
mkdir -p "$BUFFER_ROOT" "$OUTPUT_DIR"

echo "== 1) Subindo câmera sintética pra ${QUADRA_ID} =="
"$ROOT/capture/capture_camera.sh" \
    "$QUADRA_ID" "lavfi:testsrc=size=640x480:rate=15" "$BUFFER_ROOT" "$SEGMENT_TIME" &
CAPTURE_PID=$!

echo "== 2) Subindo a API (uvicorn) =="
python3 -m uvicorn api.main:app --host 127.0.0.1 --port 8123 --log-level warning &
API_PID=$!

cleanup() {
    kill "$CAPTURE_PID" "$API_PID" 2>/dev/null || true
    wait "$CAPTURE_PID" "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "== 3) Esperando a API e o buffer ficarem prontos (~55s) =="
sleep 3
curl -sf http://127.0.0.1:8123/health && echo " <- API respondendo"
sleep 52

echo "== 4) Testando quadra_id DESCONHECIDO (deve dar 404) =="
curl -s -o /tmp/replay-api-test/resp_404.json -w "HTTP %{http_code}\n" \
    -X POST http://127.0.0.1:8123/replay/quadra-que-nao-existe
cat /tmp/replay-api-test/resp_404.json; echo

echo "== 5) Simulando o botão de verdade: POST /replay/${QUADRA_ID} =="
curl -s -o /tmp/replay-api-test/resp_ok.json -w "HTTP %{http_code}\n" \
    -X POST "http://127.0.0.1:8123/replay/${QUADRA_ID}"
cat /tmp/replay-api-test/resp_ok.json; echo

CLIP_URL=$(python3 -c "import json;print(json.load(open('/tmp/replay-api-test/resp_ok.json'))['clip_url'])")

echo "== 6) Baixando o clipe pela URL pública retornada (${CLIP_URL}) =="
curl -sf "http://127.0.0.1:8123${CLIP_URL}" -o /tmp/replay-api-test/downloaded_clip.mp4
ls -la /tmp/replay-api-test/downloaded_clip.mp4

echo "== 7) Validando o clipe baixado com ffprobe =="
ffprobe -v error -select_streams v:0 \
    -show_entries stream=duration,codec_name -of default=noprint_wrappers=1 \
    /tmp/replay-api-test/downloaded_clip.mp4

echo "== 8) Arquivo final também está salvo em disco persistente aqui: =="
ls -la "$OUTPUT_DIR"

echo "== OK: endpoint testado de ponta a ponta =="
