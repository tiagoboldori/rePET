#!/usr/bin/env bash
#
# ensure_services.sh — garante que API, capturas, limpeza do buffer, retenção
# local e (se configurado) o worker da plataforma externa estejam no ar.
# Idempotente: checa o PID em run/*.pid antes de subir qualquer coisa.
# Chamado pelo start.sh e pelo watchdog_loop.sh.
#
# Espera exportados: BUFFER_ROOT, OUTPUT_DIR, CAMERAS_FILE, DATABASE_URL,
# SEGMENT_TIME, OVERLAY_CACHE_DIR e, opcionalmente, LARA_BASE_URL/REPLAY_API_TOKEN.
# Com ENSURE_SERVICES_QUIET=1 omite as linhas "já rodando".
#
# Uso: ROOT=/caminho/do/repo bash ensure_services.sh
set -uo pipefail

ROOT="${ROOT:?ROOT precisa estar setado (diretório raiz do repositório)}"
LOG_DIR="$ROOT/logs"
RUN_DIR="$ROOT/run"
PY="$ROOT/.venv/bin/python"
QUIET="${ENSURE_SERVICES_QUIET:-0}"
mkdir -p "$LOG_DIR" "$RUN_DIR"

info() { echo "[INFO]    $*"; }
fail() { echo "[FALHOU]  $*"; }
ok()   { [[ "$QUIET" == "1" ]] || echo "[OK]      $*"; }

# Evita subir duplicata de um processo que roda fora do PID file (ex.: de outro
# usuário). O `kill -0` falha entre usuários, então usamos `pgrep -f`, que só lê
# /proc. Duas capturas da mesma câmera geram clipe com conteúdo fora de ordem.
check_no_foreign_duplicate() {
    local label="$1" pattern="$2" expected_pid="${3:-}"
    local found
    found="$(pgrep -f -- "$pattern" 2>/dev/null | grep -vx "${expected_pid:-__nenhum__}" || true)"
    if [[ -n "$found" ]]; then
        fail "$label: já existe processo rodando fora do run/*.pid esperado (PID(s): $(echo "$found" | tr '\n' ' ')) — provável duplicata de outra sessão/usuário. Não vou subir mais um: capturas simultâneas da mesma câmera geram clipes com conteúdo fora de ordem. Confira o dono ('ps -o pid,user,cmd -p <PID>') e mate manualmente ('sudo kill <PID>' se for de outro usuário) antes de rodar de novo."
        return 1
    fi
    return 0
}

BUFFER_MAX_AGE_MIN=2

# --- API -------------------------------------------------------------------
API_PID_FILE="$RUN_DIR/api.pid"
if [[ -f "$API_PID_FILE" ]] && kill -0 "$(cat "$API_PID_FILE")" 2>/dev/null; then
    ok "API já rodando (PID $(cat "$API_PID_FILE"))"
elif ! check_no_foreign_duplicate "API" "uvicorn api.main:app --host 0.0.0.0 --port 8000" "$(cat "$API_PID_FILE" 2>/dev/null || true)"; then
    :  # o check já avisou
else
    info "Subindo a API em 0.0.0.0:8000 ..."
    nohup "$PY" -m uvicorn api.main:app --host 0.0.0.0 --port 8000 \
        >>"$LOG_DIR/api.log" 2>&1 &
    echo $! > "$API_PID_FILE"
    sleep 2
    if kill -0 "$(cat "$API_PID_FILE")" 2>/dev/null; then
        ok "API no ar (PID $(cat "$API_PID_FILE")), log em logs/api.log"
    else
        fail "API caiu ao subir — veja logs/api.log"
    fi
fi

# --- Captura de cada câmera de cameras.json ---------------------------------
while IFS=$'\t' read -r quadra_id input_url; do
    [[ -z "$quadra_id" ]] && continue
    PID_FILE="$RUN_DIR/capture_${quadra_id}.pid"
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        ok "Captura de '$quadra_id' já rodando (PID $(cat "$PID_FILE"))"
        continue
    fi
    if ! check_no_foreign_duplicate "Captura de '$quadra_id'" "${BUFFER_ROOT}/${quadra_id}/seg_%Y%m%d%H%M%S.mp4" "$(cat "$PID_FILE" 2>/dev/null || true)"; then
        continue  # o check já avisou
    fi
    info "Subindo captura de '$quadra_id' ..."
    nohup bash "$ROOT/capture/capture_camera.sh" "$quadra_id" "$input_url" "$BUFFER_ROOT" "$SEGMENT_TIME" \
        >>"$LOG_DIR/capture_${quadra_id}.log" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 1
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        ok "Captura de '$quadra_id' no ar (PID $(cat "$PID_FILE")), log em logs/capture_${quadra_id}.log"
    else
        fail "Captura de '$quadra_id' caiu ao subir — veja logs/capture_${quadra_id}.log"
    fi
done < <("$PY" -c "
import json
with open('$CAMERAS_FILE') as f:
    for c in json.load(f):
        print(c['quadra_id'] + '\t' + c['input_url'])
")

# --- Limpeza do buffer bruto ------------------------
CLEANUP_PID_FILE="$RUN_DIR/cleanup.pid"
if [[ -f "$CLEANUP_PID_FILE" ]] && kill -0 "$(cat "$CLEANUP_PID_FILE")" 2>/dev/null; then
    ok "Limpeza do buffer já rodando (PID $(cat "$CLEANUP_PID_FILE"))"
elif ! check_no_foreign_duplicate "Limpeza do buffer" "scripts/cleanup_loop.sh $BUFFER_ROOT" "$(cat "$CLEANUP_PID_FILE" 2>/dev/null || true)"; then
    :  # o check já avisou
else
    info "Subindo limpeza do buffer (retenção: últimos ${BUFFER_MAX_AGE_MIN}min) ..."
    nohup bash "$ROOT/scripts/cleanup_loop.sh" "$BUFFER_ROOT" "$BUFFER_MAX_AGE_MIN" 30 \
        >>"$LOG_DIR/cleanup.log" 2>&1 &
    echo $! > "$CLEANUP_PID_FILE"
    sleep 1
    if kill -0 "$(cat "$CLEANUP_PID_FILE")" 2>/dev/null; then
        ok "Limpeza do buffer no ar (PID $(cat "$CLEANUP_PID_FILE")), log em logs/cleanup.log"
    else
        fail "Limpeza do buffer caiu ao subir — veja logs/cleanup.log"
    fi
fi

# --- Retenção local dos clipes finais ---------------------------------------
# Não depende de credencial externa, sobe sempre.
RETENTION_PID_FILE="$RUN_DIR/local_retention.pid"
if [[ -f "$RETENTION_PID_FILE" ]] && kill -0 "$(cat "$RETENTION_PID_FILE")" 2>/dev/null; then
    ok "Retenção local já rodando (PID $(cat "$RETENTION_PID_FILE"))"
elif ! check_no_foreign_duplicate "Retenção local" "-m scripts.local_retention_loop" "$(cat "$RETENTION_PID_FILE" 2>/dev/null || true)"; then
    :  # o check já avisou
else
    info "Subindo retenção local dos clipes finais (LOCAL_RAW_RETENTION_DAYS) ..."
    # -u: sem buffer de saída, senão o log fica vazio quando não é um TTY
    nohup "$PY" -u -m scripts.local_retention_loop >>"$LOG_DIR/local_retention.log" 2>&1 &
    echo $! > "$RETENTION_PID_FILE"
    sleep 1
    if kill -0 "$(cat "$RETENTION_PID_FILE")" 2>/dev/null; then
        ok "Retenção local no ar (PID $(cat "$RETENTION_PID_FILE")), log em logs/local_retention.log"
    else
        fail "Retenção local caiu ao subir — veja logs/local_retention.log"
    fi
fi

# --- Worker da plataforma externa (se configurado) --------------------------
if [[ -z "${LARA_BASE_URL:-}" || -z "${REPLAY_API_TOKEN:-}" ]]; then
    [[ "$QUIET" == "1" ]] || info "LARA_BASE_URL/REPLAY_API_TOKEN não definidos — worker do Lara não sobe (integração ainda não configurada)."
else
    LARA_PID_FILE="$RUN_DIR/lara_worker.pid"
    if [[ -f "$LARA_PID_FILE" ]] && kill -0 "$(cat "$LARA_PID_FILE")" 2>/dev/null; then
        ok "Worker do Lara já rodando (PID $(cat "$LARA_PID_FILE"))"
    elif ! check_no_foreign_duplicate "Worker do Lara" "-m scripts.lara_worker" "$(cat "$LARA_PID_FILE" 2>/dev/null || true)"; then
        :  # o check já avisou
    else
        info "Subindo worker do Lara (sync/upload/heartbeat) ..."
        # -u: mesmo motivo da retenção local
        nohup "$PY" -u -m scripts.lara_worker >>"$LOG_DIR/lara_worker.log" 2>&1 &
        echo $! > "$LARA_PID_FILE"
        sleep 1
        if kill -0 "$(cat "$LARA_PID_FILE")" 2>/dev/null; then
            ok "Worker do Lara no ar (PID $(cat "$LARA_PID_FILE")), log em logs/lara_worker.log"
        else
            fail "Worker do Lara caiu ao subir — veja logs/lara_worker.log"
        fi
    fi
fi
