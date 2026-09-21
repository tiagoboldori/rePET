"""
config_sync.py — worker de sincronização de configuração com o Lara
(PT-14, PLANO_DE_ACAO.md v3 seção 6, item 1). Consulta `GET /cameras`
periodicamente e só reprocessa (baixa overlay novo, atualiza a `Quadra`)
quando o `config_hash` de uma câmera mudar em relação ao último valor
salvo. Enquanto o hash for igual, não há nenhuma chamada de rede ou
escrita em disco adicional — é isso que torna o pull barato, conforme o
próprio contrato do Lara pede.

Nunca roda no caminho do acionamento do botão (RNF3/RNF8 do
PLANO_DE_ACAO.md v3) — é chamado só pelo worker de fundo
(scripts/lara_worker.py).
"""
from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from db.models import Quadra
from integrations.lara_client import CameraConfig, LaraClient, LaraClientError


def sync_once(session: Session, client: LaraClient, overlay_cache_dir: Path) -> int:
    """Retorna quantas quadras tiveram configuração atualizada nesta
    passada. Zero é o caso comum (nenhum config_hash mudou)."""
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
            continue  # nada mudou — pull barato, sem tocar em disco/DB

        if not _update_overlay_cache(client, cam, quadra, overlay_cache_dir):
            continue  # falha ao baixar overlay: não atualiza o hash, tenta de novo no próximo pull

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
    """Baixa overlay(s) novo(s) pra disco local e atualiza os caminhos na
    `quadra` (em memória, ainda não commitado). Devolve False em caso de
    falha de download — quem chama decide não avançar o config_hash nesse
    caso, pra tentar de novo no próximo ciclo."""
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
