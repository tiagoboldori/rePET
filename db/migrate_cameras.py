"""Sincroniza Local/Esporte/Quadra a partir de config/cameras.json.

Roda a cada start da API (upsert por id); para mudar câmeras, edite o
cameras.json e reinicie. Local.nome é derivado do local_id, e entradas sem
"esporte" caem em "indefinido".
"""
import json
from pathlib import Path

from sqlmodel import Session

from db.models import Esporte, Local, Quadra


def _upsert(session: Session, model: type, id_: str, **fields) -> None:
    obj = session.get(model, id_)
    if obj is None:
        session.add(model(id=id_, **fields))
    else:
        for key, value in fields.items():
            setattr(obj, key, value)
        session.add(obj)


def sync_cameras_from_file(session: Session, cameras_file: Path) -> int:
    """Retorna quantas câmeras foram processadas (0 se o arquivo não existir)."""
    if not cameras_file.is_file():
        return 0

    cameras = json.loads(cameras_file.read_text())

    for cam in cameras:
        local_id = cam["local_id"]
        _upsert(session, Local, local_id, nome=local_id.capitalize())

        esporte_id = cam.get("esporte", "indefinido")
        _upsert(session, Esporte, esporte_id, nome=esporte_id.capitalize())

        _upsert(
            session,
            Quadra,
            cam["quadra_id"],
            local_id=local_id,
            esporte_id=esporte_id,
            nome=cam["nome"],
            input_url=cam["input_url"],
            situacao="ativa",
        )

    session.commit()
    return len(cameras)
