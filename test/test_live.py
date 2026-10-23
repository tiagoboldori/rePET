"""Testes da live no YouTube: comando do ffmpeg, escolha do áudio e fallback para silêncio.

Sem ffmpeg nem câmera: `_spawn` e o ffprobe (`subprocess.run`) são substituídos por fakes.
"""
from __future__ import annotations

import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from api import config, live

URL = "rtsp://user:senha@10.0.0.1:554/stream1"


class _FakeProc:
    """Processo que só termina quando o teste manda (ou quando o stop o termina)."""

    def __init__(self, alive: bool = True):
        self.returncode = None if alive else 1
        self._exited = threading.Event()
        if not alive:
            self._exited.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.returncode

    def die(self, code: int = 1):
        self.returncode = code
        self._exited.set()

    def terminate(self):
        self.die(-15)

    def kill(self):
        self.die(-9)


def _fake_spawn(monkeypatch, first=()):
    """Troca o `_spawn` por um que registra o `camera_audio` de cada lançamento.

    `first` são os processos devolvidos nos primeiros lançamentos; depois, um vivo novo."""
    spawned: list[bool] = []
    created: list[_FakeProc] = []
    queue = list(first)

    def _spawn(quadra_id, input_url, stream_key, camera_audio):
        spawned.append(camera_audio)
        proc = queue.pop(0) if queue else _FakeProc()
        created.append(proc)
        return proc

    monkeypatch.setattr(live, "_spawn", _spawn)
    return spawned, created


def _wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def _live_defaults(monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_SECONDS", 0)
    monkeypatch.setattr(live, "STARTUP_CHECK_SECONDS", 0)
    monkeypatch.setattr(live, "RESTART_DELAY_SECONDS", 0.01)
    yield
    live.stop_all()


# --- comando do ffmpeg -------------------------------------------------------


def test_build_cmd_with_camera_audio_reencodes_only_the_audio(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_RTMP_URL", "rtmp://yt.example/live2/")

    cmd = live._build_cmd(URL, "abc-key", camera_audio=True)

    joined = " ".join(cmd)
    assert cmd[-3:] == ["-f", "flv", "rtmp://yt.example/live2/abc-key"]
    assert "-map 0:v:0 -map 0:a:0" in joined  # vídeo e áudio vêm da câmera
    assert "-c:v copy" in joined  # nunca reencoda o vídeo
    assert "-c:a aac -ar 44100 -ac 2" in joined  # câmera é AAC mono 16 kHz; YouTube prefere estéreo 44,1 kHz
    assert "anullsrc" not in joined
    assert "-shortest" not in cmd  # sem silêncio infinito, o ffmpeg sai sozinho quando o RTSP cai


def test_build_cmd_without_camera_audio_adds_silence(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_RTMP_URL", "rtmp://yt.example/live2")

    cmd = live._build_cmd(URL, "abc-key", camera_audio=False)

    joined = " ".join(cmd)
    assert cmd[-3:] == ["-f", "flv", "rtmp://yt.example/live2/abc-key"]
    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in joined
    assert "-map 0:v:0 -map 1:a:0" in joined  # áudio vem do silêncio, não da câmera
    assert "0:a:0" not in joined
    assert "-c:v copy" in joined
    assert "-shortest" in cmd  # o silêncio é infinito; sem isso o ffmpeg ficaria preso


# --- ffprobe do áudio da câmera ----------------------------------------------


@pytest.mark.parametrize(
    "returncode, stdout, expected",
    [
        (0, "1\n", True),  # há trilha de áudio
        (0, "", False),  # ffprobe respondeu e não há trilha: o único caso de "sem áudio"
        (1, "", True),  # ffprobe falhou (câmera fora do ar): não conclui nada
    ],
)
def test_camera_has_audio_reads_the_ffprobe_answer(monkeypatch, returncode, stdout, expected):
    monkeypatch.setattr(
        live.subprocess, "run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout),
    )

    assert live._camera_has_audio(URL) is expected


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired("ffprobe", 15), FileNotFoundError("ffprobe")],
)
def test_camera_has_audio_assumes_audio_when_ffprobe_cannot_answer(monkeypatch, error):
    def _raise(cmd, **kwargs):
        raise error

    monkeypatch.setattr(live.subprocess, "run", _raise)

    assert live._camera_has_audio(URL) is True


# --- _spawn ------------------------------------------------------------------


def test_spawn_logs_which_audio_is_used_but_never_the_stream_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIVE_LOG_DIR", tmp_path)
    popen_cmds = []

    def _popen(cmd, **kwargs):
        popen_cmds.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(live.subprocess, "Popen", _popen)

    live._spawn("q1", URL, "segredo-key", camera_audio=True)
    live._spawn("q1", URL, "segredo-key", camera_audio=False)

    log = (tmp_path / "live_q1.log").read_text()
    assert "ffmpeg iniciado (áudio: câmera)" in log
    assert "ffmpeg iniciado (áudio: silêncio)" in log
    assert "segredo-key" not in log
    assert popen_cmds == [
        live._build_cmd(URL, "segredo-key", True),
        live._build_cmd(URL, "segredo-key", False),
    ]


# --- start: escolha do áudio e fallback --------------------------------------


def test_start_uses_the_camera_audio(monkeypatch):
    monkeypatch.setattr(live, "_camera_has_audio", lambda url: True)
    spawned, _ = _fake_spawn(monkeypatch)

    assert live.start("q1", URL, "key") is True

    assert spawned == [True]
    assert live.is_live("q1")


def test_start_is_idempotent_and_does_not_probe_again(monkeypatch):
    probes = []
    monkeypatch.setattr(live, "_camera_has_audio", lambda url: probes.append(url) or True)
    spawned, _ = _fake_spawn(monkeypatch)

    assert live.start("q1", URL, "key") is True
    assert live.start("q1", URL, "key") is False

    assert spawned == [True]
    assert len(probes) == 1


def test_start_falls_back_to_silence_when_the_camera_has_no_audio(monkeypatch):
    monkeypatch.setattr(live, "_camera_has_audio", lambda url: False)
    spawned, _ = _fake_spawn(monkeypatch)

    assert live.start("q1", URL, "key") is True

    assert spawned == [False]


def test_start_retries_with_silence_when_ffmpeg_with_camera_audio_dies_on_startup(monkeypatch):
    monkeypatch.setattr(live, "_camera_has_audio", lambda url: True)
    spawned, _ = _fake_spawn(monkeypatch, first=[_FakeProc(alive=False)])

    assert live.start("q1", URL, "key") is True

    assert spawned == [True, False]
    assert live.is_live("q1")


def test_start_fails_when_ffmpeg_dies_even_with_silence(monkeypatch):
    monkeypatch.setattr(live, "_camera_has_audio", lambda url: True)
    spawned, _ = _fake_spawn(monkeypatch, first=[_FakeProc(alive=False), _FakeProc(alive=False)])

    with pytest.raises(live.LiveError, match="ffmpeg encerrou ao iniciar"):
        live.start("q1", URL, "key")

    assert spawned == [True, False]
    assert not live.is_live("q1")


def test_start_does_not_retry_when_ffmpeg_with_silence_dies(monkeypatch):
    """Já estava em silêncio: tentar de novo com silêncio só repetiria o mesmo erro."""
    monkeypatch.setattr(live, "_camera_has_audio", lambda url: False)
    spawned, _ = _fake_spawn(monkeypatch, first=[_FakeProc(alive=False)])

    with pytest.raises(live.LiveError):
        live.start("q1", URL, "key")

    assert spawned == [False]
    assert not live.is_live("q1")


# --- supervisor ---------------------------------------------------------------


def test_relaunch_checks_the_camera_audio_again(monkeypatch):
    has_audio = [True]
    monkeypatch.setattr(live, "_camera_has_audio", lambda url: has_audio[0])
    spawned, created = _fake_spawn(monkeypatch)
    live.start("q1", URL, "key")
    assert spawned == [True]

    has_audio[0] = False  # a câmera perdeu a trilha de áudio...
    created[0].die()  # ...e o ffmpeg caiu junto com o RTSP

    assert _wait_until(lambda: len(spawned) == 2)
    assert spawned == [True, False]
    assert live.is_live("q1")


def test_relaunch_goes_back_to_camera_audio_when_it_returns(monkeypatch):
    has_audio = [False]
    monkeypatch.setattr(live, "_camera_has_audio", lambda url: has_audio[0])
    spawned, created = _fake_spawn(monkeypatch)
    live.start("q1", URL, "key")
    assert spawned == [False]

    has_audio[0] = True
    created[0].die()

    assert _wait_until(lambda: len(spawned) == 2)
    assert spawned == [False, True]


def test_stop_during_relaunch_does_not_spawn_again(monkeypatch):
    probing = threading.Event()
    release = threading.Event()
    answers = iter([True])

    def _probe(url):
        try:
            return next(answers)
        except StopIteration:
            probing.set()  # 2º probe = o do relançamento
            release.wait(2)
            return True

    monkeypatch.setattr(live, "_camera_has_audio", _probe)
    spawned, created = _fake_spawn(monkeypatch)
    live.start("q1", URL, "key")

    created[0].die()
    assert probing.wait(2)  # supervisor está no ffprobe do relançamento
    assert live.stop("q1") is True
    release.set()

    time.sleep(0.1)
    assert spawned == [True]  # o relançamento foi abortado antes do _spawn
