"""Aplica o overlay da plataforma externa no clipe.

O overlay já vem do tamanho do frame e é aplicado em (0,0), reescalado para a
resolução do clipe quando difere.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from clipper.clip_generator import probe_resolution
from db.models import Quadra


class OverlayApplicationError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise OverlayApplicationError(
            f"Comando falhou ({' '.join(cmd)}):\n{result.stderr}"
        )


def pick_overlay_path(quadra: Quadra) -> Path | None:
    """Devolve o overlay em cache (animado tem preferência) ou `None`."""
    animated = Path(quadra.overlay_animated_path) if quadra.overlay_animated_path else None
    png = Path(quadra.overlay_png_path) if quadra.overlay_png_path else None

    if animated is not None and animated.is_file():
        return animated
    if png is not None and png.is_file():
        return png
    return None


def apply_overlay(clip_path: Path, quadra: Quadra) -> Path:
    """Devolve `clip_path` intacto sem overlay; senão gera `<clip>_overlay.mp4`."""
    overlay_path = pick_overlay_path(quadra)
    if overlay_path is None:
        return clip_path

    width, height = probe_resolution(clip_path)
    output_path = clip_path.with_name(f"{clip_path.stem}_overlay.mp4")

    if overlay_path.suffix == ".webm":
        filter_complex = f"[1:v]scale={width}:{height}[ov];[0:v][ov]overlay=0:0:shortest=1"
        overlay_input = ["-stream_loop", "-1", "-i", str(overlay_path)]
    else:
        filter_complex = f"[1:v]scale={width}:{height}[ov];[0:v][ov]overlay=0:0"
        overlay_input = ["-i", str(overlay_path)]

    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
        "-i", str(clip_path),
        *overlay_input,
        "-filter_complex", filter_complex,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "copy",
        str(output_path),
    ]

    try:
        _run(cmd)
    except OverlayApplicationError:
        output_path.unlink(missing_ok=True)
        raise
    return output_path
