"""Sincroniza a configuração das câmeras com a plataforma externa.

Consulta `GET /cameras` e só baixa overlay novo e atualiza a `Quadra` quando o
`config_hash` muda. Roda só no worker de fundo, nunca no caminho do botão.
"""
from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from db.models import Quadra
from integrations.lara_client import CameraConfig, LaraClient, LaraClientError


def sync_once(session: Session, client: LaraClient, overlay_cache_dir: Path) -> int:
    """Retorna quantas quadras tiveram a configuração atualizada."""
    try:
        cameras = client.get_cameras()
    except LaraClientError as exc:
        print(f"[lara] aviso: falha ao consultar GET /cameras: {exc}")
        return 0

    changed = 0
    for cam in cameras:
        quadra = session.get(Quadra, cam.external_id)
        if quadra is None:
            print(
                f"[lara] aviso: câmera '{cam.external_id}' existe no Lara "
                "mas não em config/cameras.json — ignorada até ser cadastrada aqui também."
            )
            continue
        if quadra.lara_config_hash == cam.config_hash:
            continue  # nada mudou

        if not _update_overlay_cache(client, cam, quadra, overlay_cache_dir):
            continue  # não atualiza o hash, tenta de novo no próximo ciclo

        quadra.orientation = cam.orientation
        quadra.clip_seconds = cam.clip_seconds
        quadra.lara_config_hash = cam.config_hash
        session.add(quadra)
        changed += 1

    session.commit()
    return changed


def _update_overlay_cache(
    client: LaraClient, cam: CameraConfig, quadra: Quadra, cache_dir: Path
) -> bool:
    """Baixa os overlays e atualiza os caminhos na `quadra` (sem commit).
    Devolve False se o download falhar."""
    if cam.overlay is None:
        quadra.overlay_png_path = None
        quadra.overlay_animated_path = None
        return True

    try:
        animated_path = None
        if cam.overlay.animated_url:
            animated_path = cache_dir / f"{cam.external_id}.webm"
            client.download_to(cam.overlay.animated_url, animated_path)

        png_path = None
        if cam.overlay.png_url:
            png_path = cache_dir / f"{cam.external_id}.png"
            client.download_to(cam.overlay.png_url, png_path)
    except LaraClientError as exc:
        print(f"[lara] falha ao baixar overlay de '{cam.external_id}': {exc}")
        return False

    quadra.overlay_png_path = str(png_path) if png_path else None
    quadra.overlay_animated_path = str(animated_path) if animated_path else None
    return True
