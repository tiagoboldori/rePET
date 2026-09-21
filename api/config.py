"""
config.py — configuração da API, vinda de variáveis de ambiente (com
defaults razoáveis pra desenvolvimento local). Em produção, isso é setado
no unit systemd do serviço da API (ou num .env carregado por ele).
"""
import json
import os
from pathlib import Path

# Onde o buffer contínuo (tmpfs em produção) é escrito por capture_camera.sh
BUFFER_ROOT = Path(os.environ.get("BUFFER_ROOT", "/var/replay"))

# Onde os CLIPES FINAIS (já cortados) ficam salvos — disco persistente.
# É essa pasta que fica exposta publicamente em /clips/<arquivo>.mp4
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/var/replay/output"))

# Registro de câmeras conhecidas (fonte única de verdade)
CAMERAS_FILE = Path(
    os.environ.get(
        "CAMERAS_FILE",
        str(Path(__file__).resolve().parent.parent / "config" / "cameras.json"),
    )
)

CLIP_DURATION_SECONDS = float(os.environ.get("CLIP_DURATION_SECONDS", "35"))
SEGMENT_TIME = int(os.environ.get("SEGMENT_TIME", "2"))
SAFETY_MARGIN = float(os.environ.get("SAFETY_MARGIN", "0.5"))

# Intervalo mínimo (segundos) entre dois acionamentos da MESMA quadra.
# Uma segunda chamada antes disso passar recebe 429 em vez de disparar
# outro corte (protege contra clique duplo/repique do botão físico).
TRIGGER_COOLDOWN_SECONDS = float(os.environ.get("TRIGGER_COOLDOWN_SECONDS", "15"))

# Se o segmento fechado mais recente do buffer for mais velho que isso
# (câmera travada/desconectada, mas o capture_camera.sh ainda rodando),
# o corte falha em vez de devolver um clipe com conteúdo velho/errado.
MAX_STALENESS_SECONDS = float(
    os.environ.get("MAX_STALENESS_SECONDS", str(3 * SEGMENT_TIME + SAFETY_MARGIN + 5))
)

# --- Integração com o Lara (PT-14/PT-15, ver PLANO_DE_ACAO.md v3) ---------
# Base da API do Lara, ex.: https://lara.clube.example/api/replay
LARA_BASE_URL = os.environ.get("LARA_BASE_URL", "")
# Token pessoal Sanctum, gerado por `php artisan replay:token` do lado do
# Lara. Nunca versionar — só variável de ambiente (RNF8).
REPLAY_API_TOKEN = os.environ.get("REPLAY_API_TOKEN", "")

# Onde os overlays baixados do Lara ficam cacheados em disco local (um
# arquivo por quadra, hash já embutido no nome vindo da URL do Lara).
OVERLAY_CACHE_DIR = Path(os.environ.get("OVERLAY_CACHE_DIR", "/var/replay/overlays"))

# Intervalo (segundos) entre pulls de GET /cameras e entre heartbeats, por
# câmera. O contrato do Lara aceita 1-5 min; 120s fica no meio da faixa.
LARA_POLL_INTERVAL_SECONDS = float(os.environ.get("LARA_POLL_INTERVAL_SECONDS", "120"))
LARA_HEARTBEAT_INTERVAL_SECONDS = float(
    os.environ.get("LARA_HEARTBEAT_INTERVAL_SECONDS", "120")
)

# Intervalo entre execuções da fila de envio de clipes pendentes.
LARA_UPLOAD_POLL_INTERVAL_SECONDS = float(
    os.environ.get("LARA_UPLOAD_POLL_INTERVAL_SECONDS", "10")
)

# Retenção local do arquivo (bruto/com overlay), só para a página de teste
# `/quadra/{id}` — não é a entrega oficial ao sócio (essa é do Lara, 7
# dias). Proposto 3 dias (S3/RNF5 do PLANO_DE_ACAO.md v3), a confirmar.
LOCAL_RAW_RETENTION_DAYS = float(os.environ.get("LOCAL_RAW_RETENTION_DAYS", "3"))


def load_cameras() -> dict[str, dict]:
    """Carrega config/cameras.json e devolve um dict indexado por quadra_id,
    pra validar rapidamente se um quadra_id que chegou no POST é conhecido."""
    if not CAMERAS_FILE.is_file():
        return {}
    cameras = json.loads(CAMERAS_FILE.read_text())
    return {cam["quadra_id"]: cam for cam in cameras}
