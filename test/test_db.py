"""
test_db.py — testes da camada de persistência (PT-01) e da migração de
câmeras a partir de arquivo (PT-10).

Usa um SQLite próprio em arquivo temporário (via tmp_path do pytest), não
o .data/repet.db de dev — isolado e descartado a cada execução. Sempre via
create_sqlite_engine() (não create_engine puro), pra que os testes rodem
com os mesmos PRAGMAs (WAL, foreign_keys) que a aplicação real usa —
senão a integridade referencial testada abaixo não seria validada de
verdade.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from db import engine as engine_module
from db.migrate_cameras import sync_cameras_from_file
from db.models import Esporte, Local, Quadra, Replay, ReplayStatus


def _make_engine(tmp_path, name="test_repet.db"):
    db_path = tmp_path / name
    engine = engine_module.create_sqlite_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_quadra(session: Session, quadra_id: str, local_id: str = "loc1") -> None:
    """Cria a cadeia Local -> Esporte -> Quadra mínima pra satisfazer a FK
    de Replay.quadra_id."""
    if session.get(Local, local_id) is None:
        session.add(Local(id=local_id, nome=local_id.capitalize()))
    if session.get(Esporte, "futsal") is None:
        session.add(Esporte(id="futsal", nome="Futsal"))
    if session.get(Quadra, quadra_id) is None:
        session.add(
            Quadra(
                id=quadra_id,
                local_id=local_id,
                esporte_id="futsal",
                nome=quadra_id,
                input_url="rtsp://user:senha@10.0.0.1:554/stream1",
            )
        )
    session.commit()


def test_create_tables_and_roundtrip(tmp_path):
    engine = _make_engine(tmp_path)

    now = datetime.now()
    with Session(engine) as session:
        _seed_quadra(session, "loc1-quadra1")
        _seed_quadra(session, "loc1-quadra2")

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


def test_replay_requires_existing_quadra(tmp_path):
    """Integridade referencial de verdade: Replay.quadra_id agora tem FK
    formal pra Quadra (desde PT-10) — inserir um replay pra uma quadra que
    não existe deve falhar, não ficar órfão silenciosamente."""
    engine = _make_engine(tmp_path)

    with Session(engine) as session:
        session.add(
            Replay(
                id="quadra-fantasma_20260917140000",
                quadra_id="quadra-fantasma",
                arquivo_bruto="quadra-fantasma_20260917140000.mp4",
                duracao_segundos=35.0,
                tamanho_bytes=1,
            )
        )
        try:
            session.commit()
            raised = False
        except IntegrityError:
            raised = True
    assert raised


def test_create_db_and_tables_creates_replay(tmp_path, monkeypatch):
    """Cobre especificamente a função que os callers reais (main.py) usam,
    não só o padrão create_all direto dos outros testes deste arquivo.
    Existe por causa de um bug real encontrado manualmente: chamar
    create_all sem antes importar db.models não cria nenhuma tabela, porque
    SQLModel.metadata só registra classes já importadas — por isso
    create_db_and_tables() importa db.models internamente."""
    db_path = tmp_path / "startup_repet.db"
    fresh_engine = engine_module.create_sqlite_engine(f"sqlite:///{db_path}")
    monkeypatch.setattr(engine_module, "engine", fresh_engine)

    engine_module.create_db_and_tables()

    with fresh_engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"replay", "quadra", "local", "esporte"} <= tables


def test_index_exists(tmp_path):
    engine = _make_engine(tmp_path)
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA index_list('replay')").fetchall()
    index_names = {row[1] for row in rows}
    assert "ix_replay_quadra_id_criado_em" in index_names


def test_sync_cameras_from_file_is_idempotent(tmp_path):
    cameras_file = tmp_path / "cameras.json"
    cameras_file.write_text(
        json.dumps(
            [
                {
                    "quadra_id": "loc1-quadra1",
                    "local_id": "loc1",
                    "esporte": "futsal",
                    "nome": "Quadra 1",
                    "input_url": "rtsp://user:senha@10.0.1.11:554/stream1",
                },
                {
                    "quadra_id": "loc2-quadra1",
                    "local_id": "loc2",
                    "nome": "Quadra 1",
                    "input_url": "rtsp://user:senha@10.0.2.11:554/stream1",
                },
            ]
        )
    )
    engine = _make_engine(tmp_path, name="cameras_repet.db")

    with Session(engine) as session:
        count = sync_cameras_from_file(session, cameras_file)
        assert count == 2

        locais = session.exec(select(Local)).all()
        esportes = session.exec(select(Esporte)).all()
        quadras = session.exec(select(Quadra)).all()
        assert {loc.id for loc in locais} == {"loc1", "loc2"}
        assert {esp.id for esp in esportes} == {"futsal", "indefinido"}
        assert {q.id for q in quadras} == {"loc1-quadra1", "loc2-quadra1"}

        quadra1 = session.get(Quadra, "loc1-quadra1")
        assert quadra1.esporte_id == "futsal"
        assert quadra1.input_url == "rtsp://user:senha@10.0.1.11:554/stream1"

    # roda de novo com um dado alterado — precisa atualizar, não duplicar
    cameras_file.write_text(
        json.dumps(
            [
                {
                    "quadra_id": "loc1-quadra1",
                    "local_id": "loc1",
                    "esporte": "futsal",
                    "nome": "Quadra 1 (renomeada)",
                    "input_url": "rtsp://user:senha@10.0.1.99:554/stream1",
                },
            ]
        )
    )
    with Session(engine) as session:
        count = sync_cameras_from_file(session, cameras_file)
        assert count == 1

        quadras = session.exec(select(Quadra)).all()
        assert {q.id for q in quadras} == {"loc1-quadra1", "loc2-quadra1"}  # nada foi apagado

        quadra1 = session.get(Quadra, "loc1-quadra1")
        assert quadra1.nome == "Quadra 1 (renomeada)"
        assert quadra1.input_url == "rtsp://user:senha@10.0.1.99:554/stream1"


def test_sync_cameras_from_file_missing_file_returns_zero(tmp_path):
    engine = _make_engine(tmp_path, name="empty_repet.db")
    with Session(engine) as session:
        assert sync_cameras_from_file(session, tmp_path / "nao-existe.json") == 0
