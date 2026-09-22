"""
audio.py — mixagem de música de fundo no clipe final, antes do envio ao
Lara. Ao contrário de orientation.py/overlay.py, esta é uma decisão LOCAL
(o contrato do Lara não tem campo de áudio, ver PLANO_DE_ACAO.md/README —
"este sistema nunca decide" vale só pro que o Lara de fato manda).

Fonte da trilha: qualquer arquivo de áudio dentro de `music_dir`. Hoje
normalmente um único arquivo (uso fixo, fase inicial), mas a escolha já é
ALEATÓRIA entre todos os arquivos encontrados — pronta pro sorteio quando
houver mais de uma faixa, sem precisar mexer neste módulo de novo. Pasta
ausente/vazia = sem música (no-op silencioso, não é erro — ninguém
configurou uma faixa ainda).

Como as câmeras não entregam áudio (captura descarta/nunca inclui, ver
capture/capture_camera.sh), o clipe final só ganha a faixa da música — não
há áudio original pra mixar junto.
"""
from __future__ import annotations

import random
import subprocess
from pathlib import Path

from clipper.clip_generator import probe_duration_seconds

_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"}


class AudioApplicationError(RuntimeError):
    pass


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AudioApplicationError(
            f"Comando falhou ({' '.join(cmd)}):\n{result.stderr}"
        )


def _pick_track(music_dir: Path | None) -> Path | None:
    if music_dir is None or not music_dir.is_dir():
        return None
    tracks = sorted(p for p in music_dir.iterdir() if p.suffix.lower() in _EXTENSIONS)
    if not tracks:
        return None
    return random.choice(tracks)


def apply_audio(
    clip_path: Path,
    music_dir: Path | None,
    volume: float = 0.5,
    fade_seconds: float = 1.5,
) -> Path:
    """Se não houver nenhuma faixa disponível em `music_dir`, devolve
    `clip_path` sem tocar nele (sem reencode — caso comum enquanto nenhuma
    música foi configurada ainda). Senão, sorteia uma faixa, corta pra
    exatamente a duração do clipe (com loop se a faixa for mais curta),
    aplica fade in/out e volume fixo, e devolve o novo arquivo
    (`<clip>_audio.mp4`)."""
    track = _pick_track(music_dir)
    if track is None:
        return clip_path

    duration = probe_duration_seconds(clip_path)
    fade_out_start = max(duration - fade_seconds, 0.0)
    output_path = clip_path.with_name(f"{clip_path.stem}_audio.mp4")

    filter_a = (
        f"[1:a]atrim=0:{duration},"
        f"afade=t=in:st=0:d={fade_seconds},"
        f"afade=t=out:st={fade_out_start}:d={fade_seconds},"
        f"volume={volume}[a]"
    )
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
        "-i", str(clip_path),
        "-stream_loop", "-1", "-i", str(track),
        "-filter_complex", filter_a,
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac",
        "-shortest",
        str(output_path),
    ]

    try:
        _run(cmd)
    except AudioApplicationError:
        output_path.unlink(missing_ok=True)
        raise
    return output_path
