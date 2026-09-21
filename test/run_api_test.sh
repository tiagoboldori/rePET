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
export DATABASE_URL="sqlite:///${WORKDIR}/repet_test.db"
export CLIP_DURATION_SECONDS=35
export SEGMENT_TIME=2

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

echo "== 3) Esperando a API e o buffer ficarem prontos (~48s) =="
sleep 3
curl -sf http://127.0.0.1:8123/health && echo " <- API respondendo"
sleep 45

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

echo "== 9) Validando que o replay foi registrado no banco (PT-02) =="
REPLAY_ID=$(basename "$CLIP_URL" .mp4)
sqlite3 "${WORKDIR}/repet_test.db" \
    "SELECT id, quadra_id, lara_status, duracao_segundos, tamanho_bytes FROM replay WHERE id = '${REPLAY_ID}';"
COUNT=$(sqlite3 "${WORKDIR}/repet_test.db" "SELECT COUNT(*) FROM replay WHERE id = '${REPLAY_ID}';")
if [[ "$COUNT" != "1" ]]; then
    echo "FALHOU: replay '${REPLAY_ID}' não encontrado no banco (esperado 1 linha, achou ${COUNT})" >&2
    exit 1
fi

echo "== 10) Validando que a quadra foi migrada de cameras.json pro banco (PT-10) =="
sqlite3 "${WORKDIR}/repet_test.db" \
    "SELECT q.id, q.nome, q.esporte_id, l.nome FROM quadra q JOIN local l ON l.id = q.local_id WHERE q.id = '${QUADRA_ID}';"

echo "== OK: endpoint testado de ponta a ponta =="
