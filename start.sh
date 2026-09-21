#!/usr/bin/env bash
#
# start.sh — prepara o ambiente (venv + dependências), roda os testes
# padrão do repositório com log e feedback, e garante que a API, a
# captura de cada câmera de config/cameras.json e a limpeza do buffer
# (retenção fixa de 2min) estejam no ar.
#
# Idempotente: se a API, a captura de uma câmera ou a limpeza já estiverem
# rodando (via PID salvo em run/*.pid), não sobe duplicata.
#
# Uso: ./start.sh
#
set -uo pipefail   # sem -e de propósito: quero seguir e reportar falha de teste, não abortar o script

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
# Credenciais (LARA_BASE_URL, REPLAY_API_TOKEN, ADMIN_USERNAME,
# ADMIN_PASSWORD) e ajustes finos opcionais NÃO têm default de propósito
# (ver api/config.py) — sem isso, cada sessão de shell nova exigiria
# `export` manual antes de rodar este script, o que não sobrevive a
# reboot nem é prático em produção. Mesmo padrão já usado em
# config/cameras.json: arquivo real gitignored, `.env.example` versionado
# como modelo (`cp .env.example .env` e preencher). Valores que já vêm de
# variável de ambiente do chamador (ex.: systemd EnvironmentFile=, ou um
# `export` manual) continuam tendo prioridade — `.env` só preenche o que
# ainda não estiver setado.
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

# --- 3) Sobe a API real (se não estiver rodando) ------------------------
export BUFFER_ROOT="$ROOT/.data/buffer"
export OUTPUT_DIR="$ROOT/.data/output"
export CAMERAS_FILE="$ROOT/config/cameras.json"
export DATABASE_URL="sqlite:///$ROOT/.data/repet.db"   # lida por db/engine.py, ver README
export SEGMENT_TIME=2   # precisa ser o mesmo valor pra API e pra captura — fonte única aqui
BUFFER_MAX_AGE_MIN=2    # retenção do buffer bruto: só os últimos 2min, nada mais
mkdir -p "$BUFFER_ROOT" "$OUTPUT_DIR"

API_PID_FILE="$RUN_DIR/api.pid"
if [[ -f "$API_PID_FILE" ]] && kill -0 "$(cat "$API_PID_FILE")" 2>/dev/null; then
    ok "API já rodando (PID $(cat "$API_PID_FILE"))"
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
echo

# --- 4) Sobe a captura de cada câmera de cameras.json (se não estiver) --
info "Conferindo captura de cada câmera em config/cameras.json ..."
while IFS=$'\t' read -r quadra_id input_url; do
    [[ -z "$quadra_id" ]] && continue
    PID_FILE="$RUN_DIR/capture_${quadra_id}.pid"
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        ok "Captura de '$quadra_id' já rodando (PID $(cat "$PID_FILE"))"
        continue
    fi
    info "Subindo captura de '$quadra_id' ..."
    nohup bash capture/capture_camera.sh "$quadra_id" "$input_url" "$BUFFER_ROOT" "$SEGMENT_TIME" \
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
echo

# --- 5) Sobe o loop de limpeza do buffer (retenção fixa de 2min) --------
CLEANUP_PID_FILE="$RUN_DIR/cleanup.pid"
if [[ -f "$CLEANUP_PID_FILE" ]] && kill -0 "$(cat "$CLEANUP_PID_FILE")" 2>/dev/null; then
    ok "Limpeza do buffer já rodando (PID $(cat "$CLEANUP_PID_FILE"))"
else
    info "Subindo limpeza do buffer (retenção: últimos ${BUFFER_MAX_AGE_MIN}min) ..."
    nohup bash scripts/cleanup_loop.sh "$BUFFER_ROOT" "$BUFFER_MAX_AGE_MIN" 30 \
        >>"$LOG_DIR/cleanup.log" 2>&1 &
    echo $! > "$CLEANUP_PID_FILE"
    sleep 1
    if kill -0 "$(cat "$CLEANUP_PID_FILE")" 2>/dev/null; then
        ok "Limpeza do buffer no ar (PID $(cat "$CLEANUP_PID_FILE")), log em logs/cleanup.log"
    else
        fail "Limpeza do buffer caiu ao subir — veja logs/cleanup.log"
    fi
fi
echo

# --- 5.5) Sobe o worker de integração com o Lara (se configurado) ------
# LARA_BASE_URL e REPLAY_API_TOKEN não têm default aqui de propósito — são
# credencial/endereço de outro sistema, não algo pra inventar. Defina-os no
# ambiente antes de rodar ./start.sh (ex.: `export REPLAY_API_TOKEN=...`)
# quando a integração estiver pronta pra ligar; até lá, o worker fica fora
# do ar e o resto do sistema continua funcionando normalmente (PT-14/PT-15
# são Should/Must deste ciclo, mas nunca bloqueiam o botão — RNF9).
export OVERLAY_CACHE_DIR="$ROOT/.data/overlays"
mkdir -p "$OVERLAY_CACHE_DIR"

if [[ -z "${LARA_BASE_URL:-}" || -z "${REPLAY_API_TOKEN:-}" ]]; then
    info "LARA_BASE_URL/REPLAY_API_TOKEN não definidos — worker do Lara não sobe (integração ainda não configurada)."
else
    LARA_PID_FILE="$RUN_DIR/lara_worker.pid"
    if [[ -f "$LARA_PID_FILE" ]] && kill -0 "$(cat "$LARA_PID_FILE")" 2>/dev/null; then
        ok "Worker do Lara já rodando (PID $(cat "$LARA_PID_FILE"))"
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
echo

# --- 6) Resumo final ------------------------------------------------------
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
