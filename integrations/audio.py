"""Mixagem de música de fundo no clipe final, antes do envio.

Escolhe uma faixa aleatória de `music_dir`; sem pasta ou sem arquivos, não faz nada.
O áudio da câmera (som ambiente), quando o clipe tem, segue por baixo da música.
"""
from __future__ import annotations

import random
import subprocess
from pathlib import Path

from clipper.clip_generator import probe_duration_seconds, probe_has_audio

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


def _mix_cmd(clip_path: Path, track: Path, filter_a: str, output_path: Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
        "-i", str(clip_path),
        "-stream_loop", "-1", "-i", str(track),
        "-filter_complex", filter_a,
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac",
        "-shortest",
        str(output_path),
    ]


def apply_audio(
    clip_path: Path,
    music_dir: Path | None,
    volume: float = 0.4,
    fade_seconds: float = 1.5,
    ambient_volume: float = 0.4,
) -> Path:
    """Devolve `clip_path` intacto se não houver faixa; senão gera `<clip>_audio.mp4`
    com a música cortada na duração do clipe (em loop se for curta), com fades e volume fixo.

    Se o clipe tem o áudio da câmera, ele segue por baixo da música, em `ambient_volume`
    (0 desliga). Sem áudio da câmera, ou se a mixagem com ele falhar, sai só a música."""
    track = _pick_track(music_dir)
    if track is None:
        return clip_path

    duration = probe_duration_seconds(clip_path)
    fade_out_start = max(duration - fade_seconds, 0.0)
    output_path = clip_path.with_name(f"{clip_path.stem}_audio.mp4")

    music = (
        f"[1:a]atrim=0:{duration},"
        f"afade=t=in:st=0:d={fade_seconds},"
        f"afade=t=out:st={fade_out_start}:d={fade_seconds},"
        f"volume={volume}"
    )
    music_only = _mix_cmd(clip_path, track, f"{music}[a]", output_path)

    try:
        if ambient_volume > 0 and probe_has_audio(clip_path):
            # a câmera entrega mono 16 kHz; vira estéreo 44,1 kHz como a música antes de mixar.
            # normalize=0 mantém a música no volume configurado (o padrão dividiria por 2).
            with_ambient = _mix_cmd(
                clip_path, track,
                f"{music}[music];"
                f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={ambient_volume}[ambient];"
                "[music][ambient]amix=inputs=2:duration=first:normalize=0[a]",
                output_path,
            )
            try:
                _run(with_ambient)
            except AudioApplicationError as exc:
                reason = str(exc).strip().splitlines()[-1]
                print(f"[audio] mixagem com o áudio da câmera falhou em '{clip_path.name}', "
                      f"seguindo só com a música: {reason}")
                _run(music_only)
        else:
            _run(music_only)
    except AudioApplicationError:
        output_path.unlink(missing_ok=True)
        raise
    return output_path
