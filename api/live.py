"""Transmissão ao vivo no YouTube: um ffmpeg por quadra, RTSP -> RTMP.

O vídeo da câmera é repassado sem reencode (`-c:v copy`); como as câmeras
não têm áudio e o YouTube exige trilha de áudio, entra uma trilha de
silêncio. A stream key de cada quadra vem de `youtube_stream_key` em
config/cameras.json.

Se o ffmpeg cair (câmera derrubou o RTSP, rede), um supervisor o relança
até a live ser encerrada ou estourar LIVE_MAX_SECONDS.
"""
import subprocess
import threading
import time
from pathlib import Path

from api import config

_lock = threading.Lock()
_procs: dict[str, subprocess.Popen] = {}
_timers: dict[str, threading.Timer] = {}
_stops: dict[str, threading.Event] = {}

RESTART_DELAY_SECONDS = 3


class LiveError(Exception):
    """Falha ao iniciar a transmissão (mensagem segura para devolver na API)."""


def _build_cmd(input_url: str, stream_key: str) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-loglevel", "warning",
        # -timeout: sem dados da câmera por 10s o ffmpeg sai (e o supervisor relança).
        "-rtsp_transport", "tcp", "-timeout", "10000000", "-i", input_url,
        "-re", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        # O silêncio é infinito: sem -shortest o ffmpeg nunca sairia ao
        # acabar o vídeo e ficaria preso, "no ar" mas sem transmitir.
        "-shortest",
        "-f", "flv", f"{config.YOUTUBE_RTMP_URL.rstrip('/')}/{stream_key}",
    ]


def _alive(quadra_id: str) -> bool:
    # "No ar" = há supervisor ativo; o ffmpeg pode estar no meio de um relançamento.
    return quadra_id in _stops


def is_live(quadra_id: str) -> bool:
    with _lock:
        return _alive(quadra_id)


def _spawn(quadra_id: str, input_url: str, stream_key: str) -> subprocess.Popen:
    log_dir: Path = config.LIVE_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    # stderr do ffmpeg vai pra arquivo; o comando (com a key) nunca é logado.
    with open(log_dir / f"live_{quadra_id}.log", "ab") as log:
        return subprocess.Popen(
            _build_cmd(input_url, stream_key),
            stdout=subprocess.DEVNULL, stderr=log, start_new_session=True,
        )


def _supervise(quadra_id: str, input_url: str, stream_key: str, stop_event: threading.Event) -> None:
    """Relança o ffmpeg quando ele cai, até a live ser encerrada."""
    while True:
        proc = _procs.get(quadra_id)
        if proc is not None:
            proc.wait()
        if stop_event.wait(RESTART_DELAY_SECONDS):
            return
        try:
            proc = _spawn(quadra_id, input_url, stream_key)
        except OSError:
            continue
        with _lock:
            if stop_event.is_set():
                proc.terminate()
                return
            _procs[quadra_id] = proc


def start(quadra_id: str, input_url: str, stream_key: str) -> bool:
    """Inicia a live. Retorna False se já estava no ar (idempotente)."""
    with _lock:
        if _alive(quadra_id):
            return False

        proc = _spawn(quadra_id, input_url, stream_key)

        # Falha imediata (RTSP inacessível, key recusada) aparece em ~1s.
        time.sleep(1.5)
        if proc.poll() is not None:
            raise LiveError(
                f"ffmpeg encerrou ao iniciar (código {proc.returncode}); "
                f"veja {config.LIVE_LOG_DIR / f'live_{quadra_id}.log'}."
            )

        _procs[quadra_id] = proc
        stop_event = threading.Event()
        _stops[quadra_id] = stop_event
        threading.Thread(
            target=_supervise, args=(quadra_id, input_url, stream_key, stop_event), daemon=True,
        ).start()
        if config.LIVE_MAX_SECONDS > 0:
            timer = threading.Timer(config.LIVE_MAX_SECONDS, stop, args=(quadra_id,))
            timer.daemon = True
            timer.start()
            _timers[quadra_id] = timer
        return True


def stop(quadra_id: str) -> bool:
    """Encerra a live. Retorna False se não havia nenhuma no ar."""
    with _lock:
        stop_event = _stops.pop(quadra_id, None)
        if stop_event is not None:
            stop_event.set()
        timer = _timers.pop(quadra_id, None)
        if timer is not None:
            timer.cancel()
        proc = _procs.pop(quadra_id, None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return stop_event is not None


def stop_all() -> None:
    for quadra_id in list(_stops):
        stop(quadra_id)
