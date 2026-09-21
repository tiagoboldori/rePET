#!/usr/bin/env python3
"""
lara_diagnostic.py — diagnóstico da integração com o Lara (S2,
PLANO_DE_ACAO.md v3): chama `/ping` e `/cameras` (ao vivo, não só o cache
local) e mostra, por câmera, a configuração publicada AGORA pelo Lara ao
lado da que está em vigor no cache local (útil pra flagrar sync
atrasado/pendente) e se o overlay já foi baixado. Entrega explicitamente
pedida pelo lado do Lara — só relata, não decide nada.

Uso:
    python -m scripts.lara_diagnostic
"""
from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from api import config
from db.engine import create_db_and_tables, engine
from db.models import Quadra
from integrations.lara_client import CameraConfig, LaraClient, LaraClientError


def _overlay_status(quadra: Quadra) -> str:
    if quadra.overlay_animated_path and Path(quadra.overlay_animated_path).is_file():
        return f"animado em cache ({quadra.overlay_animated_path})"
    if quadra.overlay_png_path and Path(quadra.overlay_png_path).is_file():
        return f"estático em cache ({quadra.overlay_png_path})"
    if quadra.overlay_png_path or quadra.overlay_animated_path:
        return "configurado no Lara, mas arquivo local ausente (falha de download?)"
    return "sem overlay"


def main() -> None:
    create_db_and_tables()  # idempotente — cobre rodar isolado, sem a API já ter subido

    try:
        client = LaraClient(config.LARA_BASE_URL, config.REPLAY_API_TOKEN)
    except ValueError as exc:
        print(f"FALHOU: {exc}")
        return

    print("== /ping ==")
    try:
        print(client.ping())
    except LaraClientError as exc:
        print(f"FALHOU: {exc}")

    print()
    print("== /cameras (ao vivo, publicado agora pelo Lara) ==")
    cameras_lara: dict[str, CameraConfig] = {}
    try:
        for cam in client.get_cameras():
            cameras_lara[cam.external_id] = cam
            overlay = "sem overlay" if cam.overlay is None else (
                f"animated_url={'sim' if cam.overlay.animated_url else 'não'} "
                f"png_url={'sim' if cam.overlay.png_url else 'não'}"
            )
            print(
                f"- {cam.external_id}: orientação={cam.orientation} "
                f"clip_seconds={cam.clip_seconds} config_hash={cam.config_hash} {overlay}"
            )
    except LaraClientError as exc:
        print(f"FALHOU: {exc}")

    print()
    print("== Cache local (por quadra) — e se está sincronizado com o Lara acima ==")
    with Session(engine) as session:
        quadras = session.exec(select(Quadra)).all()
        if not quadras:
            print("(nenhuma quadra cadastrada localmente — rode a API pelo menos uma vez)")
        for q in quadras:
            lara_hash = cameras_lara.get(q.id).config_hash if q.id in cameras_lara else None
            if q.id not in cameras_lara:
                sync_status = "câmera não existe no /cameras do Lara agora (cadastro pendente?)"
            elif lara_hash == q.lara_config_hash:
                sync_status = "sincronizado"
            else:
                sync_status = "DESATUALIZADO — Lara publicou config_hash novo, worker ainda não pegou"
            print(
                f"- {q.id}: orientação={q.orientation or '?'} "
                f"clip_seconds={q.clip_seconds or '?'} "
                f"config_hash={q.lara_config_hash or '(nunca sincronizado)'} "
                f"overlay={_overlay_status(q)} [{sync_status}]"
            )


if __name__ == "__main__":
    main()
