"""Transmissão ao vivo no YouTube: um ffmpeg por quadra, RTSP -> RTMP.

O vídeo da câmera é repassado sem reencode (`-c:v copy`); como as câmeras
não têm áudio e o YouTube exige trilha de áudio, entra uma trilha de
silêncio. A stream key de cada quadra vem de `youtube_stream_key` em
config/cameras.json.
"""
import subprocess
import threading
import time
from pathlib import Path

from api import config

_lock = threading.Lock()
_procs: dict[str, subprocess.Popen] = {}
_timers: dict[str, threading.Timer] = {}


class LiveError(Exception):
    """Falha ao iniciar a transmissão (mensagem segura para devolver na API)."""


def _build_cmd(input_url: str, stream_key: str) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-loglevel", "warning",
        "-rtsp_transport", "tcp", "-i", input_url,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
        "-f", "flv", f"{config.YOUTUBE_RTMP_URL.rstrip('/')}/{stream_key}",
    ]


def _alive(quadra_id: str) -> bool:
    proc = _procs.get(quadra_id)
    if proc is not None and proc.poll() is None:
        return True
    _procs.pop(quadra_id, None)
    return False


def is_live(quadra_id: str) -> bool:
    with _lock:
        return _alive(quadra_id)


def start(quadra_id: str, input_url: str, stream_key: str) -> bool:
    """Inicia a live. Retorna False se já estava no ar (idempotente)."""
    with _lock:
        if _alive(quadra_id):
            return False

        log_dir: Path = config.LIVE_LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        # stderr do ffmpeg vai pra arquivo; o comando (com a key) nunca é logado.
        with open(log_dir / f"live_{quadra_id}.log", "ab") as log:
            proc = subprocess.Popen(
                _build_cmd(input_url, stream_key),
                stdout=subprocess.DEVNULL, stderr=log, start_new_session=True,
            )

        # Falha imediata (RTSP inacessível, key recusada) aparece em ~1s.
        time.sleep(1.5)
        if proc.poll() is not None:
            raise LiveError(
                f"ffmpeg encerrou ao iniciar (código {proc.returncode}); "
                f"veja {log_dir / f'live_{quadra_id}.log'}."
            )

        _procs[quadra_id] = proc
        if config.LIVE_MAX_SECONDS > 0:
            timer = threading.Timer(config.LIVE_MAX_SECONDS, stop, args=(quadra_id,))
            timer.daemon = True
            timer.start()
            _timers[quadra_id] = timer
        return True


def stop(quadra_id: str) -> bool:
    """Encerra a live. Retorna False se não havia nenhuma no ar."""
    with _lock:
        timer = _timers.pop(quadra_id, None)
        if timer is not None:
            timer.cancel()
        proc = _procs.pop(quadra_id, None)
    if proc is None or proc.poll() is not None:
        return False
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return True


def stop_all() -> None:
    for quadra_id in list(_procs):
        stop(quadra_id)
