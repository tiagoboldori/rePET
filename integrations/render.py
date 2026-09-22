"""
render.py — orquestra orientação (integrations/orientation.py) e overlay
(integrations/overlay.py) sobre o clipe. Quando os DOIS se aplicam à mesma
quadra (achado numa auditoria de CPU em 2026-09-22: é o caso comum em
produção — câmeras entregam 4:3, quadra pede 16:9, e a maioria das quadras
tem overlay configurado), rodá-los em sequência paga dois passes completos
de decode+encode (orientation.py grava `_oriented.mp4`, overlay.py lê esse
arquivo de volta e grava `_oriented_overlay.mp4`) quando um só já resolve:
este módulo funde os dois filtros (`crop` + `scale`+`overlay`) num único
`filter_complex`, um decode e um encode só. Quando só um dos dois (ou
nenhum) se aplica, delega pro módulo correspondente sem nenhum custo
extra — a lógica de decisão (`compute_crop`/`pick_overlay_path`) é
compartilhada com eles, não duplicada.
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
    """Aplica orientação e/ou overlay conforme a config da quadra, no
    menor número de passes de ffmpeg possível. Sem nenhum dos dois
    aplicável, devolve `clip_path` sem tocar nele (sem reencode). Interface
    única pro chamador (upload_queue.py): falha de qualquer um dos passes
    delegados vira `RenderApplicationError` (mensagem original preservada),
    então quem chama `render_clip` não precisa conhecer os tipos de erro
    específicos de orientation.py/overlay.py."""
    # Só sonda a resolução (ffprobe) quando a orientação da quadra é um
    # valor reconhecido — senão `apply_orientation` já seria um no-op sem
    # nunca chamar probe_resolution, e chamar aqui incondicionalmente
    # adicionaria um ffprobe supérfluo no caminho comum de "orientação
    # ainda não sincronizada" (achado escrevendo os testes deste módulo).
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

    # scale usa crop_w/crop_h (a resolução JÁ cortada), não a original —
    # mesma regra de overlay.py (escalar pro tamanho real do vídeo que vai
    # ser servido, não pro tamanho antes do crop).
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
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "copy",
        str(output_path),
    ]

    try:
        _run(cmd)
    except RenderApplicationError:
        output_path.unlink(missing_ok=True)
        raise
    return output_path
