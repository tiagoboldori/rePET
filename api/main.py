"""
main.py — API do backend central.

Endpoint principal desta fase:
    POST /replay/{quadra_id}
Este é o endpoint que o firmware ESPHome do botão físico chama direto
(via `http_request.post`) quando o botão daquela quadra é pressionado —
sem Home Assistant no meio. Não recebe corpo — o `quadra_id` na URL já é
toda a informação necessária.

Rodar localmente (dev):
    uvicorn api.main:app --reload --port 8000
"""
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

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
        # PT-03 (M4): recupera no banco qualquer clipe que já exista em
        # disco mas não tenha registro (ex.: banco perdido/recriado, ou
        # clipes anteriores à introdução do banco). Nunca sobrescreve
        # registro existente — só preenche ausência (RNF6: disco é a
        # fonte de verdade final).
        recuperados = reconcile_replays(session, config.OUTPUT_DIR)
        if recuperados:
            print(f"[api] reconciliação: {recuperados} replay(s) recuperado(s) do disco.")
    yield


app = FastAPI(title="Replay System — backend central", lifespan=lifespan)

# Clipes finais (disco persistente) ficam acessíveis publicamente em
# /clips/<arquivo>.mp4 — é o que a página pública de cada quadra vai listar.
config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/clips", StaticFiles(directory=str(config.OUTPUT_DIR)), name="clips")

# Intervalo mínimo entre acionamentos da MESMA quadra — protege contra
# clique duplo/repique do botão físico e contra dois cortes (caros, ~5-8s
# de ffmpeg) rodando ao mesmo tempo pra mesma câmera.
_last_trigger: dict[str, datetime] = {}
_trigger_lock = threading.Lock()


@app.post("/replay/{quadra_id}")
def trigger_replay(quadra_id: str, session: Session = Depends(get_session)):
    """Aciona o corte do clipe dos últimos N segundos para `quadra_id`.

    Chamado pelo firmware do botão físico daquela quadra. O momento do
    corte é "agora", no instante em que esta chamada chega.
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

    # Duração do clipe vem do cache local sincronizado com o Lara
    # (Quadra.clip_seconds, PT-14) quando já houver uma sincronização
    # feita; nunca uma consulta ao vivo (RNF3/RNF8 do PLANO_DE_ACAO.md v3).
    # Sem sincronização ainda (quadra recém-cadastrada), cai no default
    # global — mesmo comportamento de antes desta integração.
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

    # Registro no banco (M3) é best-effort: o clipe já está em disco e
    # servível nesse ponto — disco é a fonte de verdade final (RNF6), uma
    # falha aqui não pode impedir a entrega do replay que já foi gerado
    # com sucesso (mesmo princípio de RNF9 pra falha de envio ao Lara). O
    # registro nasce com lara_status=PENDENTE (default do modelo) — quem
    # envia ao Lara é o worker de fundo (scripts/lara_worker.py), nunca
    # este caminho de requisição. Se esse registro se perder de qualquer
    # forma, db.reconcile.reconcile_replays (PT-03) recupera no próximo
    # start da API, lendo o arquivo já em disco.
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
    except Exception as exc:  # noqa: BLE001 — best-effort, ver comentário acima
        print(f"[api] aviso: falha ao registrar replay '{clip_path.stem}' no banco: {exc}")

    return {
        "quadra_id": quadra_id,
        "clip_filename": clip_path.name,
        "clip_url": f"/clips/{clip_path.name}",
    }


@app.get("/quadra/{quadra_id}", response_class=HTMLResponse)
def list_replays(quadra_id: str):
    """Página pública (sem login) listando os replays já cortados dessa
    quadra, mais recente primeiro. Lê direto do disco — não depende de
    Postgres (ainda não existe)."""
    cameras = config.load_cameras()
    if quadra_id not in cameras:
        raise HTTPException(
            status_code=404,
            detail=(
                f"quadra_id '{quadra_id}' não está em {config.CAMERAS_FILE}. "
                "Confira config/cameras.json."
            ),
        )

    clips = sorted(
        config.OUTPUT_DIR.glob(f"{quadra_id}_*.mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    items = "".join(
        f"<li><video controls preload='metadata' src='/clips/{p.name}'></video>"
        f"<p>{p.name}</p></li>"
        for p in clips
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
