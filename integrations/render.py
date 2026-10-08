"""Aplica orientação e overlay no clipe.

Quando os dois se aplicam, funde crop e overlay num único passe de ffmpeg (um
decode e um encode); com só um deles, delega para o módulo correspondente.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from clipper.clip_generator import probe_resolution
from db.models import Quadra
from integrations.orientation import (
    OrientationApplicationError,
    compute_crop,
    orientation_applies,
    apply_orientation,
)
from integrations.overlay import OverlayApplicationError, pick_overlay_path, apply_overlay


class RenderApplicationError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RenderApplicationError(
            f"Comando falhou ({' '.join(cmd)}):\n{result.stderr}"
        )


def render_clip(clip_path: Path, quadra: Quadra) -> Path:
    """Aplica orientação e/ou overlay no menor número de passes possível.
    Erros dos módulos delegados viram `RenderApplicationError`."""
    # só sonda a resolução quando a orientação é reconhecida
    crop = None
    if orientation_applies(quadra.orientation):
        width, height = probe_resolution(clip_path)
        crop = compute_crop(width, height, quadra.orientation)
    overlay_path = pick_overlay_path(quadra)

    try:
        if crop is not None and overlay_path is not None:
            return _apply_combined(clip_path, crop, overlay_path)
        if crop is not None:
            return apply_orientation(clip_path, quadra)
        if overlay_path is not None:
            return apply_overlay(clip_path, quadra)
        return clip_path
    except (OrientationApplicationError, OverlayApplicationError) as exc:
        raise RenderApplicationError(str(exc)) from exc


def _apply_combined(clip_path: Path, crop: tuple[int, int], overlay_path: Path) -> Path:
    crop_w, crop_h = crop
    output_path = clip_path.with_name(f"{clip_path.stem}_oriented_overlay.mp4")

    if overlay_path.suffix == ".webm":
        overlay_input = ["-stream_loop", "-1", "-i", str(overlay_path)]
        shortest = ":shortest=1"
    else:
        overlay_input = ["-i", str(overlay_path)]
        shortest = ""

    # o scale usa a resolução já cortada, não a original
    filter_complex = (
        f"[0:v]crop={crop_w}:{crop_h}[cropped];"
        f"[1:v]scale={crop_w}:{crop_h}[ov];"
        f"[cropped][ov]overlay=0:0{shortest}"
    )
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
    except RenderApplicationError:
        output_path.unlink(missing_ok=True)
        raise
    return output_path
