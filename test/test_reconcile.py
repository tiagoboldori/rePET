"""
test_reconcile.py — testes da reconciliação idempotente entre disco e
banco (PT-03, db/reconcile.py).

`probe_duration_seconds` é substituído por monkeypatch (não chama ffprobe
de verdade) — o que importa aqui é a lógica de varredura/decisão (quais
arquivos casam, o que já existe, o que pertence a quadra desconhecida),
não o ffprobe em si (coberto pelos testes de pipeline existentes). Os
arquivos de clipe usados nos testes são vazios — só o nome importa.
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, SQLModel, select

from db import engine as engine_module
from db import reconcile as reconcile_module
from db.models import Esporte, Local, Quadra, Replay


def _make_engine(tmp_path, name="test_repet.db"):
    db_path = tmp_path / name
    engine = engine_module.create_sqlite_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_quadra(session: Session, quadra_id: str, local_id: str = "loc1") -> None:
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


def test_reconcile_inserts_missing_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(reconcile_module, "probe_duration_seconds", lambda path: 35.0)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    clip = output_dir / "loc1-quadra1_20260921093000.mp4"
    clip.write_bytes(b"fake-mp4-content")

    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session, "loc1-quadra1")

        inserted = reconcile_module.reconcile_replays(session, output_dir)
        assert inserted == 1

        replay = session.get(Replay, "loc1-quadra1_20260921093000")
        assert replay is not None
        assert replay.quadra_id == "loc1-quadra1"
        assert replay.arquivo_bruto == "loc1-quadra1_20260921093000.mp4"
        assert replay.criado_em == datetime(2026, 9, 21, 9, 30, 0)
        assert replay.duracao_segundos == 35.0
        assert replay.tamanho_bytes == len(b"fake-mp4-content")

        # Rodar de novo sobre o mesmo diretório não duplica.
        inserted_again = reconcile_module.reconcile_replays(session, output_dir)
        assert inserted_again == 0
        assert len(session.exec(select(Replay)).all()) == 1


def test_reconcile_does_not_overwrite_existing_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reconcile_module,
        "probe_duration_seconds",
        lambda path: (_ for _ in ()).throw(AssertionError("não deveria sondar um replay já existente")),
    )

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "loc1-quadra1_20260921093000.mp4").write_bytes(b"x")

    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session, "loc1-quadra1")
        session.add(
            Replay(
                id="loc1-quadra1_20260921093000",
                quadra_id="loc1-quadra1",
                arquivo_bruto="loc1-quadra1_20260921093000.mp4",
                duracao_segundos=99.0,
                tamanho_bytes=12345,
            )
        )
        session.commit()

        inserted = reconcile_module.reconcile_replays(session, output_dir)
        assert inserted == 0

        replay = session.get(Replay, "loc1-quadra1_20260921093000")
        assert replay.duracao_segundos == 99.0  # não foi sobrescrito
        assert replay.tamanho_bytes == 12345


def test_reconcile_skips_unknown_quadra(tmp_path, monkeypatch):
    monkeypatch.setattr(
        reconcile_module,
        "probe_duration_seconds",
        lambda path: (_ for _ in ()).throw(AssertionError("não deveria sondar arquivo de quadra desconhecida")),
    )

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "quadra-fantasma_20260921093000.mp4").write_bytes(b"x")

    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        inserted = reconcile_module.reconcile_replays(session, output_dir)
        assert inserted == 0
        assert session.exec(select(Replay)).all() == []


def test_reconcile_skips_probe_failure_without_aborting_scan(tmp_path, monkeypatch):
    from clipper.clip_generator import ClipGenerationError

    def fake_probe(path):
        if "20260921094000" in path.name:  # simula o clipe "corrompido"
            raise ClipGenerationError("ffprobe falhou")
        return 35.0

    monkeypatch.setattr(reconcile_module, "probe_duration_seconds", fake_probe)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "loc1-quadra1_20260921093000.mp4").write_bytes(b"x")
    (output_dir / "loc1-quadra1_20260921094000.mp4").write_bytes(b"x")

    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session, "loc1-quadra1")
        inserted = reconcile_module.reconcile_replays(session, output_dir)

    assert inserted == 1
    with Session(engine) as session:
        replays = session.exec(select(Replay)).all()
        assert len(replays) == 1
        assert replays[0].id == "loc1-quadra1_20260921093000"


def test_reconcile_ignores_non_mp4_and_unmatched_filenames(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "readme.txt").write_bytes(b"x")
    (output_dir / "loc1-quadra1-sem-timestamp.mp4").write_bytes(b"x")

    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session, "loc1-quadra1")
        inserted = reconcile_module.reconcile_replays(session, output_dir)

    assert inserted == 0


def test_reconcile_missing_output_dir_returns_zero(tmp_path):
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        assert reconcile_module.reconcile_replays(session, tmp_path / "nao-existe") == 0
