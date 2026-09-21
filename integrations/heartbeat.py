"""
heartbeat.py — sinal de vida periódico por câmera (S1, PLANO_DE_ACAO.md
v3). Falha nunca interrompe nada além de logar (RNF9) — o heartbeat só
alimenta o "último contato" do lado do Lara, usado pelo Marketing para
descobrir câmera muda antes de o sócio reclamar.
"""
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
