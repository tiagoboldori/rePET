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


def load_cameras() -> dict[str, dict]:
    """Carrega config/cameras.json e devolve um dict indexado por quadra_id,
    pra validar rapidamente se um quadra_id que chegou no POST é conhecido."""
    if not CAMERAS_FILE.is_file():
        return {}
    cameras = json.loads(CAMERAS_FILE.read_text())
    return {cam["quadra_id"]: cam for cam in cameras}
