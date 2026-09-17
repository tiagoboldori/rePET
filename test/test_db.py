"""
test_db.py — teste de fumaça da camada de persistência (PT-01).

Usa um SQLite próprio em arquivo temporário (via tmp_path do pytest), não
o .data/repet.db de dev — isolado e descartado a cada execução.
"""
from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine, select

from db import engine as engine_module
from db.models import Replay, ReplayStatus


def _make_engine(tmp_path):
    db_path = tmp_path / "test_repet.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_create_tables_and_roundtrip(tmp_path):
    engine = _make_engine(tmp_path)

    now = datetime.now()
    with Session(engine) as session:
        session.add(
            Replay(
                id="loc1-quadra1_20260917140000",
                quadra_id="loc1-quadra1",
                arquivo_bruto="loc1-quadra1_20260917140000.mp4",
                criado_em=now,
                duracao_segundos=35.0,
                tamanho_bytes=1_234_567,
            )
        )
        session.add(
            Replay(
                id="loc1-quadra1_20260917141500",
                quadra_id="loc1-quadra1",
                arquivo_bruto="loc1-quadra1_20260917141500.mp4",
                criado_em=now + timedelta(minutes=15),
                duracao_segundos=35.0,
                tamanho_bytes=1_234_000,
            )
        )
        session.add(
            Replay(
                id="loc1-quadra2_20260917140500",
                quadra_id="loc1-quadra2",
                arquivo_bruto="loc1-quadra2_20260917140500.mp4",
                criado_em=now + timedelta(minutes=5),
                duracao_segundos=35.0,
                tamanho_bytes=1_100_000,
            )
        )
        session.commit()

    with Session(engine) as session:
        replays = session.exec(
            select(Replay)
            .where(Replay.quadra_id == "loc1-quadra1")
            .order_by(Replay.criado_em.desc())
        ).all()

    assert [r.id for r in replays] == [
        "loc1-quadra1_20260917141500",
        "loc1-quadra1_20260917140000",
    ]
    assert all(r.estado == ReplayStatus.BRUTO for r in replays)
    assert all(r.arquivo_marcado is None for r in replays)


def test_create_db_and_tables_creates_replay(tmp_path, monkeypatch):
    """Cobre especificamente a função que os callers reais (futuro main.py)
    vão usar, não só o padrão create_all direto dos outros testes deste
    arquivo. Existe por causa de um bug real encontrado manualmente: chamar
    create_all sem antes importar db.models não cria nenhuma tabela, porque
    SQLModel.metadata só registra classes já importadas — por isso
    create_db_and_tables() importa db.models internamente."""
    db_path = tmp_path / "startup_repet.db"
    fresh_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(engine_module, "engine", fresh_engine)

    engine_module.create_db_and_tables()

    with fresh_engine.connect() as conn:
        tables = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    assert ("replay",) in tables


def test_index_exists(tmp_path):
    engine = _make_engine(tmp_path)
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA index_list('replay')").fetchall()
    index_names = {row[1] for row in rows}
    assert "ix_replay_quadra_id_criado_em" in index_names
