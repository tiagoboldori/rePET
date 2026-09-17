"""
models.py — modelo Replay (PT-01). Local, Esporte, Quadra e Logo entram em
PT-10/PT-14; quadra_id e logo_aplicada_id ficam sem FK formal até lá (ver
PLANEJAMENTO.md, pacote PT-01).
"""
from datetime import datetime
from enum import Enum

from sqlmodel import Field, Index, SQLModel


class ReplayStatus(str, Enum):
    BRUTO = "bruto"
    PROCESSANDO = "processando"
    MARCADO = "marcado"
    FALHA = "falha"


class Replay(SQLModel, table=True):
    """Um registro por clipe gerado. `id` é o próprio nome do arquivo bruto
    sem extensão (<quadra_id>_<timestamp>, já gerado por clip_generator.py)
    — evita ID substituto desalinhado do arquivo real (RNF6: disco é a
    fonte de verdade, o banco é índice de acesso)."""

    id: str = Field(primary_key=True)
    quadra_id: str
    arquivo_bruto: str
    arquivo_marcado: str | None = Field(default=None)
    logo_aplicada_id: str | None = Field(default=None)
    estado: ReplayStatus = Field(default=ReplayStatus.BRUTO)
    criado_em: datetime = Field(default_factory=datetime.now)
    duracao_segundos: float
    tamanho_bytes: int

    __table_args__ = (
        Index("ix_replay_quadra_id_criado_em", "quadra_id", "criado_em"),
    )
