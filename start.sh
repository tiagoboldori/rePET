#!/usr/bin/env bash
#
# start.sh — prepara o venv, roda os testes e sobe a API, a captura de cada
# câmera, a limpeza do buffer, a retenção local, o worker de integração
# (se configurado) e um watchdog que reinicia o que cair.
#
# Idempotente: não sobe duplicata do que já roda (PIDs em run/*.pid).
#
# Uso: ./start.sh
#
set -uo pipefail   # sem -e: falha de teste é reportada, não aborta o script

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/logs"
RUN_DIR="$ROOT/run"
mkdir -p "$LOG_DIR" "$RUN_DIR"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"

ok()   { echo "[OK]      $*"; }
fail() { echo "[FALHOU]  $*"; }
info() { echo "[INFO]    $*"; }

echo "== rePET start.sh =="
echo

# --- 0) Carrega .env local, se existir ----------------------------------
# Credenciais e ajustes opcionais vêm do .env (modelo em .env.example).
# Variável já exportada no ambiente tem prioridade; o .env só preenche o
# que falta.
if [[ -f "$ROOT/.env" ]]; then
    info "Carregando $ROOT/.env (variável já exportada no ambiente tem prioridade) ..."
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        if [[ -z "${!key:-}" ]]; then
            export "$key=$value"
        fi
    done < <(grep -vE '^\s*(#|$)' "$ROOT/.env")
fi

# --- 1) Ambiente Python -----------------------------------------------
if [[ ! -x "$PY" ]]; then
    info "Criando venv em .venv/ ..."
    if ! python3 -m venv "$VENV" >>"$LOG_DIR/setup.log" 2>&1; then
        fail "Não consegui criar o venv (veja $LOG_DIR/setup.log)."
        fail "Provável causa: falta o pacote de venv do sistema (ex: sudo apt install python3-venv)."
        exit 1
    fi
fi

info "Instalando dependências (requirements.txt) ..."
if ! "$VENV/bin/pip" install -q -r requirements.txt >>"$LOG_DIR/setup.log" 2>&1; then
    fail "pip install falhou (veja $LOG_DIR/setup.log)"
    exit 1
fi
ok "Ambiente Python pronto (.venv/)"

if ! command -v ffmpeg >/dev/null 2>&1; then
    fail "ffmpeg não encontrado no PATH. Instale com: sudo apt install ffmpeg"
    exit 1
fi
ok "ffmpeg encontrado ($(command -v ffmpeg))"
echo

# --- 2) Testes padrão ---------------------------------------------------
info "Rodando testes padrão (cada um leva ~1min, logs em logs/test_*.log) ..."
TESTS_OK=1

if "$PY" -m pytest test/ >"$LOG_DIR/test_pytest.log" 2>&1; then
    ok "pytest test/ (camada de persistência)"
else
    fail "pytest test/ (veja $LOG_DIR/test_pytest.log)"
    TESTS_OK=0
fi

if PATH="$VENV/bin:$PATH" bash test/run_pipeline_test.sh >"$LOG_DIR/test_pipeline.log" 2>&1; then
    ok "test/run_pipeline_test.sh"
else
    fail "test/run_pipeline_test.sh (veja $LOG_DIR/test_pipeline.log)"
    TESTS_OK=0
fi

if PATH="$VENV/bin:$PATH" bash test/run_api_test.sh >"$LOG_DIR/test_api.log" 2>&1; then
    ok "test/run_api_test.sh"
else
    fail "test/run_api_test.sh (veja $LOG_DIR/test_api.log)"
    TESTS_OK=0
fi

if [[ "$TESTS_OK" -eq 1 ]]; then
    ok "Todos os testes padrão passaram."
else
    fail "Pelo menos um teste padrão falhou — confira os logs acima antes de confiar no serviço."
fi
echo

# --- 3) Sobe API, captura, limpeza, retenção local e worker -------------
# Sem as credenciais no .env o worker de integração não sobe; o resto
# funciona normalmente.
export BUFFER_ROOT="$ROOT/.data/buffer"
export OUTPUT_DIR="$ROOT/.data/output"
export CAMERAS_FILE="$ROOT/config/cameras.json"
export DATABASE_URL="sqlite:///$ROOT/.data/repet.db"   # lida por db/engine.py
export SEGMENT_TIME=2   # API e captura precisam usar o mesmo valor
export OVERLAY_CACHE_DIR="$ROOT/.data/overlays"
mkdir -p "$BUFFER_ROOT" "$OUTPUT_DIR" "$OVERLAY_CACHE_DIR"

info "Conferindo/subindo serviços (API, captura, limpeza, retenção local, worker do Lara) ..."
ROOT="$ROOT" bash "$ROOT/scripts/ensure_services.sh"
echo

# --- 4) Sobe o watchdog ---------------------------------------------------
# Além do kill -0 no PID salvo, usa pgrep -f, que enxerga processos de
# outros usuários (kill -0 não). Dois watchdogs concorrentes duplicariam
# as capturas.
WATCHDOG_PID_FILE="$RUN_DIR/watchdog.pid"
WATCHDOG_FOREIGN="$(pgrep -f -- "scripts/watchdog_loop.sh" 2>/dev/null | grep -vx "$(cat "$WATCHDOG_PID_FILE" 2>/dev/null || true)" || true)"
if [[ -f "$WATCHDOG_PID_FILE" ]] && kill -0 "$(cat "$WATCHDOG_PID_FILE")" 2>/dev/null; then
    ok "Watchdog já rodando (PID $(cat "$WATCHDOG_PID_FILE"))"
elif [[ -n "$WATCHDOG_FOREIGN" ]]; then
    fail "Watchdog: já existe processo rodando fora do run/watchdog.pid esperado (PID(s): $(echo "$WATCHDOG_FOREIGN" | tr '\n' ' ')) — provável duplicata de outra sessão/usuário. NÃO vou subir mais um. Confira o dono ('ps -o pid,user,cmd -p <PID>') e mate manualmente ('sudo kill <PID>' se for de outro usuário) antes de rodar de novo."
else
    info "Subindo watchdog (reconfere os serviços acima a cada 30s, reinicia o que cair) ..."
    nohup env ROOT="$ROOT" bash "$ROOT/scripts/watchdog_loop.sh" 30 \
        >>"$LOG_DIR/watchdog.log" 2>&1 &
    echo $! > "$WATCHDOG_PID_FILE"
    sleep 1
    if kill -0 "$(cat "$WATCHDOG_PID_FILE")" 2>/dev/null; then
        ok "Watchdog no ar (PID $(cat "$WATCHDOG_PID_FILE")), log em logs/watchdog.log"
    else
        fail "Watchdog caiu ao subir — veja logs/watchdog.log"
    fi
fi
echo

# --- 5) Resumo final ------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP="${IP:-127.0.0.1}"

echo "===================================================================="
echo " rePET no ar"
echo "   Health:  http://${IP}:8000/health"
"$PY" -c "
import json
with open('$CAMERAS_FILE') as f:
    for c in json.load(f):
        print(f\"   Quadra:  http://${IP}:8000/quadra/{c['quadra_id']}  ({c['nome']})\")
"
echo "   Logs em:      $LOG_DIR/"
echo "   PIDs salvos:  $RUN_DIR/ (pare com: kill \$(cat run/api.pid) etc.)"
echo "===================================================================="
