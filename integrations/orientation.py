"""
orientation.py — aplicação mecânica da orientação publicada pelo Lara no
clipe (`Quadra.orientation`: "vertical" 9:16 ou "horizontal" 16:9, ver
prompt do Lara / PLANO_DE_ACAO.md v3 seção 6). Este módulo nunca decide
nada — o Lara já resolveu qual orientação vale pra quadra, a única
decisão mecânica daqui é COMO chegar nesse aspect ratio a partir do que a
câmera entrega: corte centralizado (resolução e FPS continuam sendo
decisão nossa, o prompt só fala de orientação).

Sem `Quadra.orientation` ainda sincronizada (`None`) ou com valor
desconhecido, não mexe no clipe — mesmo princípio de "cai no default até
sincronizar" já usado por `clip_seconds` em `api/main.py`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from clipper.clip_generator import probe_resolution
from db.models import Quadra

# Diferença de aspect ratio pequena o bastante pra não valer o reencode
# (câmera já entrega quase exatamente o alvo — ex.: 16:9 nativo pedido
# como "horizontal").
_TOLERANCE = 0.01

_TARGET_RATIO = {
    "vertical": 9 / 16,
    "horizontal": 16 / 9,
}


class OrientationApplicationError(RuntimeError):
    pass


def orientation_applies(orientation: str | None) -> bool:
    """Decisão pura e barata (sem ffprobe): True só se `orientation` for
    um valor reconhecido (vertical/horizontal). Usado por
    `integrations/render.py` pra decidir se vale a pena sondar a
    resolução do clipe — sem isso, `render_clip` chamaria `probe_resolution`
    incondicionalmente, mesmo pra quadra sem orientação sincronizada
    ainda (regressão real encontrada ao escrever os testes deste módulo)."""
    return orientation in _TARGET_RATIO


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise OrientationApplicationError(
            f"Comando falhou ({' '.join(cmd)}):\n{result.stderr}"
        )


def _even(value: float) -> int:
    """H.264/yuv420p exige largura e altura pares."""
    return max(2, int(value) // 2 * 2)


def compute_crop(width: int, height: int, orientation: str | None) -> tuple[int, int] | None:
    """Decisão pura (sem ffmpeg): devolve o (crop_w, crop_h) a aplicar, ou
    `None` se nada precisa mudar (orientação não sincronizada/desconhecida,
    ou o frame já está dentro da tolerância do aspect ratio alvo). Extraído
    de `apply_orientation` pra ser reutilizável por `integrations/render.py`
    (fusão orientação+overlay num só passe de ffmpeg quando os dois se
    aplicam, ver docstring de lá)."""
    target_ratio = _TARGET_RATIO.get(orientation)
    if target_ratio is None:
        return None

    current_ratio = width / height
    diff = current_ratio - target_ratio

    if abs(diff) <= _TOLERANCE:
        return None
    elif diff > 0:
        # frame mais largo que o alvo -> corta as laterais, mantém altura
        return _even(height * target_ratio), height
    else:
        # frame mais alto/estreito que o alvo -> corta topo/base, mantém largura
        return width, _even(width / target_ratio)


def apply_orientation(clip_path: Path, quadra: Quadra) -> Path:
    """Se a orientação da quadra ainda não foi sincronizada, for
    desconhecida, ou o clipe já está (dentro da tolerância) no aspect
    ratio pedido, devolve `clip_path` sem tocar nele (sem reencode — caso
    comum quando a câmera já é nativamente do formato certo). Senão,
    corta centralizado pro aspect ratio alvo e devolve o novo arquivo
    (`<clip>_oriented.mp4`)."""
    if not orientation_applies(quadra.orientation):
        return clip_path

    width, height = probe_resolution(clip_path)
    crop = compute_crop(width, height, quadra.orientation)
    if crop is None:
        return clip_path
    crop_w, crop_h = crop

    output_path = clip_path.with_name(f"{clip_path.stem}_oriented.mp4")
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
        "-i", str(clip_path),
        "-vf", f"crop={crop_w}:{crop_h}",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "copy",
        str(output_path),
    ]

    try:
        _run(cmd)
    except OrientationApplicationError:
        output_path.unlink(missing_ok=True)
        raise
    return output_path
