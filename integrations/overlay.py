"""
overlay.py — aplicação mecânica do overlay do Lara no clipe (PT-15,
PLANO_DE_ACAO.md v3 seção 6, item 3). Este módulo nunca decide nada: o
Lara já resolveu qual overlay vale para a quadra e o compôs pronto,
tamanho cheio do frame, para aplicar em (0,0). A única decisão daqui é
mecânica — reescalar para a resolução real do clipe quando ela difere de
overlay.width/overlay.height (mesma proporção, sem distorção).
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
    """Decisão pura (sem ffmpeg): devolve o arquivo de overlay a aplicar
    (preferindo o animado, conforme o contrato do Lara), ou `None` se a
    quadra não tem overlay em cache local. Extraído de `apply_overlay` pra
    ser reutilizável por `integrations/render.py` (fusão orientação+overlay
    num só passe de ffmpeg quando os dois se aplicam, ver docstring de lá)."""
    animated = Path(quadra.overlay_animated_path) if quadra.overlay_animated_path else None
    png = Path(quadra.overlay_png_path) if quadra.overlay_png_path else None

    if animated is not None and animated.is_file():
        return animated
    if png is not None and png.is_file():
        return png
    return None


def apply_overlay(clip_path: Path, quadra: Quadra) -> Path:
    """Se a quadra não tiver overlay em cache local, devolve `clip_path`
    sem tocar nele (sem reencode — caso comum). Senão, queima o overlay
    (preferindo o animado quando presente, conforme o contrato do Lara) e
    devolve o novo arquivo (`<clip>_overlay.mp4`). É o único ponto em que
    o corte volta a pagar reencode depois da otimização `-c copy`."""
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
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "copy",
        str(output_path),
    ]

    try:
        _run(cmd)
    except OverlayApplicationError:
        output_path.unlink(missing_ok=True)
        raise
    return output_path
