"""Transmissão ao vivo no YouTube: a própria câmera empurra o RTMP.

Nada passa pelo servidor: ligar a live é configurar a câmera (ISAPI Hikvision,
`PUT /ISAPI/Custom/RTMP`) com a URL do YouTube + stream key da quadra
(`youtube_stream_key` em config/cameras.json) e `enabled=true`; desligar é
`enabled=false`. A câmera reconecta sozinha se o RTMP cair e a live sobrevive
a reinício da API.

Pontos do firmware (DS-2CD1347G2H-LIU, V5.7.13):
- as câmeras estão sem DNS, então a URL leva o IP do ingest (resolvido aqui);
- a URL não pode ficar vazia (`Invalid XML Content`), então ao desligar vai um
  placeholder, que também apaga a stream key da câmera;
- o áudio vem do stream 101 (AAC mono 16 kHz); não há fallback para silêncio.

O prazo (LIVE_MAX_SECONDS) é gravado em LIVE_STATE_FILE para ser reassumido
quando a API sobe (`resume`); a câmera em si não tem timer.
"""
import json
import socket
import threading
import time
from urllib.parse import unquote, urlparse
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPDigestAuth

from api import config

_lock = threading.Lock()
_timers: dict[str, threading.Timer] = {}

ISAPI_TIMEOUT_SECONDS = 10
OFF_URL = "rtmp://127.0.0.1/off"
_RTMP_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<RTMP version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
    "<enabled>{enabled}</enabled><URL>{url}</URL></RTMP>"
)


class LiveError(Exception):
    """Falha ao falar com a câmera (mensagem segura para devolver na API)."""


def _camera_access(input_url: str) -> tuple[str, HTTPDigestAuth]:
    """Host e credencial da câmera, tirados do RTSP (a mesma conta serve no ISAPI)."""
    parsed = urlparse(input_url)
    auth = HTTPDigestAuth(unquote(parsed.username or ""), unquote(parsed.password or ""))
    return f"http://{parsed.hostname}", auth


def _isapi(method: str, input_url: str, body: str | None = None) -> str:
    base, auth = _camera_access(input_url)
    try:
        resp = requests.request(
            method, f"{base}/ISAPI/Custom/RTMP", auth=auth, timeout=ISAPI_TIMEOUT_SECONDS,
            data=body.encode() if body else None, headers={"Content-Type": "application/xml"},
        )
    except requests.RequestException as exc:
        # str(exc) pode trazer a URL; a mensagem só diz o tipo do erro.
        raise LiveError(f"câmera não respondeu ({type(exc).__name__}).") from exc
    if resp.status_code != 200 or (method == "PUT" and "<statusCode>1</statusCode>" not in resp.text):
        raise LiveError(f"câmera recusou {method} /ISAPI/Custom/RTMP (HTTP {resp.status_code}).")
    return resp.text


def _read(input_url: str) -> tuple[bool, str]:
    """(enabled, URL) como a câmera tem agora."""
    text = _isapi("GET", input_url)
    enabled = "<enabled>true</enabled>" in text
    url = text.split("<URL>", 1)[1].split("</URL>", 1)[0] if "<URL>" in text else ""
    return enabled, url


def _write(input_url: str, enabled: bool, url: str) -> None:
    _isapi("PUT", input_url, _RTMP_XML.format(enabled=str(enabled).lower(), url=escape(url)))


def _ingest_url(stream_key: str) -> str:
    """URL do YouTube com o host trocado pelo IPv4 (a câmera não resolve DNS)."""
    parsed = urlparse(config.YOUTUBE_RTMP_URL)
    try:
        ip = socket.getaddrinfo(parsed.hostname, None, socket.AF_INET)[0][4][0]
    except OSError as exc:
        raise LiveError(f"não consegui resolver {parsed.hostname}.") from exc
    port = f":{parsed.port}" if parsed.port else ""
    return f"rtmp://{ip}{port}{parsed.path.rstrip('/')}/{stream_key}"


# --- prazo (LIVE_MAX_SECONDS) persistido ------------------------------------

def _load_deadlines() -> dict[str, float]:
    try:
        return json.loads(config.LIVE_STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_deadlines(deadlines: dict[str, float]) -> None:
    config.LIVE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.LIVE_STATE_FILE.write_text(json.dumps(deadlines))


def _arm(quadra_id: str, input_url: str, deadline: float) -> None:
    timer = threading.Timer(max(deadline - time.time(), 0), _expire, args=(quadra_id, input_url))
    timer.daemon = True
    timer.start()
    _timers[quadra_id] = timer


def _expire(quadra_id: str, input_url: str) -> None:
    print(f"[live] {quadra_id}: LIVE_MAX_SECONDS atingido, encerrando.")
    try:
        stop(quadra_id, input_url)
    except LiveError as exc:
        print(f"[live] {quadra_id}: falha ao encerrar no prazo: {exc}")


def _forget(quadra_id: str) -> None:
    timer = _timers.pop(quadra_id, None)
    if timer is not None:
        timer.cancel()
    deadlines = _load_deadlines()
    if deadlines.pop(quadra_id, None) is not None:
        _save_deadlines(deadlines)


# --- API pública -------------------------------------------------------------

def status(input_url: str) -> bool:
    """True se a câmera está configurada para empurrar o RTMP."""
    return _read(input_url)[0]


def start(quadra_id: str, input_url: str, stream_key: str) -> bool:
    """Liga a live. Retorna False se já estava no ar com essa stream key (idempotente)."""
    url = _ingest_url(stream_key)
    with _lock:
        enabled, current = _read(input_url)
        if enabled and current == url:
            return False
        _write(input_url, True, url)
        if not _read(input_url)[0]:
            raise LiveError("a câmera aceitou o PUT mas não ficou com a live ligada.")
        _forget(quadra_id)
        if config.LIVE_MAX_SECONDS > 0:
            deadline = time.time() + config.LIVE_MAX_SECONDS
            _save_deadlines({**_load_deadlines(), quadra_id: deadline})
            _arm(quadra_id, input_url, deadline)
        return True


def stop(quadra_id: str, input_url: str) -> bool:
    """Desliga a live. Retorna False se a câmera já estava com a live desligada."""
    with _lock:
        enabled, _ = _read(input_url)
        if enabled:
            _write(input_url, False, OFF_URL)
        _forget(quadra_id)
        return enabled


def resume(cameras: dict[str, dict]) -> None:
    """Na subida da API: reassume os prazos gravados (os vencidos desligam já)."""
    with _lock:
        for quadra_id, deadline in _load_deadlines().items():
            camera = cameras.get(quadra_id)
            if camera is not None:
                _arm(quadra_id, camera["input_url"], deadline)


def cancel_timers() -> None:
    """No desligamento da API: só para os timers; a live continua na câmera."""
    with _lock:
        for timer in _timers.values():
            timer.cancel()
        _timers.clear()
