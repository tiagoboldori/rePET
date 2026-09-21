#!/usr/bin/env python3
"""
lara_diagnostic.py — diagnóstico da integração com o Lara (S2,
PLANO_DE_ACAO.md v3): chama `/ping` e mostra, por câmera, a configuração
em vigor no cache local e se o overlay já foi baixado. Entrega
explicitamente pedida pelo lado do Lara — só relata, não decide nada.

Uso:
    python -m scripts.lara_diagnostic
"""
from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from api import config
from db.engine import create_db_and_tables, engine
from db.models import Quadra
from integrations.lara_client import LaraClient, LaraClientError


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

    print("== /ping ==")
    try:
        client = LaraClient(config.LARA_BASE_URL, config.REPLAY_API_TOKEN)
        print(client.ping())
    except (LaraClientError, ValueError) as exc:
        print(f"FALHOU: {exc}")

    print()
    print("== Configuração em cache local (por quadra) ==")
    with Session(engine) as session:
        quadras = session.exec(select(Quadra)).all()
        if not quadras:
            print("(nenhuma quadra cadastrada localmente — rode a API pelo menos uma vez)")
        for q in quadras:
            print(
                f"- {q.id}: orientação={q.orientation or '?'} "
                f"clip_seconds={q.clip_seconds or '?'} "
                f"config_hash={q.lara_config_hash or '(nunca sincronizado)'} "
                f"overlay={_overlay_status(q)}"
            )


if __name__ == "__main__":
    main()
