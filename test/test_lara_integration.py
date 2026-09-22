"""
test_lara_integration.py — testes do cliente do Lara, do cache de
configuração (config_hash) e da fila de envio (PT-14/PT-15,
PLANO_DE_ACAO.md v3). Sem rede real: `requests` é substituído por um
transporte falso, e `apply_overlay` é isolado do ffmpeg de verdade
substituindo `_run` — o que importa aqui é a lógica de decisão (hash
mudou? qual overlay preferir? qual exceção mapear?), não o ffmpeg em si
(coberto pelos testes de pipeline existentes).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, select

from db import engine as engine_module
from db.models import Esporte, Local, Quadra, Replay, ReplayLaraStatus
from integrations import config_sync, heartbeat, upload_queue
from integrations.lara_client import (
    CameraConfig,
    LaraAuthError,
    LaraClient,
    LaraNotFoundError,
    LaraPayloadTooLargeError,
    LaraRateLimitError,
    LaraServerError,
    LaraValidationError,
    OverlayConfig,
    UploadResult,
)
from integrations.audio import AudioApplicationError, apply_audio
from integrations.orientation import OrientationApplicationError, apply_orientation
from integrations.overlay import OverlayApplicationError, apply_overlay


def _make_engine(tmp_path):
    db_path = tmp_path / "test_repet.db"
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
            nome="Quadra 1",
            input_url="rtsp://user:senha@10.0.0.1:554/stream1",
        )
    )
    session.commit()


# --- LaraClient: mapeamento de erro por status HTTP -----------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | list | None = None, ok: bool | None = None):
        self.status_code = status_code
        self._json_body = json_body
        self.ok = ok if ok is not None else 200 <= status_code < 300

    def json(self):
        if self._json_body is None:
            raise ValueError("sem corpo JSON")
        return self._json_body


def _client_with_fake_response(monkeypatch, response: _FakeResponse) -> LaraClient:
    client = LaraClient(base_url="https://lara.example/api/replay", token="1|abc")
    monkeypatch.setattr(client._session, "request", lambda *a, **kw: response)
    return client


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_raised(monkeypatch, status):
    client = _client_with_fake_response(monkeypatch, _FakeResponse(status))
    with pytest.raises(LaraAuthError):
        client.ping()


def test_not_found_error_raised(monkeypatch):
    client = _client_with_fake_response(monkeypatch, _FakeResponse(404))
    with pytest.raises(LaraNotFoundError):
        client.get_camera("quadra-fantasma")


def test_validation_error_with_body(monkeypatch):
    body = {"message": "duration_seconds inválido"}
    client = _client_with_fake_response(monkeypatch, _FakeResponse(422, json_body=body))
    with pytest.raises(LaraValidationError, match="duration_seconds inválido"):
        client.send_heartbeat("loc1-quadra1")


def test_payload_too_large_on_422_empty_body(monkeypatch):
    client = _client_with_fake_response(monkeypatch, _FakeResponse(422, json_body=None))
    with pytest.raises(LaraPayloadTooLargeError):
        client.send_heartbeat("loc1-quadra1")


def test_payload_too_large_on_413(monkeypatch):
    client = _client_with_fake_response(monkeypatch, _FakeResponse(413))
    with pytest.raises(LaraPayloadTooLargeError):
        client.send_heartbeat("loc1-quadra1")


def test_rate_limit_error(monkeypatch):
    client = _client_with_fake_response(monkeypatch, _FakeResponse(429))
    with pytest.raises(LaraRateLimitError):
        client.send_heartbeat("loc1-quadra1")


def test_server_error(monkeypatch):
    client = _client_with_fake_response(monkeypatch, _FakeResponse(500))
    with pytest.raises(LaraServerError):
        client.send_heartbeat("loc1-quadra1")


def test_get_cameras_parses_config_hash_and_overlay(monkeypatch):
    # Shape real do Lara: hash do lote na raiz, lista sob "cameras", sem
    # config_hash por item (confirmado batendo no servidor real em 2026-09-22).
    body = {
        "config_hash": "abc123",
        "cameras": [
            {
                "external_id": "loc1-quadra1",
                "orientation": "vertical",
                "clip_seconds": 20,
                "overlay": {
                    "png_url": "https://lara.example/overlays/abc.png",
                    "animated_url": None,
                    "width": 1080,
                    "height": 1920,
                },
            }
        ],
    }
    client = _client_with_fake_response(monkeypatch, _FakeResponse(200, json_body=body))
    cameras = client.get_cameras()
    assert len(cameras) == 1
    assert cameras[0].config_hash == "abc123"
    assert cameras[0].orientation == "vertical"
    assert cameras[0].overlay.png_url == "https://lara.example/overlays/abc.png"
    assert cameras[0].overlay.animated_url is None


def test_get_cameras_uses_per_camera_hash_when_present(monkeypatch):
    # Resiliência: se o Lara passar a mandar um config_hash por item no
    # futuro, ele deve prevalecer sobre o hash do lote.
    body = {
        "config_hash": "batch-hash",
        "cameras": [
            {
                "external_id": "loc1-quadra1",
                "config_hash": "own-hash",
                "orientation": "vertical",
                "clip_seconds": 20,
                "overlay": None,
            }
        ],
    }
    client = _client_with_fake_response(monkeypatch, _FakeResponse(200, json_body=body))
    cameras = client.get_cameras()
    assert cameras[0].config_hash == "own-hash"


def test_upload_video_sends_recorded_at_with_explicit_utc_offset(monkeypatch, tmp_path):
    # Replay.criado_em vem de datetime.now() naive — mas o relógio do
    # servidor é UTC. Sem offset explícito, o Lara (Laravel/Carbon)
    # interpreta a string no timezone do app dele (America/Sao_Paulo,
    # UTC-3), deslocando o horário do clipe em 3h.
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        return _FakeResponse(
            201,
            json_body={"uuid": "u1", "url": "https://lara.example/u1", "expires_at": "2026-09-29T00:00:00-03:00"},
        )

    client = LaraClient(base_url="https://lara.example/api/replay", token="1|abc")
    monkeypatch.setattr(client._session, "request", fake_request)

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")

    client.upload_video(
        external_id="loc1-quadra1",
        file_path=clip,
        recorded_at=datetime(2026, 9, 22, 13, 37, 49),  # naive, instante real em UTC
        duration_seconds=35,
        clip_external_id="replay-1",
    )

    assert captured["data"]["recorded_at"] == "2026-09-22T13:37:49+00:00"


# --- config_sync: config_hash como cache barato ----------------------------


class _FakeLaraClientForSync:
    """Substitui LaraClient nos testes de config_sync/upload_queue — não
    fala com requests, só devolve o que o teste configurar."""

    def __init__(self, cameras=None):
        self._cameras = cameras or []
        self.download_calls: list[tuple[str, Path]] = []
        self.upload_calls: list[dict] = []
        self.heartbeat_calls: list[str] = []
        self._upload_result = UploadResult(uuid="u1", url="https://lara/x.mp4", expires_at="2026-09-24T00:00:00", duplicated=False)
        self._upload_exception = None

    def get_cameras(self):
        return self._cameras

    def download_to(self, url: str, dest: Path) -> None:
        self.download_calls.append((url, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake")

    def upload_video(self, **kwargs):
        self.upload_calls.append(kwargs)
        if self._upload_exception:
            raise self._upload_exception
        return self._upload_result

    def send_heartbeat(self, external_id: str) -> None:
        self.heartbeat_calls.append(external_id)


def test_sync_once_skips_when_hash_unchanged(tmp_path):
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session)
        quadra = session.get(Quadra, "loc1-quadra1")
        quadra.lara_config_hash = "same-hash"
        session.add(quadra)
        session.commit()

        client = _FakeLaraClientForSync(
            cameras=[
                CameraConfig(
                    external_id="loc1-quadra1",
                    config_hash="same-hash",
                    orientation="horizontal",
                    clip_seconds=35,
                    overlay=None,
                )
            ]
        )
        changed = config_sync.sync_once(session, client, tmp_path / "overlays")
        assert changed == 0
        assert client.download_calls == []  # pull barato: nenhuma chamada extra


def test_sync_once_updates_and_downloads_overlay_when_hash_changes(tmp_path):
    engine = _make_engine(tmp_path)
    overlay_dir = tmp_path / "overlays"
    with Session(engine) as session:
        _seed_quadra(session)

        client = _FakeLaraClientForSync(
            cameras=[
                CameraConfig(
                    external_id="loc1-quadra1",
                    config_hash="new-hash",
                    orientation="vertical",
                    clip_seconds=20,
                    overlay=OverlayConfig(
                        png_url="https://lara.example/o.png",
                        animated_url=None,
                        width=1080,
                        height=1920,
                    ),
                )
            ]
        )
        changed = config_sync.sync_once(session, client, overlay_dir)
        assert changed == 1
        assert len(client.download_calls) == 1

        quadra = session.get(Quadra, "loc1-quadra1")
        assert quadra.orientation == "vertical"
        assert quadra.clip_seconds == 20
        assert quadra.lara_config_hash == "new-hash"
        assert quadra.overlay_png_path == str(overlay_dir / "loc1-quadra1.png")
        assert Path(quadra.overlay_png_path).is_file()


def test_sync_once_ignores_camera_not_registered_locally(tmp_path):
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session)
        client = _FakeLaraClientForSync(
            cameras=[
                CameraConfig(
                    external_id="quadra-que-nao-existe-aqui",
                    config_hash="h",
                    orientation="horizontal",
                    clip_seconds=35,
                    overlay=None,
                )
            ]
        )
        changed = config_sync.sync_once(session, client, tmp_path / "overlays")
        assert changed == 0


# --- upload_queue: idempotência e classificação de erro --------------------


def _seed_pending_replay(session: Session, output_dir: Path, replay_id: str = "loc1-quadra1_20260917140000") -> Replay:
    output_dir.mkdir(parents=True, exist_ok=True)
    clip_file = output_dir / f"{replay_id}.mp4"
    clip_file.write_bytes(b"fake mp4 content")
    replay = Replay(
        id=replay_id,
        quadra_id="loc1-quadra1",
        arquivo_bruto=clip_file.name,
        criado_em=datetime(2026, 9, 17, 14, 0, 0),
        duracao_segundos=35.0,
        tamanho_bytes=clip_file.stat().st_size,
    )
    session.add(replay)
    session.commit()
    return replay


def test_process_pending_marks_enviado_on_success(tmp_path):
    engine = _make_engine(tmp_path)
    output_dir = tmp_path / "output"
    with Session(engine) as session:
        _seed_quadra(session)
        _seed_pending_replay(session, output_dir)

        client = _FakeLaraClientForSync()
        upload_queue.process_pending(session, client, output_dir)

        replay = session.get(Replay, "loc1-quadra1_20260917140000")
        assert replay.lara_status == ReplayLaraStatus.ENVIADO
        assert replay.lara_uuid == "u1"
        assert len(client.upload_calls) == 1
        assert client.upload_calls[0]["clip_external_id"] == replay.id


def test_process_pending_marks_falha_on_validation_error(tmp_path):
    engine = _make_engine(tmp_path)
    output_dir = tmp_path / "output"
    with Session(engine) as session:
        _seed_quadra(session)
        _seed_pending_replay(session, output_dir)

        client = _FakeLaraClientForSync()
        client._upload_exception = LaraValidationError("campo faltando")
        upload_queue.process_pending(session, client, output_dir)

        replay = session.get(Replay, "loc1-quadra1_20260917140000")
        assert replay.lara_status == ReplayLaraStatus.FALHA
        assert "campo faltando" in replay.lara_ultimo_erro


def test_process_pending_stays_pendente_on_server_error(tmp_path):
    engine = _make_engine(tmp_path)
    output_dir = tmp_path / "output"
    with Session(engine) as session:
        _seed_quadra(session)
        _seed_pending_replay(session, output_dir)

        client = _FakeLaraClientForSync()
        client._upload_exception = LaraServerError("500 do Lara")
        upload_queue.process_pending(session, client, output_dir)

        replay = session.get(Replay, "loc1-quadra1_20260917140000")
        assert replay.lara_status == ReplayLaraStatus.PENDENTE  # retentável — próxima passada tenta de novo


def test_process_pending_applies_exponential_backoff_and_skips_before_it_elapses(tmp_path):
    """Prompt do Lara: 'retente com backoff', especificamente pro 429 —
    sem isso, uma indisponibilidade prolongada bateria no Lara a cada
    passada da fila pra cada replay pendente."""
    engine = _make_engine(tmp_path)
    output_dir = tmp_path / "output"
    with Session(engine) as session:
        _seed_quadra(session)
        _seed_pending_replay(session, output_dir)

        client = _FakeLaraClientForSync()
        client._upload_exception = LaraRateLimitError("429 do Lara")

        upload_queue.process_pending(session, client, output_dir)
        replay = session.get(Replay, "loc1-quadra1_20260917140000")
        assert replay.lara_status == ReplayLaraStatus.PENDENTE
        assert replay.lara_tentativas == 1
        assert replay.lara_proxima_tentativa_em > datetime.now()
        primeira_proxima_tentativa = replay.lara_proxima_tentativa_em

        # Antes do backoff passar, uma nova passada da fila não tenta de novo.
        upload_queue.process_pending(session, client, output_dir)
        assert len(client.upload_calls) == 1  # não subiu

        # Simula o backoff já ter passado — a próxima passada tenta de novo
        # e, ao falhar outra vez, dobra o atraso.
        replay.lara_proxima_tentativa_em = datetime.now()
        session.add(replay)
        session.commit()
        upload_queue.process_pending(session, client, output_dir)
        replay = session.get(Replay, "loc1-quadra1_20260917140000")
        assert len(client.upload_calls) == 2
        assert replay.lara_tentativas == 2
        segundo_atraso = (replay.lara_proxima_tentativa_em - datetime.now()).total_seconds()
        primeiro_atraso = (primeira_proxima_tentativa - datetime.now()).total_seconds()
        assert segundo_atraso > primeiro_atraso  # backoff cresceu


def test_process_pending_resets_backoff_on_success(tmp_path):
    engine = _make_engine(tmp_path)
    output_dir = tmp_path / "output"
    with Session(engine) as session:
        _seed_quadra(session)
        replay = _seed_pending_replay(session, output_dir)
        replay.lara_tentativas = 3
        replay.lara_proxima_tentativa_em = datetime.now()
        session.add(replay)
        session.commit()

        client = _FakeLaraClientForSync()
        upload_queue.process_pending(session, client, output_dir)

        replay = session.get(Replay, "loc1-quadra1_20260917140000")
        assert replay.lara_status == ReplayLaraStatus.ENVIADO
        assert replay.lara_tentativas == 0
        assert replay.lara_proxima_tentativa_em is None


def test_process_pending_applies_backoff_on_local_processing_failure(tmp_path, monkeypatch):
    """Achado numa auditoria: falha de processamento LOCAL (ffmpeg do
    overlay/orientação, já visto na prática pelo menos uma vez em
    produção) não era capturada em _process_one — propagava pra fora,
    travava o resto da passada da fila e nunca aplicava backoff (batia
    de novo a cada 10s pra sempre). RNF9 pede o mesmo tratamento de
    qualquer outra falha retentável."""
    engine = _make_engine(tmp_path)
    output_dir = tmp_path / "output"
    with Session(engine) as session:
        _seed_quadra(session)
        _seed_pending_replay(session, output_dir)

        def _boom(clip_path, quadra):
            raise OverlayApplicationError("ffmpeg explodiu (transitório)")

        monkeypatch.setattr(upload_queue, "apply_overlay", _boom)

        client = _FakeLaraClientForSync()
        upload_queue.process_pending(session, client, output_dir)

        replay = session.get(Replay, "loc1-quadra1_20260917140000")
        assert replay.lara_status == ReplayLaraStatus.PENDENTE  # não é FALHA definitiva
        assert replay.lara_tentativas == 1
        assert replay.lara_proxima_tentativa_em > datetime.now()
        assert "ffmpeg explodiu" in replay.lara_ultimo_erro
        assert len(client.upload_calls) == 0  # nem chegou a tentar enviar


def test_process_pending_chains_orientation_then_overlay_and_cleans_intermediate(tmp_path, monkeypatch):
    """orientation roda antes de overlay (overlay precisa aplicar na
    resolução já cortada) e o intermediário só-orientado não pode
    sobrar em disco órfão — nem reconcile nem local_retention sabem
    dele, só arquivo_bruto/arquivo_processado são rastreados."""
    engine = _make_engine(tmp_path)
    output_dir = tmp_path / "output"
    with Session(engine) as session:
        _seed_quadra(session)
        replay = _seed_pending_replay(session, output_dir)
        clip_path = output_dir / replay.arquivo_bruto

        def _fake_orientation(path, quadra):
            oriented = path.with_name(f"{path.stem}_oriented.mp4")
            oriented.write_bytes(b"oriented")
            return oriented

        def _fake_overlay(path, quadra):
            assert path.name.endswith("_oriented.mp4")  # recebeu a saída da orientação, não o bruto
            overlaid = path.with_name(f"{path.stem}_overlay.mp4")
            overlaid.write_bytes(b"overlaid")
            return overlaid

        monkeypatch.setattr(upload_queue, "apply_orientation", _fake_orientation)
        monkeypatch.setattr(upload_queue, "apply_overlay", _fake_overlay)

        client = _FakeLaraClientForSync()
        upload_queue.process_pending(session, client, output_dir)

        replay = session.get(Replay, "loc1-quadra1_20260917140000")
        assert replay.lara_status == ReplayLaraStatus.ENVIADO
        assert replay.arquivo_processado == f"{clip_path.stem}_oriented_overlay.mp4"
        assert not (output_dir / f"{clip_path.stem}_oriented.mp4").exists()  # intermediário limpo
        assert (output_dir / f"{clip_path.stem}_oriented_overlay.mp4").exists()  # final preservado
        assert client.upload_calls[0]["file_path"].name == f"{clip_path.stem}_oriented_overlay.mp4"


def test_process_pending_chains_audio_after_overlay_and_cleans_intermediate(tmp_path, monkeypatch):
    """música é o último passo da cadeia (depois de orientação e overlay,
    ver upload_queue.py) e o intermediário sem música não pode sobrar em
    disco órfão — mesmo raciocínio do teste de orientation->overlay acima."""
    engine = _make_engine(tmp_path)
    output_dir = tmp_path / "output"
    music_dir = tmp_path / "music"
    with Session(engine) as session:
        _seed_quadra(session)
        replay = _seed_pending_replay(session, output_dir)
        clip_path = output_dir / replay.arquivo_bruto

        def _fake_overlay(path, quadra):
            overlaid = path.with_name(f"{path.stem}_overlay.mp4")
            overlaid.write_bytes(b"overlaid")
            return overlaid

        def _fake_audio(path, m_dir, volume, fade):
            assert path.name.endswith("_overlay.mp4")  # recebeu a saída do overlay
            assert m_dir == music_dir
            with_audio = path.with_name(f"{path.stem}_audio.mp4")
            with_audio.write_bytes(b"with audio")
            return with_audio

        monkeypatch.setattr(upload_queue, "apply_overlay", _fake_overlay)
        monkeypatch.setattr(upload_queue, "apply_audio", _fake_audio)

        client = _FakeLaraClientForSync()
        upload_queue.process_pending(session, client, output_dir, music_dir)

        replay = session.get(Replay, "loc1-quadra1_20260917140000")
        assert replay.lara_status == ReplayLaraStatus.ENVIADO
        assert replay.arquivo_processado == f"{clip_path.stem}_overlay_audio.mp4"
        assert not (output_dir / f"{clip_path.stem}_overlay.mp4").exists()  # intermediário limpo
        assert (output_dir / f"{clip_path.stem}_overlay_audio.mp4").exists()  # final preservado
        assert client.upload_calls[0]["file_path"].name == f"{clip_path.stem}_overlay_audio.mp4"


# --- heartbeat: falha não propaga ------------------------------------------


def test_heartbeat_failure_is_swallowed(tmp_path):
    engine = _make_engine(tmp_path)
    with Session(engine) as session:
        _seed_quadra(session)
        client = _FakeLaraClientForSync()

        def _boom(external_id):
            raise LaraServerError("indisponível")

        client.send_heartbeat = _boom  # não deve levantar pra fora de send_all
        heartbeat.send_all(session, client)  # não deve lançar


# --- overlay: mecânica, nunca decide qual logo aplicar ---------------------


def test_apply_overlay_returns_same_path_when_no_overlay_cached(tmp_path):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    quadra = Quadra(
        id="loc1-quadra1", local_id="loc1", esporte_id="futsal",
        nome="Quadra 1", input_url="rtsp://x",
    )
    result = apply_overlay(clip, quadra)
    assert result == clip


def test_apply_overlay_prefers_animated_over_png(tmp_path, monkeypatch):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    animated = tmp_path / "loc1-quadra1.webm"
    animated.write_bytes(b"fake webm")
    png = tmp_path / "loc1-quadra1.png"
    png.write_bytes(b"fake png")

    quadra = Quadra(
        id="loc1-quadra1", local_id="loc1", esporte_id="futsal",
        nome="Quadra 1", input_url="rtsp://x",
        overlay_animated_path=str(animated), overlay_png_path=str(png),
    )

    calls = []
    monkeypatch.setattr("integrations.overlay._run", lambda cmd: calls.append(cmd))
    monkeypatch.setattr("integrations.overlay.probe_resolution", lambda path: (1080, 1920))

    result = apply_overlay(clip, quadra)
    assert result.name == "loc1-quadra1_20260917140000_overlay.mp4"
    assert len(calls) == 1
    cmd = calls[0]
    assert str(animated) in cmd
    assert str(png) not in cmd
    assert "-stream_loop" in cmd


def test_apply_overlay_raises_and_cleans_up_on_ffmpeg_failure(tmp_path, monkeypatch):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    png = tmp_path / "loc1-quadra1.png"
    png.write_bytes(b"fake png")
    quadra = Quadra(
        id="loc1-quadra1", local_id="loc1", esporte_id="futsal",
        nome="Quadra 1", input_url="rtsp://x", overlay_png_path=str(png),
    )

    def _boom(cmd):
        raise OverlayApplicationError("ffmpeg explodiu")

    monkeypatch.setattr("integrations.overlay._run", _boom)
    monkeypatch.setattr("integrations.overlay.probe_resolution", lambda path: (1080, 1920))

    with pytest.raises(OverlayApplicationError):
        apply_overlay(clip, quadra)

    assert not (tmp_path / "loc1-quadra1_20260917140000_overlay.mp4").exists()


# --- orientation: crop mecânico pro aspect ratio pedido pelo Lara ----------


def _quadra_com_orientation(orientation: str | None) -> Quadra:
    return Quadra(
        id="loc1-quadra1", local_id="loc1", esporte_id="futsal",
        nome="Quadra 1", input_url="rtsp://x", orientation=orientation,
    )


def test_apply_orientation_noop_when_not_synced_yet(tmp_path):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    assert apply_orientation(clip, _quadra_com_orientation(None)) == clip


def test_apply_orientation_noop_on_unknown_value(tmp_path):
    """Defensivo: o contrato do Lara só permite vertical/horizontal, mas
    não vale travar se algum dia vier outra coisa — ignora, não decide."""
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    assert apply_orientation(clip, _quadra_com_orientation("diagonal")) == clip


def test_apply_orientation_noop_when_already_matches_target(tmp_path, monkeypatch):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    monkeypatch.setattr("integrations.orientation.probe_resolution", lambda path: (1920, 1080))

    calls = []
    monkeypatch.setattr("integrations.orientation._run", lambda cmd: calls.append(cmd))

    result = apply_orientation(clip, _quadra_com_orientation("horizontal"))
    assert result == clip
    assert calls == []  # já é 16:9, sem reencode


def test_apply_orientation_crops_landscape_to_vertical(tmp_path, monkeypatch):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    monkeypatch.setattr("integrations.orientation.probe_resolution", lambda path: (1280, 960))

    calls = []
    monkeypatch.setattr("integrations.orientation._run", lambda cmd: calls.append(cmd))

    result = apply_orientation(clip, _quadra_com_orientation("vertical"))
    assert result.name == "loc1-quadra1_20260917140000_oriented.mp4"
    assert len(calls) == 1
    assert "crop=540:960" in calls[0]  # 960 * 9/16 = 540, altura mantida


def test_apply_orientation_crops_4_3_to_horizontal_16_9(tmp_path, monkeypatch):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    monkeypatch.setattr("integrations.orientation.probe_resolution", lambda path: (1280, 960))

    calls = []
    monkeypatch.setattr("integrations.orientation._run", lambda cmd: calls.append(cmd))

    result = apply_orientation(clip, _quadra_com_orientation("horizontal"))
    assert result.name == "loc1-quadra1_20260917140000_oriented.mp4"
    assert "crop=1280:720" in calls[0]  # 1280 / 16*9 = 720, largura mantida


def test_apply_orientation_raises_and_cleans_up_on_ffmpeg_failure(tmp_path, monkeypatch):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    monkeypatch.setattr("integrations.orientation.probe_resolution", lambda path: (1280, 960))

    def _boom(cmd):
        raise OrientationApplicationError("ffmpeg explodiu")

    monkeypatch.setattr("integrations.orientation._run", _boom)

    with pytest.raises(OrientationApplicationError):
        apply_orientation(clip, _quadra_com_orientation("vertical"))

    assert not (tmp_path / "loc1-quadra1_20260917140000_oriented.mp4").exists()


# --- audio: mixagem local de música de fundo, nunca vem do Lara ------------


def test_apply_audio_noop_when_music_dir_missing(tmp_path):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    assert apply_audio(clip, tmp_path / "no-such-dir") == clip


def test_apply_audio_noop_when_music_dir_empty(tmp_path):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    assert apply_audio(clip, music_dir) == clip


def test_apply_audio_noop_when_music_dir_is_none(tmp_path):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    assert apply_audio(clip, None) == clip


def test_apply_audio_ignores_non_audio_files(tmp_path):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "README.md").write_text("não é áudio")
    assert apply_audio(clip, music_dir) == clip


def test_apply_audio_mixes_single_track_with_fade_and_volume(tmp_path, monkeypatch):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track = music_dir / "trilha.mp3"
    track.write_bytes(b"fake mp3")

    monkeypatch.setattr("integrations.audio.probe_duration_seconds", lambda path: 35.0)
    calls = []
    monkeypatch.setattr("integrations.audio._run", lambda cmd: calls.append(cmd))

    result = apply_audio(clip, music_dir, volume=0.5, fade_seconds=1.5)
    assert result.name == "loc1-quadra1_20260917140000_audio.mp4"
    assert len(calls) == 1
    cmd = calls[0]
    assert str(track) in cmd
    assert "-stream_loop" in cmd  # loop pra cobrir faixa mais curta que o clipe
    filter_arg = cmd[cmd.index("-filter_complex") + 1]
    assert "atrim=0:35.0" in filter_arg
    assert "afade=t=in:st=0:d=1.5" in filter_arg
    assert "afade=t=out:st=33.5:d=1.5" in filter_arg
    assert "volume=0.5" in filter_arg
    assert "-map" in cmd and "0:v" in cmd  # vídeo vem do clipe, não da música
    assert "-c:v" in cmd and "copy" in cmd  # nunca reencoda vídeo por causa de áudio


def test_apply_audio_picks_randomly_among_multiple_tracks(tmp_path, monkeypatch):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track_a = music_dir / "a.mp3"
    track_a.write_bytes(b"a")
    track_b = music_dir / "b.mp3"
    track_b.write_bytes(b"b")

    monkeypatch.setattr("integrations.audio.probe_duration_seconds", lambda path: 35.0)
    monkeypatch.setattr("integrations.audio._run", lambda cmd: None)
    monkeypatch.setattr("integrations.audio.random.choice", lambda seq: track_b)

    result = apply_audio(clip, music_dir)
    assert result.name == "loc1-quadra1_20260917140000_audio.mp4"


def test_apply_audio_raises_and_cleans_up_on_ffmpeg_failure(tmp_path, monkeypatch):
    clip = tmp_path / "loc1-quadra1_20260917140000.mp4"
    clip.write_bytes(b"fake")
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "trilha.mp3").write_bytes(b"fake mp3")

    monkeypatch.setattr("integrations.audio.probe_duration_seconds", lambda path: 35.0)

    def _boom(cmd):
        raise AudioApplicationError("ffmpeg explodiu")

    monkeypatch.setattr("integrations.audio._run", _boom)

    with pytest.raises(AudioApplicationError):
        apply_audio(clip, music_dir)

    assert not (tmp_path / "loc1-quadra1_20260917140000_audio.mp4").exists()
