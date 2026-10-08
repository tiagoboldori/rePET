#!/usr/bin/env python3
"""Corta o clipe final (últimos N segundos) a partir do buffer de segmentos
gravado por capture_camera.sh. Também pode ser usado via CLI.

O segmento mais recente pode ainda estar sendo escrito pelo ffmpeg. Como o
`-f segment` escreve um arquivo por vez, só o último é tratado como aberto;
`safety_margin` é só uma folga extra para diferença de relógio.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

SEGMENT_RE = re.compile(r"^seg_(\d{14})\.mp4$")  # seg_YYYYMMDDHHMMSS.mp4


class ClipGenerationError(RuntimeError):
    pass


@dataclass
class Segment:
    path: Path
    start_time: datetime


def _parse_segment(path: Path) -> Segment | None:
    m = SEGMENT_RE.match(path.name)
    if not m:
        return None
    start_time = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    return Segment(path=path, start_time=start_time)


def list_closed_segments(
    buffer_dir: Path,
    safety_margin: float,
    now: datetime | None = None,
) -> list[Segment]:
    """Segmentos já fechados, em ordem cronológica."""
    now = now or datetime.now()

    all_segments = []
    for p in buffer_dir.glob("seg_*.mp4"):
        seg = _parse_segment(p)
        if seg is not None:
            all_segments.append(seg)
    all_segments.sort(key=lambda s: s.start_time)

    if not all_segments:
        return []

    # Só o mais recente pode ainda estar em escrita.
    closed = all_segments[:-1]

    # Folga pequena contra relógio adiantado ou escrita lenta em disco.
    cutoff = now - timedelta(seconds=safety_margin)
    return [s for s in closed if s.start_time <= cutoff]


def select_segments_for_duration(
    segments: list[Segment], duration_seconds: float, segment_time: int
) -> list[Segment]:
    """Pega os segmentos mais recentes que cobrem `duration_seconds`, com folga."""
    if not segments:
        raise ClipGenerationError(
            "Nenhum segmento fechado disponível no buffer ainda "
            "(câmera muito recente ou buffer vazio)."
        )

    needed = int(duration_seconds // segment_time) + 2  # +2 de folga
    selected = segments[-needed:] if len(segments) > needed else segments

    covered = len(selected) * segment_time
    if covered < duration_seconds:
        # Câmera recém-ligada: usa o que tem.
        print(
            f"[clip_generator] aviso: buffer só cobre ~{covered}s "
            f"(< {duration_seconds}s pedidos) — gerando clipe mais curto.",
            file=sys.stderr,
        )
    return selected


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ClipGenerationError(
            f"Comando falhou ({' '.join(cmd)}):\n{result.stderr}"
        )


def probe_duration_seconds(path: Path) -> float:
    """Duração real do clipe, via ffprobe (pode passar um pouco do pedido, por causa do keyframe)."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ClipGenerationError(f"ffprobe falhou pra {path}:\n{result.stderr}")
    return float(result.stdout.strip())


def probe_resolution(path: Path) -> tuple[int, int]:
    """Resolução (largura, altura) de um vídeo, via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ClipGenerationError(f"ffprobe falhou pra {path}:\n{result.stderr}")
    width_str, height_str = result.stdout.strip().split("x")
    return int(width_str), int(height_str)


def generate_clip(
    quadra_id: str,
    buffer_root: Path,
    output_dir: Path,
    duration_seconds: float = 35,
    segment_time: int = 2,
    safety_margin: float = 0.5,
    max_staleness_seconds: float | None = None,
) -> Path:
    """Gera o clipe dos últimos `duration_seconds` da quadra em `output_dir` e devolve o caminho."""
    buffer_dir = Path(buffer_root) / quadra_id
    if not buffer_dir.is_dir():
        raise ClipGenerationError(f"Diretório de buffer não existe: {buffer_dir}")

    now = datetime.now()
    closed = list_closed_segments(buffer_dir, safety_margin, now=now)
    selected = select_segments_for_duration(closed, duration_seconds, segment_time)

    # Buffer parado (câmera travada com a captura rodando) não pode
    # virar um clipe velho com status de sucesso.
    staleness = (now - selected[-1].start_time).total_seconds()
    max_staleness_seconds = (
        max_staleness_seconds
        if max_staleness_seconds is not None
        else 3 * segment_time + safety_margin + 5
    )
    if staleness > max_staleness_seconds:
        raise ClipGenerationError(
            f"Buffer de '{quadra_id}' parece parado: segmento mais recente "
            f"tem {staleness:.0f}s de idade (esperado <= {max_staleness_seconds:.0f}s). "
            "A câmera pode estar travada, desconectada ou o capture_camera.sh "
            "não está recebendo dados novos — confira o processo/log da captura."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    concat_list = output_dir / f".concat_{quadra_id}_{ts}.txt"
    temp_concat = output_dir / f".temp_{quadra_id}_{ts}.mp4"
    final_clip = output_dir / f"{quadra_id}_{ts}.mp4"

    try:
        # 1) Concatena os segmentos num temporário (sem reencode).
        concat_list.write_text(
            "\n".join(f"file '{s.path.resolve()}'" for s in selected) + "\n"
        )
        _run(
            [
                "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-c", "copy", str(temp_concat),
            ]
        )

        # 2) Corte dos últimos `duration_seconds`. Com `-c copy` o corte cai
        #    no keyframe anterior, então o clipe pode sair um pouco mais
        #    longo. Só vale com câmeras em H.264; HEVC exigiria reencode.
        _run(
            [
                "ffmpeg", "-y", "-nostdin", "-loglevel", "warning",
                "-sseof", f"-{duration_seconds}",
                "-i", str(temp_concat),
                "-c", "copy",
                str(final_clip),
            ]
        )
    except Exception:
        # Não deixa clipe incompleto em OUTPUT_DIR (é servido publicamente).
        final_clip.unlink(missing_ok=True)
        raise
    finally:
        concat_list.unlink(missing_ok=True)
        temp_concat.unlink(missing_ok=True)

    return final_clip


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Gera clipe dos últimos N segundos.")
    parser.add_argument("quadra_id")
    parser.add_argument("buffer_root", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--duration", type=float, default=35)
    parser.add_argument("--segment-time", type=int, default=2)
    parser.add_argument("--safety-margin", type=float, default=0.5)
    args = parser.parse_args()

    clip = generate_clip(
        args.quadra_id,
        args.buffer_root,
        args.output_dir,
        duration_seconds=args.duration,
        segment_time=args.segment_time,
        safety_margin=args.safety_margin,
    )
    print(str(clip))


if __name__ == "__main__":
    _cli()
