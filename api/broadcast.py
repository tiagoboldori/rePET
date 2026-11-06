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
STALL_SECONDS = 30.0  # ffmpeg vivo, mas sem avançar nenhum quadro por tanto tempo: está travado
STARTUP_GRACE_SECONDS = 45.0  # tolerância até o 1º quadro (o RTSP pode levar -timeout a falhar)
LOG_MAX_BYTES = 5 * 1024 * 1024

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
        self.stalls = 0  # quantas vezes o ffmpeg foi morto por travamento
        self.last_frame = -1  # último nº de quadro visto no -progress
        self.last_frame_change = time.time()  # quando o nº de quadro avançou pela última vez


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


def _int(value, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _sanitize(saved) -> dict | None:
    """Estado gravado -> estado válido; campo estragado vira o padrão (None se nem é um dict)."""
    if not isinstance(saved, dict):
        return None
    st = _new_state()
    nomes, placar = saved.get("nomes"), saved.get("placar")
    for side in SIDES:
        if isinstance(nomes, dict) and isinstance(nomes.get(side), str):
            st["nomes"][side] = _clean_name(nomes[side], DEFAULT_NAMES[side])
        score = placar.get(side) if isinstance(placar, dict) else None
        if isinstance(score, dict):
            for field in ("pontos", "sets"):
                st["placar"][side][field] = max(0, _int(score.get(field)))
    st["no_ar"] = saved.get("no_ar") is True
    st["volume"] = max(0, min(_int(saved.get("volume"), 100), config.BROADCAST_VOLUME_MAX))
    st["pid"] = saved["pid"] if _int(saved.get("pid"), 0) > 0 else None
    st["inicio"], st["prazo"] = _num(saved.get("inicio")), _num(saved.get("prazo"))
    return st


def _load() -> None:
    """Lê o estado gravado. Arquivo ilegível NUNCA derruba a API: é posto de lado (.corrupt)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    path = config.BROADCAST_STATE_FILE
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("não é um objeto")
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        print(f"[broadcast] estado ilegível ({type(exc).__name__}); ignorando e seguindo do zero.")
        try:
            os.replace(path, path.with_suffix(".corrupt"))
        except OSError:
            pass
        return
    for quadra_id, saved in data.items():
        st = _sanitize(saved)
        if st is not None and _QUADRA_RE.match(str(quadra_id)):
            _state[quadra_id] = st


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
# Cor da caixa do placar: grená (RGB 7B1E2B) a 85% de opacidade. Com 65% a cor se misturava ao fundo
# (marrom sobre a quadra verde, roxo sobre a azul); a 85% lê como grená em qualquer fundo.
SCOREBOARD_BOX_COLOR = "0x7B1E2B@0.85"


def _video_filters(quadra_id: str) -> list[str]:
    """Cadeia de vídeo: 1080p + caixa + textos do placar (canto inferior esquerdo)."""
    d, font = _dir(quadra_id), config.BROADCAST_FONT
    video = [
        # As câmeras não são iguais (quadra1 sai em 1080p, quadra2 em 1440p): tudo vira
        # 1080p antes do overlay, para as posições valerem e o encode custar o mesmo.
        "scale=1920:1080",
        # ATENÇÃO: no drawbox `h` é a ALTURA DA CAIXA, não do vídeo (no drawtext é a do vídeo).
        # Por isso a posição vertical usa `ih`; com `h-228` a caixa ia para o topo (y=-48).
        f"drawbox=x=48:y=ih-228:w=700:h=180:color={SCOREBOARD_BOX_COLOR}:t=fill",
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
    return video


# Redução de ruído do áudio da câmera (mono 16 kHz), medida em áudio real das quadras: o corte do grave tira o
# zumbido contínuo e o afftdn (com rastreio do ruído) rebaixa o ruído de fundo sem mexer em batidas e vozes altas.
# Custo medido: ~+1% de um núcleo por transmissão. Roda ANTES do volume (atua sempre no nível original da câmera)
# e ANTES do aresample para 44,1 kHz estéreo (processa 5,5x menos amostras).
DENOISE_LEVELS = {
    "leve": "highpass=f=100,afftdn=nr=10:nf=-50:tn=1",
    "medio": "highpass=f=100,afftdn=nr=14:nf=-45:tn=1",
    "forte": "highpass=f=100,afftdn=nr=25:nf=-35:tn=1",
}
_denoise_filter_ok: bool | None = None


def _denoise_available() -> bool:
    """O ffmpeg desta máquina tem o filtro afftdn? (checa uma vez). Sem ele a live seguiria falhando ao subir, então
    é melhor transmitir sem o filtro e avisar. Na dúvida (não conseguiu perguntar), não aplica."""
    global _denoise_filter_ok
    if _denoise_filter_ok is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=15)
            _denoise_filter_ok = out.returncode == 0 and " afftdn " in out.stdout
        except (OSError, subprocess.TimeoutExpired):
            _denoise_filter_ok = False
        if not _denoise_filter_ok:
            print("[broadcast] aviso: este ffmpeg não tem o filtro afftdn (ou não respondeu); "
                  "a redução de ruído do áudio fica desligada.")
    return _denoise_filter_ok


def _denoise_chain() -> str | None:
    """Cadeia de filtros do nível configurado em AUDIO_DENOISE, ou None se desligada/indisponível."""
    chain = DENOISE_LEVELS.get(config.AUDIO_DENOISE)
    return chain if chain and _denoise_available() else None


def _filter_graph(quadra_id: str, volume: int, audio_input: str = "0:a", denoise: str | None = None) -> str:
    pre = f"{denoise}," if denoise else ""
    audio = (
        f"{pre}volume@vol={volume / 100:.3f}:eval=frame,alimiter=limit=0.95,"
        "aresample=44100,aformat=channel_layouts=stereo"
    )
    return f"[0:v]{','.join(_video_filters(quadra_id))}[v];[{audio_input}]{audio}[a]"


def _probe_audio(input_url: str) -> bool:
    """True se a fonte tem trilha de áudio. Na dúvida (câmera não respondeu) diz True:
    o ffmpeg então falha e o supervisor tenta de novo, em vez de irmos ao ar mudos
    por causa de um tropeço do ffprobe."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", input_url],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if out.returncode != 0:
        return True
    return "audio" in out.stdout


def _ffmpeg_cmd(
    quadra_id: str, input_url: str, stream_key: str, volume: int, has_audio: bool = True,
    rtmp_url: str | None = None,
) -> list[str]:
    bitrate = config.BROADCAST_BITRATE
    # Câmera sem áudio: sem isto o ffmpeg sairia com "matches no streams" e o supervisor
    # relançaria para sempre, com a API dizendo no_ar. Vai silêncio (-re segura o ritmo
    # e -shortest encerra junto com o vídeo, senão o anullsrc infinito prenderia o ffmpeg).
    progress = ["-progress", str(_progress(quadra_id)), "-stats_period", "5"]
    silence = [] if has_audio else ["-re", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    return [
        "ffmpeg", "-loglevel", "warning", *progress,
        "-rtsp_transport", "tcp", "-timeout", "15000000",
        "-i", input_url, *silence,
        "-filter_complex", _filter_graph(
            quadra_id, volume, "0:a" if has_audio else "1:a", _denoise_chain() if has_audio else None
        ),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", config.BROADCAST_PRESET,
        "-threads", str(config.BROADCAST_THREADS),
        "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{2 * int(bitrate[:-1])}k",
        "-pix_fmt", "yuv420p", "-force_key_frames", "expr:gte(t,n_forced*2)",
        "-c:a", "aac", "-b:a", "128k",
        *([] if has_audio else ["-shortest"]),
        "-f", "flv", "-flvflags", "no_duration_filesize",
        f"{(rtmp_url or config.YOUTUBE_RTMP_URL).rstrip('/')}/{stream_key}",
    ]


def _fifo(quadra_id: str) -> Path:
    return _dir(quadra_id) / "cmd.fifo"


def _progress(quadra_id: str) -> Path:
    return _dir(quadra_id) / "progress.txt"


def _spawn(quadra_id: str, camera: dict, runtime: _Runtime, has_audio: bool) -> None:
    """Sobe o ffmpeg. `has_audio` vem de fora (a sondagem leva segundos e não pode
    acontecer com o lock global preso)."""
    st = _st(quadra_id)
    d = _dir(quadra_id)
    d.mkdir(parents=True, exist_ok=True)
    fifo = _fifo(quadra_id)
    if not fifo.exists():
        os.mkfifo(fifo, 0o600)
    config.BROADCAST_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.BROADCAST_LOG_DIR / f"broadcast_{quadra_id}.log"
    try:
        if log_path.stat().st_size > LOG_MAX_BYTES:
            os.replace(log_path, log_path.with_suffix(".log.1"))
    except OSError:
        pass
    _progress(quadra_id).write_text("")  # o -progress de um ffmpeg velho não pode enganar o watchdog
    runtime.has_audio = has_audio
    if not has_audio:
        print(f"[broadcast] {quadra_id}: a câmera não tem áudio; a transmissão vai com silêncio.")
    elif _denoise_chain():
        print(f"[broadcast] {quadra_id}: redução de ruído do áudio: {config.AUDIO_DENOISE}.")
    cmd = _ffmpeg_cmd(
        quadra_id, camera["input_url"], camera["youtube_stream_key"], st["volume"], has_audio,
        # Só repassa quando a quadra tem URL própria: sem ela a chamada é idêntica à de antes.
        **({"rtmp_url": camera["rtmp_url"]} if camera.get("rtmp_url") else {}),
    )
    # O FIFO é aberto em leitura+escrita: o ffmpeg nunca vê EOF no stdin, mesmo
    # que a API reinicie, e a API reabre só para escrita quando precisa.
    fifo_fd = os.open(fifo, os.O_RDWR)
    try:
        with open(log_path, "ab") as log:
            runtime.proc = subprocess.Popen(
                cmd, stdin=fifo_fd, stdout=log, stderr=log, start_new_session=True
            )
    except OSError as exc:
        raise BroadcastError(f"não consegui iniciar o ffmpeg ({type(exc).__name__}).", 500) from exc
    finally:
        os.close(fifo_fd)
    runtime.last_spawn = runtime.last_frame_change = time.time()
    runtime.last_frame = -1
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


def _read_frame(quadra_id: str) -> int | None:
    """Último 'frame=N' do -progress (o ffmpeg o reescreve a cada 5 s)."""
    try:
        with open(_progress(quadra_id), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(fh.tell() - 1024, 0))
            tail = fh.read().decode(errors="ignore")
    except OSError:
        return None
    found = re.findall(r"^frame=(\d+)", tail, flags=re.MULTILINE)
    return int(found[-1]) if found else None


def _is_stalled(quadra_id: str, runtime: _Runtime) -> bool:
    """Vivo mas sem produzir quadros. Olha o contador de quadros, não o arquivo: o ffmpeg
    segue escrevendo estatística mesmo com a entrada congelada ou a saída bloqueada."""
    now = time.time()
    frame = _read_frame(quadra_id)
    if frame is not None and frame != runtime.last_frame:
        runtime.last_frame, runtime.last_frame_change = frame, now
    if now - runtime.last_spawn < STARTUP_GRACE_SECONDS:
        return False
    return now - runtime.last_frame_change > STALL_SECONDS


def _kill(quadra_id: str, runtime: _Runtime) -> None:
    """SIGKILL direto (um ffmpeg travado, ou até parado, não atende ao 'q')."""
    pid = _st(quadra_id).get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if runtime.proc is not None:
        try:
            runtime.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


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
                if _is_stalled(quadra_id, runtime):
                    runtime.stalls += 1
                    print(f"[broadcast] {quadra_id}: ffmpeg travado (sem quadros há {STALL_SECONDS:.0f}s); matando.")
                    _kill(quadra_id, runtime)
                elif time.time() - runtime.last_spawn > HEALTHY_AFTER_SECONDS:
                    backoff = RESTART_BACKOFF_MIN
                continue
            print(f"[broadcast] {quadra_id}: ffmpeg parou; relançando em {backoff:.0f}s.")
        if runtime.stop.wait(backoff):
            return
        backoff = min(backoff * 2, RESTART_BACKOFF_MAX)
        has_audio = _probe_audio(camera["input_url"])  # fora do lock: com a câmera fora leva segundos
        with _lock:
            if runtime.stop.is_set():
                return
            try:
                runtime.restarts += 1
                _spawn(quadra_id, camera, runtime, has_audio)
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
        if _st(quadra_id)["no_ar"]:
            return False
        if not camera.get("youtube_stream_key"):
            raise BroadcastError("a quadra não tem youtube_stream_key em cameras.json.", 422)
    # A sondagem de áudio leva segundos (mais se a câmera estiver fora): nunca com o lock preso,
    # senão o placar das outras quadras trava junto.
    has_audio = _probe_audio(camera["input_url"])
    with _lock:
        st = _st(quadra_id)
        if st["no_ar"]:  # outra chamada ligou enquanto sondávamos
            return False
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
        _spawn(quadra_id, camera, runtime, has_audio)
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


def _scan_orphans() -> dict[str, list[int]]:
    """ffmpeg rodando com o overlay de alguma quadra (vistos em /proc), por quadra."""
    found: dict[str, list[int]] = {}
    pattern = re.compile(re.escape(str(config.BROADCAST_DIR)).encode() + rb"/([A-Za-z0-9_-]+)/")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            continue
        joined = cmdline.replace(b"\0", b" ")
        match = pattern.search(joined)
        if match and state != "Z" and b"ffmpeg" in joined and b"-progress" in joined:
            found.setdefault(match.group(1).decode(), []).append(int(entry.name))
    return found


def _read_overlay(quadra_id: str, st: dict) -> None:
    """Refaz nomes e placar a partir dos arquivos do overlay (é o que está NO AR agora)."""
    d = _dir(quadra_id)
    for side in SIDES:
        try:
            st["nomes"][side] = _clean_name((d / f"nome_{side}.txt").read_text(), DEFAULT_NAMES[side])
            for field, name in (("sets", "sets"), ("pontos", "pontos")):
                st["placar"][side][field] = max(0, int((d / f"{name}_{side}.txt").read_text()))
        except (OSError, ValueError):
            pass


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    time.sleep(1.0)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def resume(cameras: dict[str, dict]) -> dict:
    """Na subida da API, reconcilia o que está de fato rodando com o estado gravado:

    - readota o ffmpeg que segue vivo e relança o que caiu (o supervisor faz isso);
    - ffmpeg de transmissão SEM registro no estado (arquivo perdido) é readotado, com
      nomes e placar lidos do overlay, em vez de ficar publicando sem dono;
    - transmissão cuja quadra saiu do cameras.json (ou perdeu a stream key) é encerrada.
    Devolve um resumo para o log de subida.
    """
    summary = {"readotadas": [], "relancadas": [], "orfas_adotadas": [], "encerradas": []}
    with _lock:
        _load()
        orphans = _scan_orphans()
        for quadra_id, pids in orphans.items():
            st = _state.get(quadra_id)
            tracked = st["pid"] if st and st["no_ar"] else None
            for pid in pids:
                if pid == tracked:
                    continue
                camera = cameras.get(quadra_id)
                if camera is None or not camera.get("youtube_stream_key") or tracked is not None:
                    print(f"[broadcast] {quadra_id}: ffmpeg {pid} sem dono nem quadra; encerrando.")
                    _kill_pid(pid)
                    summary["encerradas"].append(quadra_id)
                    continue
                st = _st(quadra_id)
                _read_overlay(quadra_id, st)
                now = time.time()
                st.update(no_ar=True, pid=pid, inicio=now,
                          prazo=now + config.LIVE_MAX_SECONDS if config.LIVE_MAX_SECONDS > 0 else None)
                tracked = pid
                summary["orfas_adotadas"].append(quadra_id)
                print(f"[broadcast] {quadra_id}: ffmpeg {pid} sem registro; readotado com o overlay.")
        for quadra_id, st in list(_state.items()):
            if not st["no_ar"]:
                continue
            camera = cameras.get(quadra_id)
            if camera is None or not camera.get("youtube_stream_key"):
                if _pid_is_ours(st.get("pid"), quadra_id):
                    print(f"[broadcast] {quadra_id}: quadra saiu do cameras.json; encerrando o ffmpeg.")
                    _kill_pid(st["pid"])
                st.update(no_ar=False, pid=None, inicio=None, prazo=None)
                summary["encerradas"].append(quadra_id)
                continue
            runtime = _Runtime()
            if _pid_is_ours(st.get("pid"), quadra_id):
                runtime.last_spawn = runtime.last_frame_change = time.time()
                print(f"[broadcast] {quadra_id}: readotando ffmpeg pid {st['pid']}.")
                summary["readotadas"].append(quadra_id)
            else:
                summary["relancadas"].append(quadra_id)
            _launch_supervisor(quadra_id, camera, runtime)  # se não está vivo, ele relança
        _persist()
    return summary


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
            "travamentos": runtime.stalls if runtime else 0,
            # True só se o ffmpeg está vivo E produzindo quadros agora; no_ar=true com
            # transmitindo=false é "tentando": câmera/YouTube fora, ffmpeg reiniciando.
            "transmitindo": bool(runtime and _alive(quadra_id, runtime) and now - runtime.last_frame_change < 15
                                 and runtime.last_frame >= 0),
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
