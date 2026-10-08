"""API do backend central.

POST /replay/{quadra_id} é chamado direto pelo firmware do botão físico
(sem corpo; o quadra_id na URL basta).

Dev: uvicorn api.main:app --reload --port 8000
"""
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlmodel import Session, select

from api import config
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
    yield


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
    cameras = config.load_cameras()
    if quadra_id not in cameras:
        raise HTTPException(
            status_code=404,
            detail=(
                f"quadra_id '{quadra_id}' não está em {config.CAMERAS_FILE}. "
                "Confira config/cameras.json."
            ),
        )

    now = datetime.now()
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
