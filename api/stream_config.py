"""Destino da transmissão de cada quadra: stream key e URL RTMP (GET/PUT /quadras/{id}/youtube).

Os dois valores moram em config/cameras.json (`youtube_stream_key`, e `rtmp_url` opcional; sem ele
vale a global YOUTUBE_RTMP_URL). Este módulo lê e grava esse arquivo, e nunca devolve a key inteira.

Regras que existem por segurança:
- A key só é aceita se tiver o formato de uma stream key (`[A-Za-z0-9_-]{8,128}`): ela vai para uma URL
  e para um XML, então nada de `/`, `?`, `<`, espaço etc.
- A URL RTMP só vale com esquema `rtmp://`, sem usuário/senha/query, e com host numa lista: o servidor
  e a câmera passam a empurrar o vídeo para onde ela apontar (RTMP_ALLOWED_HOSTS, padrão youtube.com,
  mais o host da YOUTUBE_RTMP_URL global). IP só vale se estiver listado por inteiro.
- Não troca nada com a quadra no ar (live direta ou com placar): o ffmpeg e o supervisor guardam o
  destino com que subiram, então a troca ficaria inconsistente e, num reinício da API, o vídeo mudaria de
  canal sozinho.
- A gravação é atômica (arquivo temporário + rename) e preserva modo, ordem e formato do arquivo.
"""
import ipaddress
import json
import os
import re
import stat
import tempfile
import threading
from urllib.parse import urlparse

from api import broadcast, config, live, monitor

_lock = threading.Lock()
UNSET = object()  # campo que o cliente não enviou (diferente de null)

_KEY_RE = re.compile(r"[A-Za-z0-9_-]{8,128}")
_HOST_RE = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")
_PATH_RE = re.compile(r"(/[A-Za-z0-9_.-]+)*")
_MAX_URL = 200


class StreamConfigError(Exception):
    """Erro com mensagem segura (nunca contém a key) e o status HTTP sugerido."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def mask_key(key: str | None) -> str | None:
    """Só os 4 últimos caracteres, e só se a key for longa o bastante para isso não ajudar a adivinhá-la."""
    if not key or len(key) < 16:
        return None
    return key[-4:]


def default_rtmp_url() -> str:
    return config.YOUTUBE_RTMP_URL.rstrip("/")


def allowed_hosts() -> list[str]:
    hosts = list(config.RTMP_ALLOWED_HOSTS)
    default_host = urlparse(config.YOUTUBE_RTMP_URL).hostname
    if default_host and default_host.lower() not in hosts:
        hosts.append(default_host.lower())
    return hosts


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _host_allowed(host: str) -> bool:
    """Domínio: igual a uma entrada ou subdomínio dela. IP (do host ou da entrada): só igualdade
    exata, nunca "termina com" (senão `1.203.0.113.7` passaria por `203.0.113.7`)."""
    for entry in allowed_hosts():
        if host == entry:
            return True
        if not _is_ip(host) and not _is_ip(entry) and host.endswith("." + entry):
            return True
    return False


def validate_key(raw: object) -> str:
    key = raw.strip() if isinstance(raw, str) else ""
    if not _KEY_RE.fullmatch(key):
        raise StreamConfigError(
            "stream_key inválida: use de 8 a 128 caracteres entre letras, números, '-' e '_' (sem espaços)."
        )
    return key


def validate_rtmp_url(raw: object) -> str:
    """Devolve a URL normalizada (host em minúsculas, sem barra no fim) ou levanta StreamConfigError."""
    url = raw.strip() if isinstance(raw, str) else ""
    if not url or len(url) > _MAX_URL or re.search(r"\s", url):
        raise StreamConfigError("rtmp_url inválida: vazia, longa demais ou com espaços.")
    parsed = urlparse(url)
    if parsed.scheme != "rtmp":
        raise StreamConfigError("rtmp_url deve começar com rtmp:// (rtmps:// não é aceito).")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.params:
        raise StreamConfigError("rtmp_url não pode ter usuário, senha, query ou fragmento; a stream key vai em stream_key.")
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError as exc:
        raise StreamConfigError("rtmp_url com porta inválida.") from exc
    if not host or not _HOST_RE.fullmatch(host):
        raise StreamConfigError("rtmp_url com host inválido.")
    path = parsed.path.rstrip("/")
    if not _PATH_RE.fullmatch(path):
        raise StreamConfigError("rtmp_url com caminho inválido (use só letras, números, '.', '_' e '-').")
    if not _host_allowed(host):
        raise StreamConfigError(
            f"host '{host}' não permitido; hosts aceitos: {', '.join(allowed_hosts())} "
            "(ajuste RTMP_ALLOWED_HOSTS no servidor para liberar outro)."
        )
    return f"rtmp://{host}{f':{port}' if port else ''}{path}"


def _camera_on_air(quadra_id: str, camera: dict, fresh: bool) -> bool:
    if broadcast.is_on_air(quadra_id):
        return True
    input_url = str(camera.get("input_url", ""))
    if fresh and input_url.startswith("rtsp://"):
        try:
            return bool(live.status(input_url))
        except live.LiveError:
            pass  # câmera muda: vale o último estado conhecido, abaixo
    return bool(monitor.get(quadra_id)["no_ar"])


def describe(quadra_id: str, camera: dict, cameras: dict[str, dict]) -> dict:
    """Visão pública da configuração de uma quadra (sem a key)."""
    key = camera.get("youtube_stream_key")
    return {
        "quadra_id": quadra_id,
        "tem_stream_key": bool(key),
        "stream_key_final": mask_key(key),
        "rtmp_url": (camera.get("rtmp_url") or default_rtmp_url()).rstrip("/"),
        "rtmp_url_padrao": not camera.get("rtmp_url"),
        "rtmp_hosts_permitidos": allowed_hosts(),
        "chave_compartilhada_com": monitor.shared_key_with(quadra_id, cameras),
        "chave_em_uso_por": monitor.key_in_use_by(quadra_id, cameras),
        "no_ar": _camera_on_air(quadra_id, camera, fresh=False),
    }


def get(quadra_id: str) -> dict:
    cameras = config.load_cameras()
    camera = cameras.get(quadra_id)
    if camera is None:
        raise StreamConfigError(f"quadra_id '{quadra_id}' não está em {config.CAMERAS_FILE}.", 404)
    return describe(quadra_id, camera, cameras)


def key_view(quadra_id: str, camera: dict, cameras: dict[str, dict]) -> dict:
    """Visão da stream key. DEVOLVE A KEY INTEIRA: é a rota feita para isso (rede fechada, exige token)."""
    key = camera.get("youtube_stream_key")
    return {
        "quadra_id": quadra_id,
        "tem_stream_key": bool(key),
        "stream_key": key or None,
        "chave_compartilhada_com": monitor.shared_key_with(quadra_id, cameras),
        "no_ar": _camera_on_air(quadra_id, camera, fresh=False),
    }


def url_view(quadra_id: str, camera: dict, cameras: dict[str, dict]) -> dict:
    """Visão da URL RTMP: a efetiva, se é a global, e a global."""
    return {
        "quadra_id": quadra_id,
        "rtmp_url": (camera.get("rtmp_url") or default_rtmp_url()).rstrip("/"),
        "rtmp_url_padrao": not camera.get("rtmp_url"),
        "rtmp_url_global": default_rtmp_url(),
        "rtmp_hosts_permitidos": allowed_hosts(),
        "no_ar": _camera_on_air(quadra_id, camera, fresh=False),
    }


def _get_with(quadra_id: str, view) -> dict:
    cameras = config.load_cameras()
    camera = cameras.get(quadra_id)
    if camera is None:
        raise StreamConfigError(f"quadra_id '{quadra_id}' não está em {config.CAMERAS_FILE}.", 404)
    return view(quadra_id, camera, cameras)


def get_key(quadra_id: str) -> dict:
    return _get_with(quadra_id, key_view)


def get_url(quadra_id: str) -> dict:
    return _get_with(quadra_id, url_view)


def _write_atomic(path, data) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _apply(quadra_id: str, stream_key: object, rtmp_url: object) -> tuple[dict, dict[str, dict], bool]:
    """Valida, confere que a quadra não está no ar e grava. Devolve (quadra, todas as quadras, mudou?).

    `rtmp_url=None` apaga o valor da quadra (volta à global). `stream_key` não pode ser apagada.
    """
    if stream_key is UNSET and rtmp_url is UNSET:
        raise StreamConfigError("informe stream_key e/ou rtmp_url.")
    new_key = new_url = None
    if stream_key is not UNSET:
        if stream_key is None:
            raise StreamConfigError("stream_key não pode ser nula: a API troca a key, não a remove.")
        new_key = validate_key(stream_key)
    if rtmp_url is not UNSET and rtmp_url is not None:
        new_url = validate_rtmp_url(rtmp_url)

    cameras = config.load_cameras()
    camera = cameras.get(quadra_id)
    if camera is None:
        raise StreamConfigError(f"quadra_id '{quadra_id}' não está em {config.CAMERAS_FILE}.", 404)
    if _camera_on_air(quadra_id, camera, fresh=True):
        raise StreamConfigError(
            f"'{quadra_id}' está transmitindo; encerre a transmissão antes de trocar a key ou a URL RTMP.", 409
        )

    with _lock:
        path = config.CAMERAS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = next(c for c in data if c.get("quadra_id") == quadra_id)
        except (OSError, ValueError, StopIteration, AttributeError) as exc:
            raise StreamConfigError("não consegui ler a configuração das câmeras.", 500) from exc
        changed = False
        if new_key is not None and entry.get("youtube_stream_key") != new_key:
            entry["youtube_stream_key"] = new_key
            changed = True
        if rtmp_url is None and "rtmp_url" in entry:
            del entry["rtmp_url"]
            changed = True
        elif new_url is not None and entry.get("rtmp_url") != new_url:
            entry["rtmp_url"] = new_url
            changed = True
        if changed:
            try:
                _write_atomic(path, data)
            except OSError as exc:
                raise StreamConfigError("não consegui gravar a configuração das câmeras.", 500) from exc
        cameras = {c["quadra_id"]: c for c in data}
    return cameras[quadra_id], cameras, changed


def update(quadra_id: str, stream_key: object = UNSET, rtmp_url: object = UNSET) -> tuple[dict, bool]:
    """Troca a key e/ou a URL RTMP. Devolve (visão combinada, houve mudança?)."""
    camera, cameras, changed = _apply(quadra_id, stream_key, rtmp_url)
    return describe(quadra_id, camera, cameras), changed


def update_key(quadra_id: str, stream_key: object) -> tuple[dict, bool]:
    """Troca só a key. Devolve (visão da key, houve mudança?)."""
    camera, cameras, changed = _apply(quadra_id, stream_key, UNSET)
    return key_view(quadra_id, camera, cameras), changed


def update_url(quadra_id: str, rtmp_url: object) -> tuple[dict, bool]:
    """Troca só a URL (`None` volta à global). Devolve (visão da URL, houve mudança?)."""
    camera, cameras, changed = _apply(quadra_id, UNSET, rtmp_url)
    return url_view(quadra_id, camera, cameras), changed
