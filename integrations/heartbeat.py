"""Sinal de vida periódico por câmera. Falha só gera log."""
from __future__ import annotations

from sqlmodel import Session, select

from db.models import Quadra
from integrations.lara_client import LaraClient, LaraClientError


def send_all(session: Session, client: LaraClient) -> None:
    quadras = session.exec(select(Quadra).where(Quadra.situacao == "ativa")).all()
    for quadra in quadras:
        try:
            client.send_heartbeat(quadra.id)
        except LaraClientError as exc:
            print(f"[lara] heartbeat de '{quadra.id}' falhou (ignorado): {exc}")
