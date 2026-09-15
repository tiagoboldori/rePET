#!/usr/bin/env bash
#
# capture_camera.sh — captura contínua de UMA câmera, gravando em segmentos
# curtos no buffer (tmpfs em produção, ver systemd/replay-capture@.service).
#
# Em produção: stream-copy puro (sem reencode) do RTSP da câmera, como já
# definido no contexto do projeto. Cada instância deste script cuida de uma
# quadra; rodam N instâncias em paralelo (uma por câmera) via systemd.
#
# Uso:
#   ./capture_camera.sh <quadra_id> <input_url> <buffer_root> [segment_time]
#
# Exemplos:
#   Produção (câmera real via RTSP, puxado pela intranet):
#     ./capture_camera.sh loc1-quadra3 \
#         "rtsp://user:senha@10.0.1.13:554/stream1" /var/replay
#
#   Teste local (SEM câmera — gera vídeo sintético só pra validar o pipeline):
#     ./capture_camera.sh test-quadra1 \
#         "lavfi:testsrc=size=640x480:rate=15" /tmp/replay-test/buffer
#
set -euo pipefail

QUADRA_ID="${1:?uso: capture_camera.sh <quadra_id> <input_url> <buffer_root> [segment_time]}"
INPUT_URL="${2:?input_url obrigatório (rtsp://... para câmera real, ou lavfi:... para teste)}"
BUFFER_ROOT="${3:?buffer_root obrigatório (ex: /var/replay)}"
SEGMENT_TIME="${4:-5}"

OUT_DIR="${BUFFER_ROOT}/${QUADRA_ID}"
mkdir -p "$OUT_DIR"

# --- Monta os argumentos de entrada e codec dependendo do tipo de fonte ---
INPUT_ARGS=()
CODEC_ARGS=()

if [[ "$INPUT_URL" == rtsp://* ]]; then
    # Câmera real: puxa via TCP (mais confiável que UDP em rede compartilhada)
    # e faz stream-copy puro — sem reencode, CPU quase zero (câmera já entrega H264).
    INPUT_ARGS=(-rtsp_transport tcp -i "$INPUT_URL")
    CODEC_ARGS=(-c copy)
elif [[ "$INPUT_URL" == lavfi:* ]]; then
    # Fonte sintética pra teste local sem câmera. lavfi não entrega stream
    # comprimido, então aqui SIM precisa reencode leve (só nesse modo de teste).
    # -re: gera os frames no ritmo de tempo real, senão o lavfi despeja tudo
    # instantaneamente e o teste de "últimos N segundos" perde o sentido.
    SRC="${INPUT_URL#lavfi:}"
    INPUT_ARGS=(-re -f lavfi -i "$SRC")
    CODEC_ARGS=(-c:v libx264 -preset ultrafast -pix_fmt yuv420p -g 30 -an)
else
    # Qualquer outra URL de vídeo (arquivo, http, etc.) — tratamento genérico.
    INPUT_ARGS=(-i "$INPUT_URL")
    CODEC_ARGS=(-c copy)
fi

echo "[capture_camera] quadra=${QUADRA_ID} out=${OUT_DIR} segment_time=${SEGMENT_TIME}s" >&2

exec ffmpeg -nostdin -loglevel warning \
    "${INPUT_ARGS[@]}" \
    "${CODEC_ARGS[@]}" \
    -f segment -segment_time "$SEGMENT_TIME" -reset_timestamps 1 \
    -strftime 1 "${OUT_DIR}/seg_%Y%m%d%H%M%S.mp4"
