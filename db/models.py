"""Modelos de persistência: Local, Esporte, Quadra e Replay."""
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
    """`id` é o quadra_id de cameras.json (ex.: "loc1-quadra1"), também usado
    como `external_id` da câmera na plataforma externa."""

    id: str = Field(primary_key=True)
    local_id: str = Field(foreign_key="local.id")
    esporte_id: str = Field(foreign_key="esporte.id")
    nome: str
    input_url: str  # RTSP; não expor em resposta de API nem enviar à plataforma externa
    situacao: str = Field(default="ativa")

    # Espelho da configuração vinda de GET /cameras, escrito só pelo worker
    # de sincronização (integrations/config_sync.py). Se `lara_config_hash`
    # não mudou, nada aqui muda.
    orientation: str | None = Field(default=None)  # "vertical" ou "horizontal"
    clip_seconds: int | None = Field(default=None)  # pré-roll pedido (5-60s)
    overlay_png_path: str | None = Field(default=None)  # cache local do overlay estático
    overlay_animated_path: str | None = Field(default=None)  # cache local do WebM animado, se houver
    lara_config_hash: str | None = Field(default=None)


class ReplayLaraStatus(str, Enum):
    PENDENTE = "pendente"
    ENVIADO = "enviado"
    FALHA = "falha"


class Replay(SQLModel, table=True):
    """Um registro por clipe. `id` é o nome do arquivo bruto sem extensão
    (<quadra_id>_<timestamp>) e também o `external_id` do clipe no envio,
    o que torna o reenvio idempotente."""

    id: str = Field(primary_key=True)
    quadra_id: str = Field(foreign_key="quadra.id")
    arquivo_bruto: str
    # Versão final com orientação (crop) e/ou overlay aplicados; None se
    # nenhum dos dois se aplica.
    arquivo_processado: str | None = Field(default=None)
    criado_em: datetime = Field(default_factory=datetime.now)
    duracao_segundos: float
    tamanho_bytes: int

    # Estado do envio (integrations/upload_queue.py).
    lara_status: ReplayLaraStatus = Field(default=ReplayLaraStatus.PENDENTE)
    lara_uuid: str | None = Field(default=None)
    lara_enviado_em: datetime | None = Field(default=None)
    lara_ultimo_erro: str | None = Field(default=None)

    # Backoff exponencial: zera em sucesso ou falha definitiva, incrementa
    # a cada falha transitória.
    lara_tentativas: int = Field(default=0)
    lara_proxima_tentativa_em: datetime | None = Field(default=None)

    __table_args__ = (
        Index("ix_replay_quadra_id_criado_em", "quadra_id", "criado_em"),
    )
