"""
models.py — modelos de persistência.

PT-01: Replay. PT-10: Local, Esporte, Quadra (com migração a partir de
config/cameras.json, ver db/migrate_cameras.py) — Quadra.id reusa o
`quadra_id` global já em uso hoje (evita rota aninhada nas páginas
públicas). PT-14/PT-15: integração com o Lara (ver PLANO_DE_ACAO.md v3) —
`Quadra` ganha um espelho local, somente-leitura para o resto do sistema,
da configuração publicada pelo Lara; `Replay` ganha o estado de envio ao
Lara. Não existe mais entidade `Logo` — cadastro e hierarquia de logo são
responsabilidade do Lara.
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
    públicas (ex.: "loc1-quadra1"), não um substituto novo — e é o mesmo
    valor usado como `external_id` de câmera nas chamadas ao Lara."""

    id: str = Field(primary_key=True)
    local_id: str = Field(foreign_key="local.id")
    esporte_id: str = Field(foreign_key="esporte.id")
    nome: str
    input_url: str  # RTSP — nunca deve ser exposta em resposta de API nem enviada ao Lara (RNF10)
    situacao: str = Field(default="ativa")

    # Espelho local da configuração publicada pelo Lara (GET /cameras),
    # escrito só pelo worker de sincronização (integrations/config_sync.py)
    # — nunca editado por outra rota. Enquanto `lara_config_hash` for igual
    # ao último valor visto, nada aqui muda (é o que torna o pull barato).
    orientation: str | None = Field(default=None)  # "vertical" ou "horizontal"
    clip_seconds: int | None = Field(default=None)  # pré-roll pedido pelo Lara (5-60s)
    overlay_png_path: str | None = Field(default=None)  # cache local do overlay estático
    overlay_animated_path: str | None = Field(default=None)  # cache local do WebM animado, se houver
    lara_config_hash: str | None = Field(default=None)


class ReplayLaraStatus(str, Enum):
    PENDENTE = "pendente"
    ENVIADO = "enviado"
    FALHA = "falha"


class Replay(SQLModel, table=True):
    """Um registro por clipe gerado. `id` é o próprio nome do arquivo bruto
    sem extensão (<quadra_id>_<timestamp>, já gerado por clip_generator.py)
    — evita ID substituto desalinhado do arquivo real (RNF6: disco é a
    fonte de verdade, o banco é índice de acesso). Esse mesmo `id` é enviado
    como `external_id` do clipe ao Lara (RNF11: idempotência do envio)."""

    id: str = Field(primary_key=True)
    quadra_id: str = Field(foreign_key="quadra.id")
    arquivo_bruto: str
    arquivo_com_overlay: str | None = Field(default=None)
    criado_em: datetime = Field(default_factory=datetime.now)
    duracao_segundos: float
    tamanho_bytes: int

    # Estado do envio ao Lara (worker de fila, integrations/upload_queue.py).
    lara_status: ReplayLaraStatus = Field(default=ReplayLaraStatus.PENDENTE)
    lara_uuid: str | None = Field(default=None)
    lara_enviado_em: datetime | None = Field(default=None)
    lara_ultimo_erro: str | None = Field(default=None)

    # Backoff exponencial por item (prompt do Lara: "retente com backoff";
    # "429 -> backoff") — zerado em sucesso ou falha definitiva,
    # incrementado a cada falha transitória (ver integrations/upload_queue.py).
    lara_tentativas: int = Field(default=0)
    lara_proxima_tentativa_em: datetime | None = Field(default=None)

    __table_args__ = (
        Index("ix_replay_quadra_id_criado_em", "quadra_id", "criado_em"),
    )
