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
export ADMIN_USERNAME=admin-teste
export ADMIN_PASSWORD=senha-teste

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

echo "== 11) GET /api/replays/{replay_id} — metadados (M5/PT-04) =="
curl -sf "http://127.0.0.1:8123/api/replays/${REPLAY_ID}" | tee /tmp/replay-api-test/resp_meta.json; echo
python3 -c "
import json
meta = json.load(open('/tmp/replay-api-test/resp_meta.json'))
assert meta['id'] == '${REPLAY_ID}', meta
assert meta['quadra_id'] == '${QUADRA_ID}', meta
assert meta['media_url'] == '/api/replays/${REPLAY_ID}/media', meta
assert meta['lara_status'] == 'pendente', meta
"

echo "== 11.1) GET /api/replays/{replay_id} desconhecido (deve dar 404) =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" "http://127.0.0.1:8123/api/replays/replay-que-nao-existe"

echo "== 12) GET /api/replays/{replay_id}/media — download integral (M6/PT-05) =="
curl -sf "http://127.0.0.1:8123/api/replays/${REPLAY_ID}/media" -o /tmp/replay-api-test/media_full.mp4
cmp /tmp/replay-api-test/downloaded_clip.mp4 /tmp/replay-api-test/media_full.mp4 \
    && echo "OK: conteúdo idêntico ao servido em /clips"

echo "== 13) GET .../media com cabeçalho Range — requisição parcial (RNF1) =="
curl -s -D /tmp/replay-api-test/range_headers.txt -o /tmp/replay-api-test/media_partial.mp4 \
    -H "Range: bytes=0-99" "http://127.0.0.1:8123/api/replays/${REPLAY_ID}/media"
head -n1 /tmp/replay-api-test/range_headers.txt
grep -qi "^HTTP/.* 206" /tmp/replay-api-test/range_headers.txt \
    && echo "OK: 206 Partial Content" \
    || { echo "FALHOU: esperava 206 Partial Content"; cat /tmp/replay-api-test/range_headers.txt; exit 1; }
grep -qi "^content-range:" /tmp/replay-api-test/range_headers.txt \
    && echo "OK: Content-Range presente"
PARTIAL_SIZE=$(stat -c%s /tmp/replay-api-test/media_partial.mp4)
if [[ "$PARTIAL_SIZE" != "100" ]]; then
    echo "FALHOU: esperava 100 bytes no corpo parcial, veio ${PARTIAL_SIZE}" >&2
    exit 1
fi
echo "OK: corpo parcial com 100 bytes"

echo "== 14) Esperando o cooldown (${QUADRA_ID}) pra acionar um 2º replay, pra testar paginação =="
sleep 16
curl -s -o /tmp/replay-api-test/resp_ok2.json -w "HTTP %{http_code}\n" \
    -X POST "http://127.0.0.1:8123/replay/${QUADRA_ID}"
cat /tmp/replay-api-test/resp_ok2.json; echo
REPLAY_ID_2=$(python3 -c "import json;print(json.load(open('/tmp/replay-api-test/resp_ok2.json'))['clip_filename'])" | sed 's/\.mp4$//')

echo "== 15) GET /api/quadras/{quadra_id}/replays — listagem paginada (M7/PT-06) =="
curl -sf "http://127.0.0.1:8123/api/quadras/${QUADRA_ID}/replays?page=1&page_size=1" \
    | tee /tmp/replay-api-test/resp_page1.json; echo
python3 -c "
import json
data = json.load(open('/tmp/replay-api-test/resp_page1.json'))
assert data['total'] == 2, data
assert data['page'] == 1 and data['page_size'] == 1, data
assert len(data['items']) == 1, data
assert data['items'][0]['id'] == '${REPLAY_ID_2}', data  # mais recente primeiro
"
echo "OK: total=2, página 1 traz o replay mais recente primeiro"

echo "== 15.1) GET /api/quadras/{quadra_id}/replays de quadra desconhecida (deve dar 404) =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" "http://127.0.0.1:8123/api/quadras/quadra-que-nao-existe/replays"

echo "== 15.2) GET /quadra/{quadra_id} — página pública, cada replay em 1 <li> só (não 2) =="
curl -sf "http://127.0.0.1:8123/quadra/${QUADRA_ID}" -o /tmp/replay-api-test/quadra_page.html
# marcador de 1 item = o <p>...</p> com o id (o id também aparece dentro do
# <video src=...>, então contar ocorrências soltas do id daria falso
# positivo mesmo sem bug de duplicação — o item da lista é que não pode duplicar)
COUNT_LI_TOTAL=$(grep -o "<li>" /tmp/replay-api-test/quadra_page.html | wc -l)
COUNT_REPLAY_1=$(grep -o "<p>${REPLAY_ID}</p>" /tmp/replay-api-test/quadra_page.html | wc -l)
COUNT_REPLAY_2=$(grep -o "<p>${REPLAY_ID_2}</p>" /tmp/replay-api-test/quadra_page.html | wc -l)
if [[ "$COUNT_LI_TOTAL" != "2" || "$COUNT_REPLAY_1" != "1" || "$COUNT_REPLAY_2" != "1" ]]; then
    echo "FALHOU: esperava 2 <li> no total (1 por replay), achou ${COUNT_LI_TOTAL} <li>, ${COUNT_REPLAY_1}x o replay 1 e ${COUNT_REPLAY_2}x o replay 2" >&2
    exit 1
fi
echo "OK: 2 replays, 2 <li> — um item por replay, sem duplicata"

echo "== 16) DELETE /api/replays/{replay_id} — sem credencial (deve dar 401) =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X DELETE "http://127.0.0.1:8123/api/replays/${REPLAY_ID_2}"

echo "== 16.1) DELETE com credencial ERRADA (deve dar 401) =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X DELETE \
    -u "admin-teste:senha-errada" "http://127.0.0.1:8123/api/replays/${REPLAY_ID_2}"

echo "== 16.2) DELETE com credencial certa (M8/PT-07, deve dar 204) =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X DELETE \
    -u "admin-teste:senha-teste" "http://127.0.0.1:8123/api/replays/${REPLAY_ID_2}"

echo "== 16.3) Confirmando remoção: registro sumiu do banco, arquivo sumiu do disco, 404 na consulta =="
COUNT_AFTER_DELETE=$(sqlite3 "${WORKDIR}/repet_test.db" "SELECT COUNT(*) FROM replay WHERE id = '${REPLAY_ID_2}';")
if [[ "$COUNT_AFTER_DELETE" != "0" ]]; then
    echo "FALHOU: replay '${REPLAY_ID_2}' ainda está no banco depois do DELETE" >&2
    exit 1
fi
if [[ -f "${OUTPUT_DIR}/${REPLAY_ID_2}.mp4" ]]; then
    echo "FALHOU: arquivo '${REPLAY_ID_2}.mp4' ainda está em disco depois do DELETE" >&2
    exit 1
fi
curl -s -o /dev/null -w "HTTP %{http_code}\n" "http://127.0.0.1:8123/api/replays/${REPLAY_ID_2}"
echo "OK: registro removido do banco, arquivo removido do disco"

echo "== OK: endpoint testado de ponta a ponta =="
