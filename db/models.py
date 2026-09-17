"""
models.py — modelos de persistência.

PT-01: Replay. PT-10: Local, Esporte, Quadra (com migração a partir de
config/cameras.json, ver db/migrate_cameras.py) — Quadra.id reusa o
`quadra_id` global já em uso hoje (evita rota aninhada nas páginas
públicas). Logo entra em PT-14; `Replay.logo_aplicada_id` fica sem FK
formal até lá.
"""
from datetime import datetime
from enum import Enum

from sqlmodel import Field, Index, SQLModel


class Local(SQLModel, table=True):
    id: str = Field(primary_key=True)
    nome: str
    observacoes: str | None = Field(default=None)


class Esporte(SQLModel, table=True):
    id: str = Field(primary_key=True)
    nome: str


class Quadra(SQLModel, table=True):
    """`id` é o identificador global já usado hoje em cameras.json/URLs
    públicas (ex.: "loc1-quadra1"), não um substituto novo."""

    id: str = Field(primary_key=True)
    local_id: str = Field(foreign_key="local.id")
    esporte_id: str = Field(foreign_key="esporte.id")
    nome: str
    input_url: str  # RTSP — nunca deve ser exposta em resposta de API
    situacao: str = Field(default="ativa")


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
    quadra_id: str = Field(foreign_key="quadra.id")
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
