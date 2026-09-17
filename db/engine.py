"""
engine.py — engine SQLite + fábrica de sessões pra camada de persistência.

Lê seu próprio env var (DATABASE_URL), no mesmo padrão que
capture_camera.sh/clip_generator.py já usam pros deles: cada módulo se
configura sozinho, sem depender de api/config.py (o worker assíncrono de
aplicação de logo, que não é a API, também vai precisar deste módulo).

Sem Alembic por enquanto: create_all() é idempotente e só cria o que
falta. Enquanto o schema mudar durante o ciclo de desenvolvimento atual, o
banco em .data/repet.db é descartável — apagar o arquivo e deixar recriar
é o fluxo esperado. Migração de verdade só entra quando houver dados de
produção a preservar.
"""
import os
from collections.abc import Generator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///.data/repet.db")

_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)


if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_db_and_tables() -> None:
    # Import obrigatório aqui: SQLModel.metadata só conhece as classes que já
    # foram importadas (corpo da classe executado) no momento do create_all;
    # sem isso, create_all roda silenciosamente sem criar nenhuma tabela.
    from db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
