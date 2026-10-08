"""Configuração da API, lida de variáveis de ambiente (defaults para dev local)."""
import json
import os
from pathlib import Path

# Onde o buffer contínuo (tmpfs em produção) é escrito por capture_camera.sh
BUFFER_ROOT = Path(os.environ.get("BUFFER_ROOT", "/var/replay"))

# Clipes finais (já cortados), em disco persistente; servidos em /clips/<arquivo>.mp4
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/var/replay/output"))

# Registro de câmeras conhecidas
CAMERAS_FILE = Path(
    os.environ.get(
        "CAMERAS_FILE",
        str(Path(__file__).resolve().parent.parent / "config" / "cameras.json"),
    )
)

CLIP_DURATION_SECONDS = float(os.environ.get("CLIP_DURATION_SECONDS", "35"))
SEGMENT_TIME = int(os.environ.get("SEGMENT_TIME", "2"))
SAFETY_MARGIN = float(os.environ.get("SAFETY_MARGIN", "0.5"))

# Intervalo mínimo entre dois acionamentos da mesma quadra; antes disso
# a API responde 429 (evita clique duplo/repique do botão).
TRIGGER_COOLDOWN_SECONDS = float(os.environ.get("TRIGGER_COOLDOWN_SECONDS", "15"))

# Se o segmento mais recente do buffer for mais velho que isso (câmera
# travada com a captura ainda rodando), o corte falha em vez de gerar
# um clipe velho.
MAX_STALENESS_SECONDS = float(
    os.environ.get("MAX_STALENESS_SECONDS", str(3 * SEGMENT_TIME + SAFETY_MARGIN + 5))
)

# --- Integração com a plataforma externa ----------------------------------
# Base da API, ex.: https://lara.clube.example/api/replay
LARA_BASE_URL = os.environ.get("LARA_BASE_URL", "")
# Token Sanctum (`php artisan replay:token`); só via variável de ambiente.
REPLAY_API_TOKEN = os.environ.get("REPLAY_API_TOKEN", "")

# Cache local dos overlays baixados (um arquivo por quadra).
OVERLAY_CACHE_DIR = Path(os.environ.get("OVERLAY_CACHE_DIR", "/var/replay/overlays"))

# Intervalo (s) entre pulls de GET /cameras e entre heartbeats, por câmera.
# O contrato aceita 1-5 min.
LARA_POLL_INTERVAL_SECONDS = float(os.environ.get("LARA_POLL_INTERVAL_SECONDS", "120"))
LARA_HEARTBEAT_INTERVAL_SECONDS = float(
    os.environ.get("LARA_HEARTBEAT_INTERVAL_SECONDS", "120")
)

# Intervalo entre execuções da fila de envio.
LARA_UPLOAD_POLL_INTERVAL_SECONDS = float(
    os.environ.get("LARA_UPLOAD_POLL_INTERVAL_SECONDS", "10")
)

# Dias de retenção local dos clipes (a entrega oficial é da plataforma externa).
LOCAL_RAW_RETENTION_DAYS = float(os.environ.get("LOCAL_RAW_RETENTION_DAYS", "3"))

# --- Música de fundo (mixada localmente no clipe final) -------------------
# Pasta com as faixas; uma é sorteada a cada clipe. Pasta ausente ou
# vazia = sem música.
MUSIC_DIR = Path(
    os.environ.get(
        "MUSIC_DIR",
        str(Path(__file__).resolve().parent.parent / "assets" / "music"),
    )
)
MUSIC_VOLUME = float(os.environ.get("MUSIC_VOLUME", "0.5"))
MUSIC_FADE_SECONDS = float(os.environ.get("MUSIC_FADE_SECONDS", "1.5"))

# --- HTTP Basic dos endpoints de gerenciamento -----------------------------
# Sem default de propósito: enquanto não configurado, esses endpoints
# (hoje só DELETE /api/replays/{id}) respondem 401 a tudo.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def load_cameras() -> dict[str, dict]:
    """Carrega config/cameras.json, indexado por quadra_id."""
    if not CAMERAS_FILE.is_file():
        return {}
    cameras = json.loads(CAMERAS_FILE.read_text())
    return {cam["quadra_id"]: cam for cam in cameras}
