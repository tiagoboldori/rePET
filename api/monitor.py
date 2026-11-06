"""Estado das lives diretas (a câmera empurra o RTMP) e verificação na subida.

A live direta mora DENTRO da câmera: a API só sabe se ela está ligada perguntando por
ISAPI, e uma câmera lenta leva até 10 s para responder. Por isso uma thread pergunta a
todas as câmeras de tempos em tempos e guarda o resultado; o painel e o `GET /status`
leem esse cache e nunca esperam uma câmera.

A PRIMEIRA rodada, na subida da API, também reconcilia o que já estava no ar:
- live direta ligada sem prazo registrado (ligada na interface da câmera, ou arquivo de
  prazos perdido) ganha o prazo de LIVE_MAX_SECONDS, senão nunca expiraria;
- prazo gravado de uma live que já não está no ar é esquecido;
- quadra com transmissão com placar E live direta ao mesmo tempo é avisada (as duas
  empurrariam para a mesma stream key); nada é desligado por conta própria.
Obs.: o que se vê aqui é o lado do servidor/câmera. O YouTube em si só poderia ser
consultado pela API do YouTube (OAuth), que não está configurada.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from api import broadcast, config, live

_lock = threading.Lock()
_cache: dict[str, dict] = {}
_stop = threading.Event()
_thread: threading.Thread | None = None
last_startup_summary: dict = {}


def _check(quadra_id: str, camera: dict) -> bool | None:
    try:
        on, error = live.status(camera["input_url"]), None
    except live.LiveError as exc:
        on, error = None, str(exc)
    with _lock:
        _cache[quadra_id] = {"no_ar": on, "erro": error, "em": time.time()}
    return on


def get(quadra_id: str) -> dict:
    """Último estado conhecido da live direta: {no_ar: bool|None, erro, verificado_ha_segundos}."""
    with _lock:
        entry = _cache.get(quadra_id)
    if entry is None:
        return {"no_ar": None, "erro": "ainda não verificado", "verificado_ha_segundos": None}
    return {"no_ar": entry["no_ar"], "erro": entry["erro"],
            "verificado_ha_segundos": round(time.time() - entry["em"])}


def mark(quadra_id: str, no_ar: bool) -> None:
    """Atualiza o cache na hora (depois de ligar/desligar), sem esperar a próxima rodada."""
    with _lock:
        _cache[quadra_id] = {"no_ar": no_ar, "erro": None, "em": time.time()}


def run_once(cameras: dict[str, dict], first: bool = False) -> dict:
    """Pergunta a todas as câmeras RTSP, em paralelo. Na 1ª rodada, reconcilia."""
    targets = {q: c for q, c in cameras.items() if str(c.get("input_url", "")).startswith("rtsp://")}
    summary = {"direta": [], "placar": [], "adotadas": [], "prazos_obsoletos": [], "conflitos": [], "sem_resposta": []}
    if targets:
        with ThreadPoolExecutor(max_workers=min(len(targets), 4)) as pool:
            results = dict(zip(targets, pool.map(lambda item: _check(*item), targets.items())))
    else:
        results = {}
    for quadra_id, camera in targets.items():
        on = results[quadra_id]
        placar = broadcast.is_on_air(quadra_id)
        if placar:
            summary["placar"].append(quadra_id)
        if on is None:
            summary["sem_resposta"].append(quadra_id)
            continue
        if on:
            summary["direta"].append(quadra_id)
            if placar:
                summary["conflitos"].append(quadra_id)
            if first and live.adopt(quadra_id, camera["input_url"]):
                summary["adotadas"].append(quadra_id)
        elif first and live.drop_stale(quadra_id):
            summary["prazos_obsoletos"].append(quadra_id)
    return summary


def _log_startup(summary: dict) -> None:
    print(
        "[startup] lives: "
        f"direta={summary['direta']} placar={summary['placar']} "
        f"prazo_dado_a_direta_orfa={summary['adotadas']} prazos_obsoletos={summary['prazos_obsoletos']} "
        f"sem_resposta_da_camera={summary['sem_resposta']}"
    )
    for quadra_id in summary["conflitos"]:
        print(
            f"[startup] ATENÇÃO {quadra_id}: transmissão com placar E live direta no ar ao mesmo "
            "tempo (mesma stream key); encerre uma das duas."
        )


def _loop(load_cameras) -> None:
    global last_startup_summary
    first = True
    while not _stop.is_set():
        try:
            summary = run_once(load_cameras(), first)
            if first:
                last_startup_summary = summary
                _log_startup(summary)
        except Exception as exc:  # a verificação nunca pode derrubar a API
            print(f"[monitor] falha na verificação: {type(exc).__name__}: {exc}")
        first = False
        _stop.wait(config.LIVE_MONITOR_INTERVAL_SECONDS)


def start(load_cameras) -> None:
    """Sobe a thread (já faz a verificação de subida em segundo plano: a API não espera)."""
    global _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(load_cameras,), daemon=True, name="live-monitor")
    _thread.start()


def shutdown() -> None:
    _stop.set()
