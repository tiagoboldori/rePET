"""API do backend central.

POST /replay/{quadra_id} é chamado direto pelo firmware do botão físico
(sem corpo; o quadra_id na URL basta).

POST/DELETE /live/{quadra_id} liga/desliga o RTMP que a própria câmera
empurra para o YouTube (via ISAPI); GET /live/{quadra_id} diz se está no ar. Exigem
`Authorization: Bearer <token>` (LIVE_API_KEY ou REPLAY_API_TOKEN; o header legado
X-Live-Key e a sessão do painel também valem; ver api/auth.py).

/broadcast/{quadra_id}/... é a transmissão COM placar (o servidor reencoda e desenha
o overlay): iniciar/encerrar, pontos, sets, nomes e volume. Mesma autorização.

Dev: uvicorn api.main:app --reload --port 8000
"""
import secrets
import threading
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlmodel import Session, select

from api import auth, broadcast, config, live, monitor, panel, stream_config
from clipper.clip_generator import (
    ClipGenerationError,
    generate_clip,
    probe_duration_seconds,
)
from db.engine import create_db_and_tables, engine, get_session
from db.migrate_cameras import sync_cameras_from_file
from db.models import Quadra, Replay
from db.reconcile import reconcile_replays


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    with Session(engine) as session:
        sync_cameras_from_file(session, config.CAMERAS_FILE)
        # Recupera no banco clipes que existem em disco sem registro.
        recuperados = reconcile_replays(session, config.OUTPUT_DIR)
        if recuperados:
            print(f"[api] reconciliação: {recuperados} replay(s) recuperado(s) do disco.")
    live.resume(config.load_cameras())
    cameras = config.load_cameras()
    resumed = broadcast.resume(cameras)
    print(f"[startup] transmissões com placar: {resumed}")
    monitor.start(config.load_cameras)  # verifica as lives diretas em segundo plano
    yield
    monitor.shutdown()
    live.cancel_timers()
    broadcast.detach()  # os ffmpeg das transmissões seguem no ar; resume() os readota


app = FastAPI(title="Replay System — backend central", lifespan=lifespan)

config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/clips", StaticFiles(directory=str(config.OUTPUT_DIR)), name="clips")

# Último acionamento por quadra, para o cooldown (evita dois cortes
# simultâneos na mesma câmera).
_last_trigger: dict[str, datetime] = {}
_trigger_lock = threading.Lock()

# --- HTTP Basic dos endpoints de gerenciamento -----------------------------
_admin_security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(_admin_security)) -> None:
    """Exige HTTP Basic; sem ADMIN_USERNAME/ADMIN_PASSWORD configurados,
    recusa tudo."""
    configured = bool(config.ADMIN_USERNAME and config.ADMIN_PASSWORD)
    user_ok = configured and secrets.compare_digest(credentials.username, config.ADMIN_USERNAME)
    pass_ok = configured and secrets.compare_digest(credentials.password, config.ADMIN_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Credencial inválida." if configured else (
                "Autenticação de administrador não configurada "
                "(ADMIN_USERNAME/ADMIN_PASSWORD)."
            ),
            headers={"WWW-Authenticate": "Basic"},
        )


_bearer_scheme = HTTPBearer(auto_error=False, description="LIVE_API_KEY ou REPLAY_API_TOKEN")


def require_live_access(request: Request, _bearer: object = Depends(_bearer_scheme)) -> None:
    """Bearer (LIVE_API_KEY ou REPLAY_API_TOKEN), X-Live-Key (legado) OU sessão do painel.
    A regra completa está em api/auth.py; o HTTPBearer só registra o esquema no /docs
    (auto_error=False: quem decide e responde é auth.check_access)."""
    auth.check_access(request)


def _live_camera(quadra_id: str) -> dict:
    camera = config.load_cameras().get(quadra_id)
    if camera is None:
        raise HTTPException(
            status_code=404,
            detail=f"quadra_id '{quadra_id}' não está em {config.CAMERAS_FILE}.",
        )
    return camera


@app.get("/live/{quadra_id}", dependencies=[Depends(require_live_access)])
def live_status(quadra_id: str):
    """Diz se a câmera está empurrando a live (lê o estado direto da câmera)."""
    camera = _live_camera(quadra_id)
    try:
        return {"quadra_id": quadra_id, "no_ar": live.status(camera["input_url"])}
    except live.LiveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/live/{quadra_id}", dependencies=[Depends(require_live_access)])
def start_live(quadra_id: str):
    """Liga o RTMP da câmera para o YouTube (idempotente)."""
    camera = _live_camera(quadra_id)
    stream_key = camera.get("youtube_stream_key")
    if not stream_key:
        raise HTTPException(
            status_code=422,
            detail=f"'{quadra_id}' não tem youtube_stream_key em {config.CAMERAS_FILE}.",
        )
    if broadcast.is_on_air(quadra_id):
        raise HTTPException(
            status_code=409,
            detail=f"'{quadra_id}' está em transmissão com placar; encerre-a antes (POST /broadcast/{quadra_id}/stop).",
        )
    other = monitor.key_in_use_by(quadra_id, config.load_cameras(), fresh=True)
    if other:
        raise HTTPException(
            status_code=409,
            detail=f"A stream key de '{quadra_id}' é a mesma de '{other}', que já está no ar; encerre '{other}' antes.",
        )
    try:
        started = live.start(quadra_id, camera["input_url"], stream_key, camera.get("rtmp_url"))
    except live.LiveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    monitor.mark(quadra_id, True)
    return {
        "quadra_id": quadra_id,
        "status": "iniciada" if started else "ja_no_ar",
        "max_seconds": config.LIVE_MAX_SECONDS or None,
    }


@app.post("/live/{quadra_id}/stop", dependencies=[Depends(require_live_access)])
@app.delete("/live/{quadra_id}", dependencies=[Depends(require_live_access)])
def stop_live(quadra_id: str):
    """Desliga o RTMP da câmera."""
    camera = _live_camera(quadra_id)
    try:
        stopped = live.stop(quadra_id, camera["input_url"])
    except live.LiveError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    monitor.mark(quadra_id, False)
    return {"quadra_id": quadra_id, "status": "encerrada" if stopped else "nao_estava_no_ar"}


# --- Transmissão com placar --------------------------------------------------

class BroadcastStart(BaseModel):
    nome_a: str | None = None
    nome_b: str | None = None
    reset: bool = True  # jogo novo: placar 0x0 e volume 100% (o relançamento interno nunca zera)
    volume: int | None = None  # % do volume da câmera; vazio = o de `reset` (ou o último, se reset=false)


class BroadcastNames(BaseModel):
    nome_a: str | None = None
    nome_b: str | None = None


class BroadcastScore(BaseModel):
    pontos_a: int | None = None
    pontos_b: int | None = None
    sets_a: int | None = None
    sets_b: int | None = None


class BroadcastVolume(BaseModel):
    nivel: int  # % do volume da câmera (100 = original)


def _bc(quadra_id: str, call, *args):
    """Chama o módulo broadcast para uma quadra existente, traduzindo os erros."""
    _live_camera(quadra_id)
    try:
        return call(quadra_id, *args)
    except broadcast.BroadcastError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/broadcast", dependencies=[Depends(require_live_access)])
def broadcast_list():
    """Todas as transmissões com placar (e o limite simultâneo)."""
    return broadcast.list_all()


@app.get("/broadcast/{quadra_id}", dependencies=[Depends(require_live_access)])
def broadcast_status(quadra_id: str):
    return _bc(quadra_id, broadcast.snapshot)


@app.post("/broadcast/{quadra_id}/start", dependencies=[Depends(require_live_access)])
def broadcast_start(quadra_id: str, body: BroadcastStart | None = None):
    """Liga a transmissão com placar (idempotente). Corpo opcional: nomes e reset."""
    camera = _live_camera(quadra_id)
    body = body or BroadcastStart()
    if not broadcast.is_on_air(quadra_id):
        try:
            direct = live.status(camera["input_url"])
        except live.LiveError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if direct:
            raise HTTPException(
                status_code=409,
                detail=f"'{quadra_id}' está em live direta; encerre-a antes (POST /live/{quadra_id}/stop).",
            )
        other = monitor.key_in_use_by(quadra_id, config.load_cameras(), fresh=True)
        if other:
            raise HTTPException(
                status_code=409,
                detail=f"A stream key de '{quadra_id}' é a mesma de '{other}', que já está no ar; encerre '{other}' antes.",
            )
    try:
        started = broadcast.start(
            quadra_id, camera, {"a": body.nome_a, "b": body.nome_b}, body.reset, body.volume
        )
    except broadcast.BroadcastError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"status": "iniciada" if started else "ja_no_ar", **broadcast.snapshot(quadra_id)}


@app.post("/broadcast/{quadra_id}/stop", dependencies=[Depends(require_live_access)])
@app.delete("/broadcast/{quadra_id}", dependencies=[Depends(require_live_access)])
def broadcast_stop(quadra_id: str):
    stopped = _bc(quadra_id, broadcast.stop)
    return {"status": "encerrada" if stopped else "nao_estava_no_ar", **broadcast.snapshot(quadra_id)}


@app.post("/broadcast/{quadra_id}/ponto/{lado}", dependencies=[Depends(require_live_access)])
def broadcast_point(quadra_id: str, lado: str, delta: int = 1):
    """+1 ponto no lado 'a' ou 'b' (delta=-1 corrige um erro)."""
    return _bc(quadra_id, broadcast.add_point, lado, delta)


@app.post("/broadcast/{quadra_id}/set/{lado}", dependencies=[Depends(require_live_access)])
def broadcast_set(quadra_id: str, lado: str, delta: int = 1):
    """+1 set no lado e zera os pontos dos dois (delta=-1 só tira um set)."""
    return _bc(quadra_id, broadcast.add_set, lado, delta)


@app.put("/broadcast/{quadra_id}/placar", dependencies=[Depends(require_live_access)])
def broadcast_score(quadra_id: str, body: BroadcastScore):
    """Correção manual: grava exatamente os valores enviados."""
    return _bc(quadra_id, broadcast.set_score, body.model_dump())


@app.post("/broadcast/{quadra_id}/placar/zerar", dependencies=[Depends(require_live_access)])
def broadcast_reset_score(quadra_id: str):
    return _bc(quadra_id, broadcast.reset_score)


@app.put("/broadcast/{quadra_id}/nomes", dependencies=[Depends(require_live_access)])
def broadcast_names(quadra_id: str, body: BroadcastNames):
    return _bc(quadra_id, broadcast.set_names, body.nome_a, body.nome_b)


@app.post("/broadcast/{quadra_id}/volume/{direcao}", dependencies=[Depends(require_live_access)])
def broadcast_volume_step(quadra_id: str, direcao: str, passo: int | None = None):
    """direcao = 'up' ou 'down'; passo em % (padrão BROADCAST_VOLUME_STEP)."""
    if direcao not in ("up", "down"):
        raise HTTPException(status_code=422, detail="direcao deve ser 'up' ou 'down'.")
    return _bc(quadra_id, broadcast.change_volume, 1 if direcao == "up" else -1, passo)


@app.put("/broadcast/{quadra_id}/volume", dependencies=[Depends(require_live_access)])
def broadcast_volume(quadra_id: str, body: BroadcastVolume):
    return _bc(quadra_id, broadcast.set_volume, body.nivel)


# --- Estado geral (usado pelo painel) -----------------------------------------

@app.get("/status", dependencies=[Depends(require_live_access)])
def live_overview():
    """Uma linha por quadra com os dois modos (live direta e transmissão com placar).

    Não consulta câmera nenhuma na hora: a live direta vem do cache da verificação periódica.
    `modo`: placar | direta | conflito (os dois ligados) | nenhum | desconhecido (câmera
    não respondeu e não há transmissão com placar).
    """
    quadras = []
    cameras = config.load_cameras()
    for quadra_id, camera in cameras.items():
        direta = monitor.get(quadra_id)
        placar = broadcast.snapshot(quadra_id)
        if placar["no_ar"] and direta["no_ar"]:
            modo = "conflito"
        elif placar["no_ar"]:
            modo = "placar"
        elif direta["no_ar"]:
            modo = "direta"
        elif direta["no_ar"] is None and str(camera.get("input_url", "")).startswith("rtsp://"):
            modo = "desconhecido"
        else:
            modo = "nenhum"
        quadras.append({
            "quadra_id": quadra_id,
            "nome": camera.get("nome", quadra_id),
            "esporte": camera.get("esporte"),
            "tem_stream_key": bool(camera.get("youtube_stream_key")),
            # Quadras com a MESMA stream key (intencional) e, destas, a que está no ar agora:
            # enquanto uma estiver no ar, as outras não podem ligar (409).
            "chave_compartilhada_com": monitor.shared_key_with(quadra_id, cameras),
            "chave_em_uso_por": monitor.key_in_use_by(quadra_id, cameras),
            "modo": modo,
            "direta": direta,
            "placar": placar,
        })
    on_air = sum(1 for q in quadras if q["placar"]["no_ar"])
    return {
        "quadras": quadras,
        "transmissoes_com_placar": {"no_ar": on_air, "limite": config.BROADCAST_MAX_CONCURRENT},
        "verificacao_na_subida": monitor.last_startup_summary,
    }


# --- Destino da transmissão: stream key e URL RTMP por quadra -----------------

class StreamConfigUpdate(BaseModel):
    stream_key: str | None = None  # nova stream key (não pode ser null)
    rtmp_url: str | None = None  # nova URL rtmp://...; null volta à URL global


def _stream_config_call(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except stream_config.StreamConfigError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@app.get("/quadras/{quadra_id}/youtube", dependencies=[Depends(require_live_access)])
def youtube_config_get(quadra_id: str):
    """Destino da transmissão da quadra. A stream key NUNCA é devolvida (só os 4 últimos caracteres)."""
    return _stream_config_call(stream_config.get, quadra_id)


@app.put("/quadras/{quadra_id}/youtube", dependencies=[Depends(require_live_access)])
def youtube_config_put(quadra_id: str, body: StreamConfigUpdate, request: Request):
    """Troca a stream key e/ou a URL RTMP da quadra (o que vier no corpo; o resto não muda)."""
    fields = body.model_fields_set
    view, changed = _stream_config_call(
        stream_config.update,
        quadra_id,
        body.stream_key if "stream_key" in fields else stream_config.UNSET,
        body.rtmp_url if "rtmp_url" in fields else stream_config.UNSET,
    )
    if changed:  # auditoria: quem e o quê, nunca a key
        who = getattr(request.state, "token_owner", "sessão do painel ou X-Live-Key")
        print(f"[youtube] {quadra_id}: destino da transmissão alterado ({', '.join(sorted(fields))}) por {who}.")
    return {"status": "atualizada" if changed else "sem_mudanca", **view}


class StreamKeyUpdate(BaseModel):
    stream_key: str | None = None  # obrigatório; null é recusado (a API troca a key, não a remove)


class RtmpUrlUpdate(BaseModel):
    rtmp_url: str | None = None  # obrigatório; null apaga a URL da quadra e volta à global


def _audit(request: Request, quadra_id: str, what: str) -> None:
    who = getattr(request.state, "token_owner", "sessão do painel ou X-Live-Key")
    print(f"[youtube] {quadra_id}: {what} por {who}.")  # nunca a key


@app.get("/quadras/{quadra_id}/youtube/key", dependencies=[Depends(require_live_access)])
def youtube_key_get(quadra_id: str, request: Request):
    """A stream key da quadra, INTEIRA (a rede é fechada e a rota exige token). Cada leitura é registrada no log."""
    view = _stream_config_call(stream_config.get_key, quadra_id)
    _audit(request, quadra_id, "stream key lida")
    return view


@app.put("/quadras/{quadra_id}/youtube/key", dependencies=[Depends(require_live_access)])
def youtube_key_put(quadra_id: str, body: StreamKeyUpdate, request: Request):
    """Troca a stream key da quadra (não vale com ela transmitindo)."""
    if "stream_key" not in body.model_fields_set:
        raise HTTPException(status_code=422, detail="informe stream_key.")
    view, changed = _stream_config_call(stream_config.update_key, quadra_id, body.stream_key)
    if changed:
        _audit(request, quadra_id, "stream key alterada")
    return {"status": "atualizada" if changed else "sem_mudanca", **view}


@app.get("/quadras/{quadra_id}/youtube/url", dependencies=[Depends(require_live_access)])
def youtube_url_get(quadra_id: str):
    """A URL RTMP efetiva da quadra (a própria, ou a global do servidor)."""
    return _stream_config_call(stream_config.get_url, quadra_id)


@app.put("/quadras/{quadra_id}/youtube/url", dependencies=[Depends(require_live_access)])
def youtube_url_put(quadra_id: str, body: RtmpUrlUpdate, request: Request):
    """Troca a URL RTMP da quadra; `null` volta à global (não vale com ela transmitindo)."""
    if "rtmp_url" not in body.model_fields_set:
        raise HTTPException(status_code=422, detail="informe rtmp_url (use null para voltar à URL global).")
    view, changed = _stream_config_call(stream_config.update_url, quadra_id, body.rtmp_url)
    if changed:
        _audit(request, quadra_id, "URL RTMP alterada")
    return {"status": "atualizada" if changed else "sem_mudanca", **view}


# --- Painel web ---------------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/painel/static", StaticFiles(directory=str(_STATIC_DIR)), name="painel_static")
_PANEL_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
}


class PanelLogin(BaseModel):
    usuario: str
    senha: str


@app.get("/painel", include_in_schema=False)
def painel_page():
    return FileResponse(_STATIC_DIR / "painel.html", headers=_PANEL_HEADERS)


@app.post("/painel/login", include_in_schema=False)
def painel_login(body: PanelLogin, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    if not panel.configured():
        raise HTTPException(status_code=503, detail="Defina ADMIN_USERNAME e ADMIN_PASSWORD no servidor.")
    if panel.login_blocked(ip):
        raise HTTPException(status_code=429, detail="Muitas tentativas; aguarde um minuto.")
    if not panel.check_credentials(body.usuario, body.senha):
        panel.register_failure(ip)
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos.")
    panel.clear_failures(ip)
    response.set_cookie(
        panel.COOKIE, panel.make_token(body.usuario), max_age=int(config.PANEL_SESSION_HOURS * 3600),
        httponly=True, samesite="strict", path="/",
    )
    return {"usuario": body.usuario}


@app.post("/painel/logout", include_in_schema=False)
def painel_logout(response: Response):
    response.delete_cookie(panel.COOKIE, path="/")
    return {"status": "saiu"}


@app.get("/painel/me", include_in_schema=False)
def painel_me(request: Request):
    user = panel.verify_token(request.cookies.get(panel.COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="Sem sessão.")
    return {"usuario": user}


def _serialize_replay(replay: Replay) -> dict:
    return {
        "id": replay.id,
        "quadra_id": replay.quadra_id,
        "criado_em": replay.criado_em,
        "duracao_segundos": replay.duracao_segundos,
        "tamanho_bytes": replay.tamanho_bytes,
        "media_url": f"/api/replays/{replay.id}/media",
        "lara_status": replay.lara_status,
        "lara_enviado_em": replay.lara_enviado_em,
    }


@app.post("/replay/{quadra_id}")
def trigger_replay(quadra_id: str, session: Session = Depends(get_session)):
    """Corta o clipe dos últimos N segundos da quadra, até o instante da chamada."""
    now = datetime.now()  # instante do botão: o clipe termina aqui
    cameras = config.load_cameras()
    if quadra_id not in cameras:
        raise HTTPException(
            status_code=404,
            detail=(
                f"quadra_id '{quadra_id}' não está em {config.CAMERAS_FILE}. "
                "Confira config/cameras.json."
            ),
        )

    with _trigger_lock:
        last = _last_trigger.get(quadra_id)
        if last is not None:
            elapsed = (now - last).total_seconds()
            if elapsed < config.TRIGGER_COOLDOWN_SECONDS:
                wait = config.TRIGGER_COOLDOWN_SECONDS - elapsed
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Aguarde mais {wait:.1f}s antes de acionar "
                        f"'{quadra_id}' de novo (intervalo mínimo: "
                        f"{config.TRIGGER_COOLDOWN_SECONDS:.0f}s)."
                    ),
                )
        _last_trigger[quadra_id] = now

    # Duração vinda do cache local (Quadra.clip_seconds); sem sincronização
    # ainda, usa o default global.
    quadra = session.get(Quadra, quadra_id)
    clip_seconds = (
        quadra.clip_seconds
        if quadra is not None and quadra.clip_seconds is not None
        else config.CLIP_DURATION_SECONDS
    )

    try:
        clip_path = generate_clip(
            quadra_id,
            buffer_root=config.BUFFER_ROOT,
            output_dir=config.OUTPUT_DIR,
            duration_seconds=clip_seconds,
            segment_time=config.SEGMENT_TIME,
            safety_margin=config.SAFETY_MARGIN,
            max_staleness_seconds=config.MAX_STALENESS_SECONDS,
            trigger_time=now,
            max_wait_seconds=config.CLIP_WAIT_TIMEOUT_SECONDS,
        )
    except ClipGenerationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Registro no banco é best-effort: o clipe já está em disco, e uma falha
    # aqui não deve impedir a resposta. O reconcile recupera no próximo
    # start. O envio fica por conta do worker (scripts/lara_worker.py).
    try:
        session.add(
            Replay(
                id=clip_path.stem,
                quadra_id=quadra_id,
                arquivo_bruto=clip_path.name,
                duracao_segundos=probe_duration_seconds(clip_path),
                tamanho_bytes=clip_path.stat().st_size,
            )
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"[api] aviso: falha ao registrar replay '{clip_path.stem}' no banco: {exc}")

    return {
        "quadra_id": quadra_id,
        "clip_filename": clip_path.name,
        "clip_url": f"/clips/{clip_path.name}",
    }


@app.get("/api/replays/{replay_id}")
def get_replay(replay_id: str, session: Session = Depends(get_session)):
    """Metadados de um replay pelo id."""
    replay = session.get(Replay, replay_id)
    if replay is None:
        raise HTTPException(status_code=404, detail=f"replay '{replay_id}' não encontrado.")

    return _serialize_replay(replay)


@app.get("/api/replays/{replay_id}/media")
def get_replay_media(replay_id: str, session: Session = Depends(get_session)):
    """Serve o vídeo do replay: o processado se existir, senão o bruto.

    O FileResponse já trata Range e ETag. Usa `no-cache` porque o arquivo
    de um mesmo replay_id passa de bruto para processado depois da
    criação, e um max-age deixaria o navegador com o bruto em cache.
    """
    replay = session.get(Replay, replay_id)
    if replay is None:
        raise HTTPException(status_code=404, detail=f"replay '{replay_id}' não encontrado.")

    filename = replay.arquivo_processado or replay.arquivo_bruto
    path = config.OUTPUT_DIR / filename
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"arquivo do replay '{replay_id}' não está mais em disco (retenção local já removeu?).",
        )

    return FileResponse(
        path,
        media_type="video/mp4",
        filename=filename,
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/quadras/{quadra_id}/replays")
def list_quadra_replays(
    quadra_id: str,
    page: int = 1,
    page_size: int = 20,
    session: Session = Depends(get_session),
):
    """Replays de uma quadra, paginados, do mais recente ao mais antigo."""
    if session.get(Quadra, quadra_id) is None:
        raise HTTPException(status_code=404, detail=f"quadra '{quadra_id}' não encontrada.")

    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)  # máximo por página

    total = session.exec(
        select(func.count()).select_from(Replay).where(Replay.quadra_id == quadra_id)
    ).one()

    items = session.exec(
        select(Replay)
        .where(Replay.quadra_id == quadra_id)
        .order_by(Replay.criado_em.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return {
        "quadra_id": quadra_id,
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [_serialize_replay(r) for r in items],
    }


@app.delete("/api/replays/{replay_id}", status_code=204)
def delete_replay(
    replay_id: str,
    session: Session = Depends(get_session),
    _admin: None = Depends(require_admin),
):
    """Remove o registro e os arquivos (bruto e processado) de um replay."""
    replay = session.get(Replay, replay_id)
    if replay is None:
        raise HTTPException(status_code=404, detail=f"replay '{replay_id}' não encontrado.")

    for filename in {replay.arquivo_bruto, replay.arquivo_processado}:
        if filename:
            (config.OUTPUT_DIR / filename).unlink(missing_ok=True)

    session.delete(replay)
    session.commit()
    return None


@app.get("/quadra/{quadra_id}", response_class=HTMLResponse)
def list_replays(quadra_id: str, session: Session = Depends(get_session)):
    """Página pública com os replays da quadra, mais recente primeiro.

    Lê da tabela `Replay` e não de glob em OUTPUT_DIR, que listaria o bruto
    e o processado como itens separados.
    """
    cameras = config.load_cameras()
    if quadra_id not in cameras:
        raise HTTPException(
            status_code=404,
            detail=(
                f"quadra_id '{quadra_id}' não está em {config.CAMERAS_FILE}. "
                "Confira config/cameras.json."
            ),
        )

    replays = session.exec(
        select(Replay).where(Replay.quadra_id == quadra_id).order_by(Replay.criado_em.desc())
    ).all()
    items = "".join(
        f"<li><video controls preload='metadata' src='/api/replays/{r.id}/media'></video>"
        f"<p>{r.id}</p></li>"
        for r in replays
    ) or "<li>Nenhum replay ainda.</li>"

    nome = cameras[quadra_id]["nome"]
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Replays — {nome}</title></head>"
        f"<body><h1>Replays — {nome}</h1><ul>{items}</ul></body></html>"
    )


@app.get("/health")
def health():
    return {"status": "ok"}
