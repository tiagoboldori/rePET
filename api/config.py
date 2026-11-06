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

# Quanto o corte espera, depois do botão, o segmento daquele instante fechar
# (o clipe termina no clique, não no último segmento já fechado). Estourou,
# termina no último segmento fechado. Precisa ser maior que o intervalo entre
# keyframes da câmera.
CLIP_WAIT_TIMEOUT_SECONDS = float(
    os.environ.get("CLIP_WAIT_TIMEOUT_SECONDS", str(2 * SEGMENT_TIME + 2))
)

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

# --- FTP do clube (destino dos clipes; o Lara só recebe o caminho) ---------
# Credenciais só via variável de ambiente (.env), nunca no código.
FTP_HOST = os.environ.get("FTP_HOST", "")
FTP_PORT = int(os.environ.get("FTP_PORT", "21"))
FTP_USER = os.environ.get("FTP_USER", "")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD", "")
# Pasta do Lara no FTP; o caminho avisado ao Lara é relativo a ela.
FTP_BASE_DIR = os.environ.get("FTP_BASE_DIR", "Lara/Repet")
FTP_TIMEOUT_SECONDS = float(os.environ.get("FTP_TIMEOUT_SECONDS", "60"))

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
MUSIC_VOLUME = float(os.environ.get("MUSIC_VOLUME", "0.4"))
MUSIC_FADE_SECONDS = float(os.environ.get("MUSIC_FADE_SECONDS", "1.5"))
# Volume do som ambiente (áudio da câmera), mantido por baixo da música.
# 0 = clipe só com a música.
AMBIENT_VOLUME = float(os.environ.get("AMBIENT_VOLUME", "0.4"))

# --- HTTP Basic dos endpoints de gerenciamento -----------------------------
# Sem default de propósito: enquanto não configurado, esses endpoints
# (hoje só DELETE /api/replays/{id}) respondem 401 a tudo.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


# --- Live no YouTube (POST /live/{quadra_id}) ------------------------------
# Chave única, enviada no header X-Live-Key, vale para todas as câmeras.
# Sem default de propósito: vazia = o endpoint responde 503. A stream key de
# cada quadra fica em `youtube_stream_key` no cameras.json.
LIVE_API_KEY = os.environ.get("LIVE_API_KEY", "")


def parse_api_tokens(raw: str) -> dict[str, str]:
    """`nome:token,nome2:token2` -> {nome: token}. O nome é só um rótulo (para saber de quem é
    cada chave e qual revogar); o token é o que vai em `Authorization: Bearer`. Entrada sem `:`,
    com nome ou token vazio é ignorada (e avisada no log, sem imprimir o conteúdo)."""
    tokens: dict[str, str] = {}
    for entry in filter(None, (e.strip() for e in (raw or "").split(","))):
        name, sep, token = entry.partition(":")
        name, token = name.strip(), token.strip()
        if not sep or not name or not token:
            print("[config] aviso: entrada inválida em API_TOKENS ignorada (use nome:token).")
            continue
        tokens[name] = token
    return tokens


# Chaves nomeadas para integrações (cada uma revogável sozinha, sem mexer na LIVE_API_KEY nem no
# REPLAY_API_TOKEN). Valem como `Authorization: Bearer` nas rotas /live, /broadcast e /status.
API_TOKENS = parse_api_tokens(os.environ.get("API_TOKENS", ""))
YOUTUBE_RTMP_URL = os.environ.get("YOUTUBE_RTMP_URL", "rtmp://a.rtmp.youtube.com/live2")
# Domínios (e seus subdomínios) que `PUT /quadras/{id}/youtube` aceita como destino RTMP de uma
# quadra, além do host de YOUTUBE_RTMP_URL. Existe porque o servidor e a câmera passam a empurrar
# o vídeo para onde esta URL apontar. IP só vale se estiver listado por inteiro.
RTMP_ALLOWED_HOSTS = [
    h.strip().lower().strip(".")
    for h in os.environ.get("RTMP_ALLOWED_HOSTS", "youtube.com").split(",")
    if h.strip().strip(".")
]


def rtmp_url_for(camera: dict) -> str:
    """URL RTMP de uma quadra: `rtmp_url` do cameras.json, senão a global YOUTUBE_RTMP_URL."""
    return camera.get("rtmp_url") or YOUTUBE_RTMP_URL
# Desliga a live na câmera depois desse tempo (0 = sem limite); o prazo é gravado
# em LIVE_STATE_FILE para sobreviver a reinício da API.
LIVE_MAX_SECONDS = float(os.environ.get("LIVE_MAX_SECONDS", "7200"))
LIVE_STATE_FILE = Path(
    os.environ.get("LIVE_STATE_FILE", str(Path(__file__).resolve().parent.parent / ".data" / "live_state.json"))
)

# --- Transmissão com placar (/broadcast/{quadra_id}) -------------------------
# O servidor reencoda (x264) e desenha o overlay; usa a mesma LIVE_API_KEY e o
# mesmo LIVE_MAX_SECONDS da live direta. Custo medido: ~1 núcleo por transmissão
# em ultrafast (~1,5 em veryfast). BROADCAST_THREADS limita os núcleos por transmissão.
_ROOT = Path(__file__).resolve().parent.parent
BROADCAST_MAX_CONCURRENT = int(os.environ.get("BROADCAST_MAX_CONCURRENT", "2"))
BROADCAST_PRESET = os.environ.get("BROADCAST_PRESET", "ultrafast")
BROADCAST_BITRATE = os.environ.get("BROADCAST_BITRATE", "4500k")  # sempre "<n>k"
BROADCAST_THREADS = int(os.environ.get("BROADCAST_THREADS", "4"))
BROADCAST_FONT = os.environ.get("BROADCAST_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
BROADCAST_VOLUME_STEP = int(os.environ.get("BROADCAST_VOLUME_STEP", "10"))  # % por clique
BROADCAST_VOLUME_MAX = int(os.environ.get("BROADCAST_VOLUME_MAX", "400"))  # % do original
BROADCAST_DIR = Path(os.environ.get("BROADCAST_DIR", str(_ROOT / ".data" / "broadcast")))
BROADCAST_STATE_FILE = Path(
    os.environ.get("BROADCAST_STATE_FILE", str(_ROOT / ".data" / "broadcast_state.json"))
)
BROADCAST_LOG_DIR = Path(os.environ.get("BROADCAST_LOG_DIR", str(_ROOT / "logs")))

# --- Verificação periódica das lives diretas (e na subida) --------------------
# Com que frequência a API pergunta às câmeras se estão empurrando RTMP.
LIVE_MONITOR_INTERVAL_SECONDS = float(os.environ.get("LIVE_MONITOR_INTERVAL_SECONDS", "15"))

# --- Painel web (/painel) ----------------------------------------------------
# Login com ADMIN_USERNAME/ADMIN_PASSWORD (os mesmos do DELETE). Sem eles o login
# responde 503. PANEL_SECRET é opcional: mistura-se à senha na chave que assina a
# sessão (trocar a senha ou o segredo desloga todo mundo).
PANEL_SECRET = os.environ.get("PANEL_SECRET", "")
PANEL_SESSION_HOURS = float(os.environ.get("PANEL_SESSION_HOURS", "12"))


def load_cameras() -> dict[str, dict]:
    """Carrega config/cameras.json, indexado por quadra_id."""
    if not CAMERAS_FILE.is_file():
        return {}
    cameras = json.loads(CAMERAS_FILE.read_text())
    return {cam["quadra_id"]: cam for cam in cameras}
