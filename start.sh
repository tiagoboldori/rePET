#!/usr/bin/env bash
#
# start.sh — prepara o ambiente (venv + dependências), roda os testes
# padrão do repositório com log e feedback, e garante que a API, a
# captura de cada câmera de config/cameras.json, a limpeza do buffer
# (retenção fixa de 2min), a retenção local dos clipes finais (S3) e,
# se configurado, o worker do Lara estejam no ar — além de um watchdog
# que reconfere tudo isso a cada 30s e reinicia sozinho o que cair.
#
# Idempotente: nada disso sobe duplicata do que já estiver rodando (via
# PID salvo em run/*.pid).
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

# --- 3) Sobe API, captura, limpeza, retenção local e worker do Lara -----
# LARA_BASE_URL/REPLAY_API_TOKEN/ADMIN_USERNAME/ADMIN_PASSWORD não têm
# default aqui de propósito — são credencial/endereço de outro sistema
# (ou credencial de admin), não algo pra inventar. Preencha `.env` (ver
# passo 0 acima) quando estiver pronto pra ligar; até lá, o worker do
# Lara fica fora do ar e o resto do sistema continua funcionando
# normalmente (RNF9 — nada disso bloqueia o botão).
export BUFFER_ROOT="$ROOT/.data/buffer"
export OUTPUT_DIR="$ROOT/.data/output"
export CAMERAS_FILE="$ROOT/config/cameras.json"
export DATABASE_URL="sqlite:///$ROOT/.data/repet.db"   # lida por db/engine.py, ver README
export SEGMENT_TIME=2   # precisa ser o mesmo valor pra API e pra captura — fonte única aqui
export OVERLAY_CACHE_DIR="$ROOT/.data/overlays"
mkdir -p "$BUFFER_ROOT" "$OUTPUT_DIR" "$OVERLAY_CACHE_DIR"

info "Conferindo/subindo serviços (API, captura, limpeza, retenção local, worker do Lara) ..."
ROOT="$ROOT" bash "$ROOT/scripts/ensure_services.sh"
echo

# --- 4) Sobe o watchdog (supervisão contínua, ver seção "Watchdog" no README) --
# Checagem extra (não só kill -0 no PID salvo): ver check_no_foreign_duplicate
# em scripts/ensure_services.sh — mesmo raciocínio se aplica aqui, com um
# agravante: o watchdog É o processo que sobe tudo mais. Dois watchdogs
# concorrentes de usuários diferentes (achado real em 2026-09-22, ver
# memória do projeto) é justamente o que causou dezenas de captura
# duplicada — pgrep -f enxerga processo de outro usuário mesmo quando
# kill -0 não consegue (sem permissão de sinal entre usuários diferentes).
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
