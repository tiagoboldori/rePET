"""Transmissão ao vivo COM placar: o servidor reencoda e desenha o overlay.

Diferente de `api.live` (a câmera empurra o RTMP direto, sem nada no meio), aqui
um ffmpeg por quadra lê o RTSP, desenha o placar (nomes, sets, pontos) por cima do
vídeo e empurra o RTMP ao YouTube. Custo medido (quadra real, 1080p20, x264
ultrafast): ~1 núcleo e ~0,7 GB por transmissão.

Como as peças se encaixam:
- O placar é desenhado por `drawtext textfile=...:reload=1`: a API só regrava três
  arquivos de texto (nomes, sets, pontos) e o ffmpeg relê a cada quadro.
- O volume muda em tempo real mandando `c volume@vol -1 volume X` ao stdin do
  ffmpeg (um FIFO por quadra), sem derrubar a live.
- O ffmpeg roda em sessão própria (`start_new_session`) e com o FIFO aberto em
  leitura+escrita pelo próprio processo, então REINICIAR A API NÃO DERRUBA A LIVE:
  `resume()` readota o pid gravado em BROADCAST_STATE_FILE.
- Uma thread por transmissão supervisiona: relança o ffmpeg se ele morrer e
  encerra no prazo (LIVE_MAX_SECONDS).

Limite: BROADCAST_MAX_CONCURRENT transmissões ao mesmo tempo (padrão 2).
"""
import errno
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from api import config

SIDES = ("a", "b")
MAX_NAME_LENGTH = 14
DEFAULT_NAMES = {"a": "DUPLA A", "b": "DUPLA B"}
RESTART_BACKOFF_MIN = 3.0
RESTART_BACKOFF_MAX = 30.0
HEALTHY_AFTER_SECONDS = 60.0  # rodou isso tudo: zera o backoff
STOP_GRACE_SECONDS = 8.0

_QUADRA_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_lock = threading.RLock()
_state: dict[str, dict] = {}  # persistido em BROADCAST_STATE_FILE
_runtime: dict[str, "_Runtime"] = {}  # só em memória
_loaded = False


class BroadcastError(Exception):
    """Erro com mensagem segura para devolver na API e o status HTTP sugerido."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class _Runtime:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None  # None quando readotado
        self.thread: threading.Thread | None = None
        self.restarts = 0
        self.last_spawn = 0.0
        self.has_audio: bool | None = None  # None = ainda não sondado (ou readotado)


# --- estado persistido -------------------------------------------------------

def _new_state() -> dict:
    return {
        "no_ar": False,
        "nomes": dict(DEFAULT_NAMES),
        "placar": {s: {"pontos": 0, "sets": 0} for s in SIDES},
        "volume": 100,
        "pid": None,
        "inicio": None,
        "prazo": None,
    }


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        data = json.loads(config.BROADCAST_STATE_FILE.read_text())
    except (OSError, ValueError):
        return
    for quadra_id, saved in data.items():
        _state[quadra_id] = {**_new_state(), **saved}


def _persist() -> None:
    path = config.BROADCAST_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state))
    os.replace(tmp, path)


def _st(quadra_id: str) -> dict:
    if not _QUADRA_RE.match(quadra_id):
        raise BroadcastError("quadra_id inválido.", 422)
    _load()
    return _state.setdefault(quadra_id, _new_state())


# --- overlay -----------------------------------------------------------------

def _dir(quadra_id: str) -> Path:
    return config.BROADCAST_DIR / quadra_id


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # o ffmpeg nunca lê o arquivo pela metade


def _write_overlay(quadra_id: str) -> None:
    """Regrava os seis arquivos de uma linha que o drawtext relê (nome/sets/pontos × a/b).

    Uma linha por arquivo e SEM '\\n': o drawtext do ffmpeg 8 desenha a quebra de linha
    como um glifo de "caractere ausente" (quadradinho) ao lado do texto.
    """
    st = _st(quadra_id)
    d = _dir(quadra_id)
    d.mkdir(parents=True, exist_ok=True)
    for side in SIDES:
        _write_atomic(d / f"nome_{side}.txt", st["nomes"][side])
        _write_atomic(d / f"sets_{side}.txt", str(st["placar"][side]["sets"]))
        _write_atomic(d / f"pontos_{side}.txt", str(st["placar"][side]["pontos"]))


def _clean_name(raw: str, default: str) -> str:
    name = re.sub(r"[^\w .&'-]", "", raw or "", flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name).strip().upper()[:MAX_NAME_LENGTH].strip()
    return name or default


# --- ffmpeg ------------------------------------------------------------------

ROW_Y = {"a": "h-190", "b": "h-124"}  # duas linhas de 52 px, 66 px entre elas


def _filter_graph(quadra_id: str, volume: int, audio_input: str = "0:a") -> str:
    d, font = _dir(quadra_id), config.BROADCAST_FONT
    video = [
        # As câmeras não são iguais (quadra1 sai em 1080p, quadra2 em 1440p): tudo vira
        # 1080p antes do overlay, para as posições valerem e o encode custar o mesmo.
        "scale=1920:1080",
        "drawbox=x=48:y=h-228:w=700:h=180:color=black@0.65:t=fill",
        f"drawtext=fontfile={font}:text=SETS:expansion=none:x=500:y=h-218:fontsize=22:fontcolor=0xBBBBBB",
        f"drawtext=fontfile={font}:text=PONTOS:expansion=none:x=610:y=h-218:fontsize=22:fontcolor=0xBBBBBB",
    ]
    for side in SIDES:
        base = f"drawtext=fontfile={font}:reload=1:expansion=none:fontsize=52:y={ROW_Y[side]}"
        video += [
            f"{base}:textfile={d}/nome_{side}.txt:x=72:fontcolor=white",
            f"{base}:textfile={d}/sets_{side}.txt:x=525:fontcolor=yellow",
            f"{base}:textfile={d}/pontos_{side}.txt:x=640:fontcolor=white",
        ]
    audio = (
        f"volume@vol={volume / 100:.3f}:eval=frame,alimiter=limit=0.95,"
        "aresample=44100,aformat=channel_layouts=stereo"
    )
    return f"[0:v]{','.join(video)}[v];[{audio_input}]{audio}[a]"


def _probe_audio(input_url: str) -> bool:
    """True se a fonte tem trilha de áudio. Na dúvida (câmera não respondeu) diz True:
    o ffmpeg então falha e o supervisor tenta de novo, em vez de irmos ao ar mudos
    por causa de um tropeço do ffprobe."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", input_url],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if out.returncode != 0:
        return True
    return "audio" in out.stdout


def _ffmpeg_cmd(
    quadra_id: str, input_url: str, stream_key: str, volume: int, has_audio: bool = True
) -> list[str]:
    bitrate = config.BROADCAST_BITRATE
    # Câmera sem áudio: sem isto o ffmpeg sairia com "matches no streams" e o supervisor
    # relançaria para sempre, com a API dizendo no_ar. Vai silêncio (-re segura o ritmo
    # e -shortest encerra junto com o vídeo, senão o anullsrc infinito prenderia o ffmpeg).
    silence = [] if has_audio else ["-re", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    return [
        "ffmpeg", "-loglevel", "warning",
        "-rtsp_transport", "tcp", "-timeout", "15000000",
        "-i", input_url, *silence,
        "-filter_complex", _filter_graph(quadra_id, volume, "0:a" if has_audio else "1:a"),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", config.BROADCAST_PRESET,
        "-threads", str(config.BROADCAST_THREADS),
        "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{2 * int(bitrate[:-1])}k",
        "-pix_fmt", "yuv420p", "-force_key_frames", "expr:gte(t,n_forced*2)",
        "-c:a", "aac", "-b:a", "128k",
        *([] if has_audio else ["-shortest"]),
        "-f", "flv", "-flvflags", "no_duration_filesize",
        f"{config.YOUTUBE_RTMP_URL.rstrip('/')}/{stream_key}",
    ]


def _fifo(quadra_id: str) -> Path:
    return _dir(quadra_id) / "cmd.fifo"


def _spawn(quadra_id: str, camera: dict, runtime: _Runtime) -> None:
    st = _st(quadra_id)
    d = _dir(quadra_id)
    d.mkdir(parents=True, exist_ok=True)
    fifo = _fifo(quadra_id)
    if not fifo.exists():
        os.mkfifo(fifo, 0o600)
    config.BROADCAST_LOG_DIR.mkdir(parents=True, exist_ok=True)
    runtime.has_audio = _probe_audio(camera["input_url"])
    if not runtime.has_audio:
        print(f"[broadcast] {quadra_id}: a câmera não tem áudio; a transmissão vai com silêncio.")
    cmd = _ffmpeg_cmd(
        quadra_id, camera["input_url"], camera["youtube_stream_key"], st["volume"], runtime.has_audio
    )
    # O FIFO é aberto em leitura+escrita: o ffmpeg nunca vê EOF no stdin, mesmo
    # que a API reinicie, e a API reabre só para escrita quando precisa.
    fifo_fd = os.open(fifo, os.O_RDWR)
    try:
        with open(config.BROADCAST_LOG_DIR / f"broadcast_{quadra_id}.log", "ab") as log:
            runtime.proc = subprocess.Popen(
                cmd, stdin=fifo_fd, stdout=log, stderr=log, start_new_session=True
            )
    except OSError as exc:
        raise BroadcastError(f"não consegui iniciar o ffmpeg ({type(exc).__name__}).", 500) from exc
    finally:
        os.close(fifo_fd)
    runtime.last_spawn = time.time()
    st["pid"] = runtime.proc.pid
    _persist()


def _pid_is_ours(pid: int | None, quadra_id: str) -> bool:
    """True se o pid existe, não é zumbi e é o ffmpeg desta quadra."""
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    joined = b" ".join(cmdline)
    return state != "Z" and b"ffmpeg" in joined and str(_dir(quadra_id)).encode() in joined


def _alive(quadra_id: str, runtime: _Runtime) -> bool:
    if runtime.proc is not None:
        return runtime.proc.poll() is None
    return _pid_is_ours(_st(quadra_id).get("pid"), quadra_id)


def _send(quadra_id: str, line: str) -> bool:
    """Escreve no stdin do ffmpeg. False se não há ffmpeg lendo."""
    try:
        fd = os.open(_fifo(quadra_id), os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENXIO, errno.ENOENT):
            return False
        raise
    try:
        os.write(fd, line.encode())
        return True
    finally:
        os.close(fd)


def _terminate(quadra_id: str, runtime: _Runtime) -> None:
    """Pede 'q' ao ffmpeg (fecha o RTMP direito); se não sair, SIGTERM e depois SIGKILL."""
    pid = _st(quadra_id).get("pid")
    if not _alive(quadra_id, runtime):
        return
    _send(quadra_id, "q\n")
    deadline = time.time() + STOP_GRACE_SECONDS
    while time.time() < deadline and _alive(quadra_id, runtime):
        time.sleep(0.2)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _alive(quadra_id, runtime):
            break
        try:
            os.kill(pid, sig)
        except OSError:
            break
        time.sleep(1.5)
    if runtime.proc is not None:
        runtime.proc.poll()


# --- supervisor --------------------------------------------------------------

def _supervise(quadra_id: str, camera: dict, runtime: _Runtime) -> None:
    backoff = RESTART_BACKOFF_MIN
    while not runtime.stop.wait(1.0):
        with _lock:
            if runtime.stop.is_set():  # stop() rodou enquanto esperávamos o lock
                return
            st = _st(quadra_id)
            if st["prazo"] and time.time() >= st["prazo"]:
                print(f"[broadcast] {quadra_id}: LIVE_MAX_SECONDS atingido, encerrando.")
                _finish(quadra_id, runtime)
                return
            if _alive(quadra_id, runtime):
                if time.time() - runtime.last_spawn > HEALTHY_AFTER_SECONDS:
                    backoff = RESTART_BACKOFF_MIN
                continue
            print(f"[broadcast] {quadra_id}: ffmpeg parou; relançando em {backoff:.0f}s.")
        if runtime.stop.wait(backoff):
            return
        backoff = min(backoff * 2, RESTART_BACKOFF_MAX)
        with _lock:
            if runtime.stop.is_set():
                return
            try:
                runtime.restarts += 1
                _spawn(quadra_id, camera, runtime)
            except BroadcastError as exc:
                print(f"[broadcast] {quadra_id}: falha ao relançar: {exc}")


def _finish(quadra_id: str, runtime: _Runtime) -> None:
    """Para o ffmpeg e marca fora do ar (com _lock). Não faz join da thread."""
    runtime.stop.set()
    _terminate(quadra_id, runtime)
    st = _st(quadra_id)
    st.update(no_ar=False, pid=None, inicio=None, prazo=None)
    _runtime.pop(quadra_id, None)
    _persist()


def _launch_supervisor(quadra_id: str, camera: dict, runtime: _Runtime) -> None:
    runtime.thread = threading.Thread(
        target=_supervise, args=(quadra_id, camera, runtime), daemon=True, name=f"broadcast-{quadra_id}"
    )
    runtime.thread.start()
    _runtime[quadra_id] = runtime


# --- API pública: ciclo de vida ----------------------------------------------

def is_on_air(quadra_id: str) -> bool:
    with _lock:
        return bool(_st(quadra_id)["no_ar"])


def start(
    quadra_id: str, camera: dict, nomes: dict | None = None, reset: bool = True, volume: int | None = None
) -> bool:
    """Liga a transmissão. False se já estava no ar (idempotente).

    `reset` (jogo novo) zera o placar E devolve o volume a 100%: sem isso, um
    "volume 0" do jogo anterior faria a live nascer muda. `volume` escolhe outro valor.
    """
    with _lock:
        st = _st(quadra_id)
        if st["no_ar"]:
            return False
        if not camera.get("youtube_stream_key"):
            raise BroadcastError("a quadra não tem youtube_stream_key em cameras.json.", 422)
        on_air = sum(1 for s in _state.values() if s["no_ar"])
        if on_air >= config.BROADCAST_MAX_CONCURRENT:
            raise BroadcastError(
                f"limite de {config.BROADCAST_MAX_CONCURRENT} transmissões simultâneas atingido."
            )
        if reset:
            st["placar"] = {s: {"pontos": 0, "sets": 0} for s in SIDES}
            st["volume"] = 100
        if volume is not None:
            st["volume"] = max(0, min(int(volume), config.BROADCAST_VOLUME_MAX))
        if nomes is not None:
            for side in SIDES:
                if nomes.get(side) is not None:
                    st["nomes"][side] = _clean_name(nomes[side], DEFAULT_NAMES[side])
        _write_overlay(quadra_id)
        runtime = _Runtime()
        _spawn(quadra_id, camera, runtime)
        now = time.time()
        st.update(no_ar=True, inicio=now, prazo=now + config.LIVE_MAX_SECONDS if config.LIVE_MAX_SECONDS > 0 else None)
        _persist()
        _launch_supervisor(quadra_id, camera, runtime)
        return True


def stop(quadra_id: str) -> bool:
    """Encerra a transmissão. False se já estava fora do ar."""
    with _lock:
        st = _st(quadra_id)
        runtime = _runtime.get(quadra_id)
        if not st["no_ar"]:
            return False
        if runtime is None:  # estado gravado sem supervisor (ex.: resume não rodou)
            runtime = _Runtime()
        _finish(quadra_id, runtime)
        return True


def resume(cameras: dict[str, dict]) -> None:
    """Na subida da API: readota os ffmpeg que seguem rodando e relança os que caíram."""
    with _lock:
        _load()
        for quadra_id, st in list(_state.items()):
            if not st["no_ar"]:
                continue
            camera = cameras.get(quadra_id)
            if camera is None or not camera.get("youtube_stream_key"):
                st.update(no_ar=False, pid=None, inicio=None, prazo=None)
                continue
            runtime = _Runtime()
            if _pid_is_ours(st.get("pid"), quadra_id):
                runtime.last_spawn = time.time()
                print(f"[broadcast] {quadra_id}: readotando ffmpeg pid {st['pid']}.")
            _launch_supervisor(quadra_id, camera, runtime)  # se não está vivo, ele relança
        _persist()


def detach() -> None:
    """No desligamento da API: para só as threads; os ffmpeg continuam no ar."""
    with _lock:
        for runtime in _runtime.values():
            runtime.stop.set()
        _runtime.clear()


# --- API pública: placar, nomes e volume --------------------------------------

def _check_side(side: str) -> str:
    side = side.lower()
    if side not in SIDES:
        raise BroadcastError("lado deve ser 'a' ou 'b'.", 422)
    return side


def add_point(quadra_id: str, side: str, delta: int = 1) -> dict:
    side = _check_side(side)
    with _lock:
        score = _st(quadra_id)["placar"][side]
        score["pontos"] = max(0, score["pontos"] + delta)
        _write_overlay(quadra_id)
        _persist()
        return snapshot(quadra_id)


def add_set(quadra_id: str, side: str, delta: int = 1) -> dict:
    """+1 set e zera os pontos dos dois lados; delta negativo só corrige o set."""
    side = _check_side(side)
    with _lock:
        placar = _st(quadra_id)["placar"]
        placar[side]["sets"] = max(0, placar[side]["sets"] + delta)
        if delta > 0:
            for s in SIDES:
                placar[s]["pontos"] = 0
        _write_overlay(quadra_id)
        _persist()
        return snapshot(quadra_id)


def set_score(quadra_id: str, values: dict[str, int | None]) -> dict:
    """Correção manual: pontos_a, pontos_b, sets_a, sets_b (os que vierem)."""
    with _lock:
        placar = _st(quadra_id)["placar"]
        for key, value in values.items():
            if value is None:
                continue
            field, side = key.split("_")
            placar[side][field] = max(0, int(value))
        _write_overlay(quadra_id)
        _persist()
        return snapshot(quadra_id)


def reset_score(quadra_id: str) -> dict:
    with _lock:
        _st(quadra_id)["placar"] = {s: {"pontos": 0, "sets": 0} for s in SIDES}
        _write_overlay(quadra_id)
        _persist()
        return snapshot(quadra_id)


def set_names(quadra_id: str, nome_a: str | None, nome_b: str | None) -> dict:
    with _lock:
        nomes = _st(quadra_id)["nomes"]
        for side, raw in (("a", nome_a), ("b", nome_b)):
            if raw is not None:
                nomes[side] = _clean_name(raw, DEFAULT_NAMES[side])
        _write_overlay(quadra_id)
        _persist()
        return snapshot(quadra_id)


def set_volume(quadra_id: str, level: int) -> dict:
    """Volume em % do original (100 = igual à câmera). Vale na hora, com a live no ar."""
    level = max(0, min(int(level), config.BROADCAST_VOLUME_MAX))
    with _lock:
        st = _st(quadra_id)
        st["volume"] = level
        _persist()  # grava antes: se o ffmpeg reiniciar, nasce já com este volume
        if st["no_ar"] and not _send(quadra_id, f"cvolume@vol -1 volume {level / 100:.3f}\n"):
            raise BroadcastError("o ffmpeg não está lendo comandos agora (reiniciando?); tente de novo.", 503)
        return snapshot(quadra_id)


def change_volume(quadra_id: str, direction: int, step: int | None = None) -> dict:
    step = config.BROADCAST_VOLUME_STEP if step is None else step
    with _lock:
        return set_volume(quadra_id, _st(quadra_id)["volume"] + direction * abs(step))


def snapshot(quadra_id: str) -> dict:
    with _lock:
        st = _st(quadra_id)
        runtime = _runtime.get(quadra_id)
        now = time.time()
        return {
            "quadra_id": quadra_id,
            "no_ar": st["no_ar"],
            "nomes": dict(st["nomes"]),
            "placar": {s: dict(st["placar"][s]) for s in SIDES},
            "volume": st["volume"],
            "no_ar_ha_segundos": round(now - st["inicio"]) if st["inicio"] else None,
            "encerra_em_segundos": max(round(st["prazo"] - now), 0) if st["prazo"] else None,
            "reinicios": runtime.restarts if runtime else 0,
            # False = a câmera está sem áudio e a live vai com silêncio; None = não sondado
            "audio_da_camera": runtime.has_audio if runtime else None,
        }


def list_all() -> dict:
    with _lock:
        _load()
        on_air = [q for q, s in _state.items() if s["no_ar"]]
        return {
            "no_ar": on_air,
            "limite": config.BROADCAST_MAX_CONCURRENT,
            "transmissoes": {q: snapshot(q) for q in _state},
        }
