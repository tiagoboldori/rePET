"""Transmissão ao vivo no YouTube: um ffmpeg por quadra, RTSP -> RTMP.

O vídeo da câmera é repassado sem reencode (`-c:v copy`) e o áudio dela
(som ambiente) é recodificado em AAC. O YouTube exige trilha de áudio, então
se a câmera estiver sem áudio entra uma trilha de silêncio no lugar. A
stream key de cada quadra vem de `youtube_stream_key` em config/cameras.json.

Se o ffmpeg cair (câmera derrubou o RTSP, rede), um supervisor o relança
até a live ser encerrada ou estourar LIVE_MAX_SECONDS.
"""
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from api import config

_lock = threading.Lock()
_procs: dict[str, subprocess.Popen] = {}
_timers: dict[str, threading.Timer] = {}
_stops: dict[str, threading.Event] = {}

RESTART_DELAY_SECONDS = 3
# Quanto esperar para considerar que o ffmpeg subiu (falha imediata aparece em ~1s).
STARTUP_CHECK_SECONDS = 1.5
AUDIO_PROBE_TIMEOUT_SECONDS = 15


class LiveError(Exception):
    """Falha ao iniciar a transmissão (mensagem segura para devolver na API)."""


def _build_cmd(input_url: str, stream_key: str, camera_audio: bool) -> list[str]:
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "warning",
        # -timeout: sem dados da câmera por 10s o ffmpeg sai (e o supervisor relança).
        "-rtsp_transport", "tcp", "-timeout", "10000000", "-i", input_url,
    ]
    if camera_audio:
        # A câmera entrega AAC mono 16 kHz; o YouTube prefere estéreo 44,1 kHz.
        cmd += [
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
        ]
    else:
        cmd += [
            "-re", "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            # O silêncio é infinito: sem -shortest o ffmpeg nunca sairia ao
            # acabar o vídeo e ficaria preso, "no ar" mas sem transmitir.
            "-shortest",
        ]
    return cmd + ["-f", "flv", f"{config.YOUTUBE_RTMP_URL.rstrip('/')}/{stream_key}"]


def _camera_has_audio(input_url: str) -> bool:
    """False só quando o ffprobe confirma que o RTSP não traz trilha de áudio.

    Falha do próprio ffprobe (câmera fora do ar, timeout) conta como "tem": nesse
    caso o ffmpeg da live acusa o problema de conexão e o supervisor tenta de novo."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-rtsp_transport", "tcp", "-timeout", "10000000",
                "-select_streams", "a", "-show_entries", "stream=index",
                "-of", "csv=p=0", input_url,
            ],
            capture_output=True, text=True, timeout=AUDIO_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return result.returncode != 0 or bool(result.stdout.strip())


def _alive(quadra_id: str) -> bool:
    # "No ar" = há supervisor ativo; o ffmpeg pode estar no meio de um relançamento.
    return quadra_id in _stops


def is_live(quadra_id: str) -> bool:
    with _lock:
        return _alive(quadra_id)


def _spawn(quadra_id: str, input_url: str, stream_key: str, camera_audio: bool) -> subprocess.Popen:
    log_dir: Path = config.LIVE_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    # stderr do ffmpeg vai pra arquivo; o comando (com a key) nunca é logado.
    with open(log_dir / f"live_{quadra_id}.log", "ab") as log:
        # O ffmpeg não carimba o tempo: esta linha marca cada (re)lançamento e o áudio usado.
        audio = "câmera" if camera_audio else "silêncio"
        log.write(f"[live] {datetime.now():%Y-%m-%d %H:%M:%S} ffmpeg iniciado (áudio: {audio})\n".encode())
        log.flush()
        return subprocess.Popen(
            _build_cmd(input_url, stream_key, camera_audio),
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
        # Reavalia o áudio a cada relançamento: a câmera pode ter ganhado ou perdido a trilha.
        camera_audio = _camera_has_audio(input_url)
        if stop_event.is_set():
            return
        try:
            proc = _spawn(quadra_id, input_url, stream_key, camera_audio)
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

        camera_audio = _camera_has_audio(input_url)
        proc = _spawn(quadra_id, input_url, stream_key, camera_audio)

        time.sleep(STARTUP_CHECK_SECONDS)
        if proc.poll() is not None and camera_audio:
            # Algo no áudio da câmera derrubou o ffmpeg: tenta de novo com silêncio.
            proc = _spawn(quadra_id, input_url, stream_key, camera_audio=False)
            time.sleep(STARTUP_CHECK_SECONDS)
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
