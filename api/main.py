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
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from api import config
from clipper.clip_generator import ClipGenerationError, generate_clip

app = FastAPI(title="Replay System — backend central")

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
def trigger_replay(quadra_id: str):
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

    try:
        clip_path = generate_clip(
            quadra_id,
            buffer_root=config.BUFFER_ROOT,
            output_dir=config.OUTPUT_DIR,
            duration_seconds=config.CLIP_DURATION_SECONDS,
            segment_time=config.SEGMENT_TIME,
            safety_margin=config.SAFETY_MARGIN,
            max_staleness_seconds=config.MAX_STALENESS_SECONDS,
        )
    except ClipGenerationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

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
