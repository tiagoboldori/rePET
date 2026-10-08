"""Corta o clipe para o aspect ratio da orientação da quadra (vertical 9:16 ou horizontal 16:9).

Sem orientação sincronizada ou com valor desconhecido, o clipe não é alterado.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from clipper.clip_generator import probe_resolution
from db.models import Quadra

# Diferença de aspect ratio pequena demais pra valer o reencode.
_TOLERANCE = 0.01

_TARGET_RATIO = {
    "vertical": 9 / 16,
    "horizontal": 16 / 9,
}


class OrientationApplicationError(RuntimeError):
    pass


def orientation_applies(orientation: str | None) -> bool:
    """True se a orientação é reconhecida; evita ffprobe desnecessário em render.py."""
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
    """Devolve (crop_w, crop_h), ou `None` se não há nada a cortar."""
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
    """Devolve `clip_path` intacto se não precisa cortar; senão gera
    `<clip>_oriented.mp4` com corte centralizado."""
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
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy",
        str(output_path),
    ]

    try:
        _run(cmd)
    except OrientationApplicationError:
        output_path.unlink(missing_ok=True)
        raise
    return output_path
