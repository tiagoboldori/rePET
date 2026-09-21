"""
test_local_retention.py — testes da retenção local dos clipes finais (S3,
db/local_retention.py). Não é a entrega oficial ao sócio (essa é do
Lara) — só evita que OUTPUT_DIR cresça sem limite.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, select

from db import engine as engine_module
from db import local_retention
from db.models import Esporte, Local, Quadra, Replay


def _make_engine(tmp_path, name="test_repet.db"):
    db_path = tmp_path / name
    engine = engine_module.create_sqlite_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_quadra(session: Session, quadra_id: str = "loc1-quadra1") -> None:
    session.add(Local(id="loc1", nome="Loc1"))
    session.add(Esporte(id="futsal", nome="Futsal"))
    session.add(
        Quadra(
            id=quadra_id,
            local_id="loc1",
            esporte_id="futsal",
            nome=quadra_id,
            input_url="rtsp://user:senha@10.0.0.1:554/stream1",
        )
    )
    session.commit()


def _add_replay(session: Session, output_dir, replay_id: str, idade_dias: float, com_overlay: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    bruto = output_dir / f"{replay_id}.mp4"
    bruto.write_bytes(b"x")
    overlay_name = None
    if com_overlay:
        overlay_name = f"{replay_id}_overlay.mp4"
        (output_dir / overlay_name).write_bytes(b"x")

    session.add(
        Replay(
            id=replay_id,
            quadra_id="loc1-quadra1",
            arquivo_bruto=bruto.name,
            arquivo_com_overlay=overlay_name,
            criado_em=datetime.now() - timedelta(days=idade_dias),
            duracao_segundos=35.0,
            tamanho_bytes=1,
        )
    )
    session.commit()


def test_purge_removes_expired_but_keeps_recent(tmp_path):
    output_dir = tmp_path / "output"
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session)
        _add_replay(session, output_dir, "loc1-quadra1_velho", idade_dias=5)
        _add_replay(session, output_dir, "loc1-quadra1_recente", idade_dias=1)

        removidos = local_retention.purge_expired_replays(session, output_dir, retention_days=3)
        assert removidos == 1

        ids_restantes = {r.id for r in session.exec(select(Replay)).all()}
        assert ids_restantes == {"loc1-quadra1_recente"}
        assert not (output_dir / "loc1-quadra1_velho.mp4").exists()
        assert (output_dir / "loc1-quadra1_recente.mp4").exists()


def test_purge_removes_overlay_file_too(tmp_path):
    output_dir = tmp_path / "output"
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session)
        _add_replay(session, output_dir, "loc1-quadra1_velho", idade_dias=10, com_overlay=True)

        removidos = local_retention.purge_expired_replays(session, output_dir, retention_days=3)
        assert removidos == 1
        assert not (output_dir / "loc1-quadra1_velho.mp4").exists()
        assert not (output_dir / "loc1-quadra1_velho_overlay.mp4").exists()


def test_purge_is_idempotent(tmp_path):
    output_dir = tmp_path / "output"
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session)
        _add_replay(session, output_dir, "loc1-quadra1_velho", idade_dias=10)

        assert local_retention.purge_expired_replays(session, output_dir, retention_days=3) == 1
        assert local_retention.purge_expired_replays(session, output_dir, retention_days=3) == 0


def test_purge_ignores_lara_status(tmp_path):
    """Expira por idade mesmo se nunca foi enviado ao Lara com sucesso —
    não é a entrega oficial, a fila de envio já trata arquivo ausente
    como falha não-crítica (RNF9)."""
    output_dir = tmp_path / "output"
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session)
        _add_replay(session, output_dir, "loc1-quadra1_pendente", idade_dias=10)
        replay = session.get(Replay, "loc1-quadra1_pendente")
        assert replay.lara_status.value == "pendente"

        removidos = local_retention.purge_expired_replays(session, output_dir, retention_days=3)
        assert removidos == 1
