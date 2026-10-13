"""Engine SQLite e fábrica de sessões. Lê DATABASE_URL direto do ambiente.

Sem Alembic: create_all() só cria o que falta; colunas novas e nuláveis entram
em _add_missing_columns(), outras mudanças de schema exigem apagar .data/repet.db.
"""
import os
from collections.abc import Generator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine


def create_sqlite_engine(database_url: str):
    """Engine SQLite com WAL e foreign_keys ligados (usado também nos testes)."""
    eng = create_engine(database_url, connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return eng


DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///.data/repet.db")

engine = (
    create_sqlite_engine(DATABASE_URL)
    if DATABASE_URL.startswith("sqlite")
    else create_engine(DATABASE_URL)
)


def create_db_and_tables() -> None:
    # O import precisa estar aqui: sem ele o metadata fica vazio e
    # create_all não cria nenhuma tabela, sem erro.
    from db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """create_all não altera tabelas existentes; colunas novas e nuláveis entram aqui."""
    from sqlalchemy import inspect, text

    wanted = {"replay": {"lara_ftp_path": "VARCHAR"}}
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in wanted.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
