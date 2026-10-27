"""Testes da live no YouTube: a câmera (ISAPI) é quem empurra o RTMP.

Sem câmera nem rede: `requests.request` é trocado por uma câmera falsa que guarda
o estado de /ISAPI/Custom/RTMP, e a resolução de DNS por um IP fixo.
"""
from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pytest
import requests

from api import config, live

URL = "rtsp://user:p%40ss@10.0.0.1:554/stream1"
KEY = "abcd-1234-efgh-5678-ijkl"
INGEST = f"rtmp://203.0.113.9/live2/{KEY}"


class _FakeCamera:
    def __init__(self):
        self.enabled = False
        self.url = ""
        self.calls: list[tuple[str, str | None]] = []
        self.auth = None
        self.put_status = "<statusCode>1</statusCode>"
        self.error: Exception | None = None

    def request(self, method, url, auth=None, timeout=None, data=None, headers=None):
        if self.error is not None:
            raise self.error
        assert url == "http://10.0.0.1/ISAPI/Custom/RTMP"
        self.auth = (auth.username, auth.password)
        body = data.decode() if data else None
        self.calls.append((method, body))
        if method == "PUT":
            if "<URL></URL>" in body:  # o firmware real recusa URL vazia
                return SimpleNamespace(status_code=200, text="<statusCode>4</statusCode>")
            self.enabled = "<enabled>true</enabled>" in body
            self.url = re.search(r"<URL>(.*)</URL>", body).group(1)
            return SimpleNamespace(status_code=200, text=self.put_status)
        return SimpleNamespace(
            status_code=200,
            text=f"<RTMP><enabled>{str(self.enabled).lower()}</enabled><URL>{self.url}</URL></RTMP>",
        )

    @property
    def puts(self):
        return [b for m, b in self.calls if m == "PUT"]


@pytest.fixture
def camera(monkeypatch, tmp_path):
    cam = _FakeCamera()
    monkeypatch.setattr(live.requests, "request", cam.request)
    monkeypatch.setattr(live.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("203.0.113.9", 0))])
    monkeypatch.setattr(config, "YOUTUBE_RTMP_URL", "rtmp://a.rtmp.youtube.com/live2")
    monkeypatch.setattr(config, "LIVE_MAX_SECONDS", 7200.0)
    monkeypatch.setattr(config, "LIVE_STATE_FILE", tmp_path / "live_state.json")
    yield cam
    live.cancel_timers()


def test_start_turns_on_the_camera_with_the_ingest_ip_and_the_key(camera):
    assert live.start("q1", URL, KEY) is True
    assert camera.enabled and camera.url == INGEST
    assert camera.auth == ("user", "p@ss")  # credencial do RTSP, já decodificada


def test_start_is_idempotent(camera):
    live.start("q1", URL, KEY)
    assert live.start("q1", URL, KEY) is False
    assert len(camera.puts) == 1


def test_start_with_another_key_reconfigures_the_camera(camera):
    live.start("q1", URL, KEY)
    assert live.start("q1", URL, "outra-key") is True
    assert camera.url.endswith("/outra-key")


def test_ingest_keeps_port_and_path_of_the_configured_url(camera, monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_RTMP_URL", "rtmp://ingest.exemplo:1936/app/")
    live.start("q1", URL, KEY)
    assert camera.url == f"rtmp://203.0.113.9:1936/app/{KEY}"


def test_stop_turns_off_and_wipes_the_key_from_the_camera(camera):
    live.start("q1", URL, KEY)
    assert live.stop("q1", URL) is True
    assert not camera.enabled
    assert KEY not in camera.url and camera.url  # URL nunca vazia: o firmware recusa


def test_stop_when_already_off_does_not_write(camera):
    assert live.stop("q1", URL) is False
    assert camera.puts == []


def test_status_reads_the_camera(camera):
    assert live.status(URL) is False
    live.start("q1", URL, KEY)
    assert live.status(URL) is True


def test_start_fails_when_the_camera_does_not_keep_it_on(camera):
    camera.put_status = "<statusCode>1</statusCode>"
    original = camera.request

    def _ignores_enable(method, url, **kw):
        if method == "PUT":
            camera.calls.append((method, kw["data"].decode()))
            return SimpleNamespace(status_code=200, text=camera.put_status)
        return original(method, url, **kw)

    live.requests.request = _ignores_enable
    with pytest.raises(live.LiveError, match="não ficou com a live ligada"):
        live.start("q1", URL, KEY)


def test_camera_unreachable_raises_live_error_without_leaking_the_url(camera):
    camera.error = requests.ConnectTimeout("http://user:p%40ss@10.0.0.1 timeout")
    with pytest.raises(live.LiveError) as exc:
        live.start("q1", URL, KEY)
    assert "p%40ss" not in str(exc.value) and "ConnectTimeout" in str(exc.value)


def test_camera_rejecting_the_put_raises_live_error(camera):
    camera.put_status = "<statusCode>4</statusCode>"
    with pytest.raises(live.LiveError, match="recusou PUT"):
        live.start("q1", URL, KEY)


def test_dns_failure_raises_live_error_before_touching_the_camera(camera, monkeypatch):
    def _fail(*a, **k):
        raise OSError("sem DNS")

    monkeypatch.setattr(live.socket, "getaddrinfo", _fail)
    with pytest.raises(live.LiveError, match="resolver"):
        live.start("q1", URL, KEY)
    assert camera.calls == []


def test_start_saves_the_deadline_and_stop_forgets_it(camera):
    live.start("q1", URL, KEY)
    assert live._load_deadlines()["q1"] == pytest.approx(time.time() + 7200, abs=5)
    live.stop("q1", URL)
    assert live._load_deadlines() == {}


def test_no_deadline_when_max_seconds_is_zero(camera, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_SECONDS", 0.0)
    live.start("q1", URL, KEY)
    assert live._load_deadlines() == {} and live._timers == {}


def test_resume_turns_off_a_live_whose_deadline_passed_while_the_api_was_down(camera):
    live.start("q1", URL, KEY)
    live._save_deadlines({"q1": time.time() - 1})
    live.cancel_timers()  # simula o reinício da API: o timer em memória some
    live.resume({"q1": {"input_url": URL}})
    for _ in range(50):
        if not camera.enabled:
            break
        time.sleep(0.05)
    assert not camera.enabled and live._load_deadlines() == {}


def test_resume_rearms_a_future_deadline_without_touching_the_camera(camera):
    live.start("q1", URL, KEY)
    puts_before = len(camera.puts)
    live.cancel_timers()
    live.resume({"q1": {"input_url": URL}})
    assert "q1" in live._timers and camera.enabled and len(camera.puts) == puts_before


def test_cancel_timers_does_not_turn_off_the_camera(camera):
    live.start("q1", URL, KEY)
    live.cancel_timers()
    assert camera.enabled and live._timers == {}
