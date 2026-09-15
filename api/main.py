"""
main.py — API do backend central.

Endpoint principal desta fase:
    POST /replay/{quadra_id}
Este é o endpoint que a automação do Home Assistant chama (via
`rest_command`) quando o botão físico de uma quadra é pressionado — ver
"Papel do Home Assistant" no contexto do projeto. O ESP fala com o HA via
webhook genérico; o HA é quem sabe transformar isso numa chamada
específica pra este endpoint, com o quadra_id certo na URL.

Rodar localmente (dev):
    uvicorn api.main:app --reload --port 8000
"""
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from api import config
from clipper.clip_generator import ClipGenerationError, generate_clip

app = FastAPI(title="Replay System — backend central")

# Clipes finais (disco persistente) ficam acessíveis publicamente em
# /clips/<arquivo>.mp4 — é o que a página pública de cada quadra vai listar.
config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/clips", StaticFiles(directory=str(config.OUTPUT_DIR)), name="clips")


@app.post("/replay/{quadra_id}")
def trigger_replay(quadra_id: str):
    """Aciona o corte do clipe dos últimos N segundos para `quadra_id`.

    Chamado pela automação do Home Assistant quando o botão físico daquela
    quadra é pressionado. Não recebe corpo — o quadra_id na URL já é toda
    a informação necessária (o momento do corte é "agora", no instante em
    que esta chamada chega).
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

    try:
        clip_path = generate_clip(
            quadra_id,
            buffer_root=config.BUFFER_ROOT,
            output_dir=config.OUTPUT_DIR,
            duration_seconds=config.CLIP_DURATION_SECONDS,
            segment_time=config.SEGMENT_TIME,
            safety_margin=config.SAFETY_MARGIN,
        )
    except ClipGenerationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "quadra_id": quadra_id,
        "clip_filename": clip_path.name,
        "clip_url": f"/clips/{clip_path.name}",
    }


@app.get("/health")
def health():
    return {"status": "ok"}
