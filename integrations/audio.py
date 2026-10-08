"""Mixagem de música de fundo no clipe final, antes do envio.

Escolhe uma faixa aleatória de `music_dir`; sem pasta ou sem arquivos, não faz nada.
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
    """Devolve `clip_path` intacto se não houver faixa; senão gera `<clip>_audio.mp4`
    com a música cortada na duração do clipe (em loop se for curta), com fades e volume fixo."""
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
