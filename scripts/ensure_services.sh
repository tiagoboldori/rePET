#!/usr/bin/env bash
#
# ensure_services.sh — garante que API, captura de cada câmera, limpeza
# do buffer, retenção local dos clipes finais e (se configurado) o worker
# do Lara estejam no ar. Idempotente: não sobe duplicata de nada que já
# esteja rodando (checa PID em run/*.pid).
#
# Extraído de start.sh pra ser chamado tanto por ele (na subida inicial,
# depois dos testes) quanto por watchdog_loop.sh (repetidamente, sem
# rodar testes de novo a cada passada) — mesma lógica, uma fonte só.
#
# Espera as variáveis de ambiente já exportadas por quem chama (start.sh
# faz isso): BUFFER_ROOT, OUTPUT_DIR, CAMERAS_FILE, DATABASE_URL,
# SEGMENT_TIME, OVERLAY_CACHE_DIR, e opcionalmente LARA_BASE_URL/
# REPLAY_API_TOKEN. Com ENSURE_SERVICES_QUIET=1, omite as linhas
# "já rodando" (usado pelo watchdog, pra não inundar o log a cada
# passada quando está tudo saudável) — falha/reinício continuam sempre
# visíveis.
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

# Achado real em 2026-09-22 (ver memória do projeto): um watchdog órfão de
# OUTRO usuário (root, sobrevivente de uma sessão anterior nunca encerrada)
# rodou em paralelo com o watchdog normal por horas. `kill -0` pra checar
# "já tá rodando" falha entre usuários diferentes (sem permissão pra
# sinalizar processo alheio), então nenhum dos dois nunca reconhecia o
# processo do outro como vivo — cada um ficava subindo duplicata sem nunca
# matar a anterior. Resultado real: ~46 `ffmpeg` simultâneos na mesma
# câmera, cada um com seu próprio relógio interno, produzindo clipe final
# com conteúdo fora de ordem (vídeo "voltando"/flicando).
#
# Defesa: antes de subir qualquer processo nesta lista, além do `kill -0`
# no PID salvo (rápido, caminho normal), procurar por QUALQUER processo no
# sistema (`pgrep -f`, que só precisa ler /proc — não depende de permissão
# de sinal, então enxerga processo de outro usuário) cujo comando bata com
# o padrão esperado e que NÃO seja o PID já checado. Se achar, não sobe
# duplicata — falha alto (visível no log/watchdog) pedindo investigação
# manual, em vez de silenciosamente empilhar mais um processo.
check_no_foreign_duplicate() {
    local label="$1" pattern="$2" expected_pid="${3:-}"
    local found
    found="$(pgrep -f -- "$pattern" 2>/dev/null | grep -vx "${expected_pid:-__nenhum__}" || true)"
    if [[ -n "$found" ]]; then
        fail "$label: já existe processo rodando fora do run/*.pid esperado (PID(s): $(echo "$found" | tr '\n' ' ')) — provável duplicata de outra sessão/usuário. NÃO vou subir mais um (evita repetir o incidente de 2026-09-22, vídeo com conteúdo fora de ordem por múltiplas capturas simultâneas da mesma câmera). Confira o dono ('ps -o pid,user,cmd -p <PID>') e mate manualmente ('sudo kill <PID>' se for de outro usuário) antes de rodar de novo."
        return 1
    fi
    return 0
}

BUFFER_MAX_AGE_MIN=2    # retenção do buffer bruto: só os últimos 2min, nada mais

# --- API -------------------------------------------------------------------
API_PID_FILE="$RUN_DIR/api.pid"
if [[ -f "$API_PID_FILE" ]] && kill -0 "$(cat "$API_PID_FILE")" 2>/dev/null; then
    ok "API já rodando (PID $(cat "$API_PID_FILE"))"
elif ! check_no_foreign_duplicate "API" "uvicorn api.main:app --host 0.0.0.0 --port 8000" "$(cat "$API_PID_FILE" 2>/dev/null || true)"; then
    :  # aviso já emitido pelo check acima, não sobe duplicata
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
        continue  # aviso já emitido, não sobe duplicata
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

# --- Limpeza do buffer bruto (retenção fixa de 2min) ------------------------
CLEANUP_PID_FILE="$RUN_DIR/cleanup.pid"
if [[ -f "$CLEANUP_PID_FILE" ]] && kill -0 "$(cat "$CLEANUP_PID_FILE")" 2>/dev/null; then
    ok "Limpeza do buffer já rodando (PID $(cat "$CLEANUP_PID_FILE"))"
elif ! check_no_foreign_duplicate "Limpeza do buffer" "scripts/cleanup_loop.sh $BUFFER_ROOT" "$(cat "$CLEANUP_PID_FILE" 2>/dev/null || true)"; then
    :  # aviso já emitido, não sobe duplicata
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

# --- Retenção local dos clipes finais (S3) ----------------------------------
# Ao contrário do worker do Lara abaixo, não depende de nenhuma
# credencial externa — sobe sempre.
RETENTION_PID_FILE="$RUN_DIR/local_retention.pid"
if [[ -f "$RETENTION_PID_FILE" ]] && kill -0 "$(cat "$RETENTION_PID_FILE")" 2>/dev/null; then
    ok "Retenção local já rodando (PID $(cat "$RETENTION_PID_FILE"))"
elif ! check_no_foreign_duplicate "Retenção local" "-m scripts.local_retention_loop" "$(cat "$RETENTION_PID_FILE" 2>/dev/null || true)"; then
    :  # aviso já emitido, não sobe duplicata
else
    info "Subindo retenção local dos clipes finais (LOCAL_RAW_RETENTION_DAYS) ..."
    nohup "$PY" -m scripts.local_retention_loop >>"$LOG_DIR/local_retention.log" 2>&1 &
    echo $! > "$RETENTION_PID_FILE"
    sleep 1
    if kill -0 "$(cat "$RETENTION_PID_FILE")" 2>/dev/null; then
        ok "Retenção local no ar (PID $(cat "$RETENTION_PID_FILE")), log em logs/local_retention.log"
    else
        fail "Retenção local caiu ao subir — veja logs/local_retention.log"
    fi
fi

# --- Worker de integração com o Lara (se configurado) -----------------------
if [[ -z "${LARA_BASE_URL:-}" || -z "${REPLAY_API_TOKEN:-}" ]]; then
    [[ "$QUIET" == "1" ]] || info "LARA_BASE_URL/REPLAY_API_TOKEN não definidos — worker do Lara não sobe (integração ainda não configurada)."
else
    LARA_PID_FILE="$RUN_DIR/lara_worker.pid"
    if [[ -f "$LARA_PID_FILE" ]] && kill -0 "$(cat "$LARA_PID_FILE")" 2>/dev/null; then
        ok "Worker do Lara já rodando (PID $(cat "$LARA_PID_FILE"))"
    elif ! check_no_foreign_duplicate "Worker do Lara" "-m scripts.lara_worker" "$(cat "$LARA_PID_FILE" 2>/dev/null || true)"; then
        :  # aviso já emitido, não sobe duplicata
    else
        info "Subindo worker do Lara (sync/upload/heartbeat) ..."
        nohup "$PY" -m scripts.lara_worker >>"$LOG_DIR/lara_worker.log" 2>&1 &
        echo $! > "$LARA_PID_FILE"
        sleep 1
        if kill -0 "$(cat "$LARA_PID_FILE")" 2>/dev/null; then
            ok "Worker do Lara no ar (PID $(cat "$LARA_PID_FILE")), log em logs/lara_worker.log"
        else
            fail "Worker do Lara caiu ao subir — veja logs/lara_worker.log"
        fi
    fi
fi
