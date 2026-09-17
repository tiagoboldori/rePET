"""
migrate_cameras.py — sincroniza Local/Esporte/Quadra a partir de
config/cameras.json (PT-10, "migração do registro em arquivo").

Idempotente: chamado a cada start da API (ver api/main.py), upsert por
identificador. cameras.json continua sendo a fonte editável — não existe
ainda gerenciamento de câmeras/locais/esportes via API (isso é PT-16 e
está deslocado pro ciclo seguinte pra locais/esportes, ver PLANO_DE_ACAO.md
seção 7.4); até lá, mudar nome/esporte/local é editar cameras.json e
reiniciar a API.

Simplificação aceita por enquanto: cameras.json não tem nome nem
observações de Local — Local.nome é derivado do local_id
(`local_id.capitalize()`). O campo "esporte" é novo neste pacote; entradas
sem ele caem em "indefinido".
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
    """Retorna o número de câmeras (quadras) processadas. Não faz nada
    (retorna 0) se cameras_file ainda não existir — acontece em dev antes
    de `cp cameras.example.json cameras.json`."""
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
