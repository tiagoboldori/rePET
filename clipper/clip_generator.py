#!/usr/bin/env python3
"""
clip_generator.py — corta o clipe final (últimos N segundos) a partir do
buffer contínuo de segmentos gravado por capture_camera.sh.

Chamado pelo handler de `POST /replay/{quadra_id}` quando o botão físico é
pressionado (fase seguinte do projeto). Por enquanto, testável isoladamente
via CLI ou pelo test/run_pipeline_test.sh.

Race condition tratada (conforme já sinalizado no contexto do projeto):
o segmento mais recente pode ainda estar sendo escrito pelo ffmpeg no
momento exato do trigger. O `-f segment` do ffmpeg escreve um arquivo por
vez (nunca dois em paralelo): assim que aparece um segmento NOVO no
buffer, o anterior já foi fechado/finalizado. Por isso só o ÚLTIMO
segmento (o mais recente por horário de início) é tratado como
"possivelmente ainda sendo escrito" — todos os outros são considerados
fechados independente de matemática de relógio. `safety_margin` continua
existindo só como uma folga residual pequena (relógio do servidor vs.
latência de escrita em disco), não é mais o que garante a segurança.
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
    """Lista, em ordem cronológica, os segmentos considerados fechados
    (ou seja, o ffmpeg já rotacionou pra frente deles)."""
    now = now or datetime.now()

    all_segments = []
    for p in buffer_dir.glob("seg_*.mp4"):
        seg = _parse_segment(p)
        if seg is not None:
            all_segments.append(seg)
    all_segments.sort(key=lambda s: s.start_time)

    if not all_segments:
        return []

    # Só o segmento mais recente pode ainda estar sendo escrito — os
    # demais já foram fechados pelo ffmpeg (ver docstring do módulo).
    closed = all_segments[:-1]

    # Folga residual pequena, não o `segment_time` inteiro: proteção extra
    # contra relógio do servidor levemente adiantado/escrita em disco lenta,
    # não é mais a defesa principal contra ler segmento em escrita.
    cutoff = now - timedelta(seconds=safety_margin)
    return [s for s in closed if s.start_time <= cutoff]


def select_segments_for_duration(
    segments: list[Segment], duration_seconds: float, segment_time: int
) -> list[Segment]:
    """Pega, a partir do fim da lista (mais recentes primeiro), segmentos
    fechados suficientes para cobrir >= duration_seconds de vídeo, com uma
    folga de 1 segmento extra pra garantir margem no corte final."""
    if not segments:
        raise ClipGenerationError(
            "Nenhum segmento fechado disponível no buffer ainda "
            "(câmera muito recente ou buffer vazio)."
        )

    needed = int(duration_seconds // segment_time) + 2  # +2 = folga de segurança
    selected = segments[-needed:] if len(segments) > needed else segments

    covered = len(selected) * segment_time
    if covered < duration_seconds:
        # Best-effort: usa o que tem, mas avisa — pode acontecer logo nos
        # primeiros segundos de vida de uma câmera recém-ligada.
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


def generate_clip(
    quadra_id: str,
    buffer_root: Path,
    output_dir: Path,
    duration_seconds: float = 35,
    segment_time: int = 2,
    safety_margin: float = 0.5,
    max_staleness_seconds: float | None = None,
) -> Path:
    """Gera o clipe final dos últimos `duration_seconds` segundos de uma
    quadra e grava no disco persistente (output_dir). Retorna o Path final.
    """
    buffer_dir = Path(buffer_root) / quadra_id
    if not buffer_dir.is_dir():
        raise ClipGenerationError(f"Diretório de buffer não existe: {buffer_dir}")

    now = datetime.now()
    closed = list_closed_segments(buffer_dir, safety_margin, now=now)
    selected = select_segments_for_duration(closed, duration_seconds, segment_time)

    # Protege contra buffer "parado" (câmera travou/desconectou e o
    # capture_camera.sh continua rodando, mas sem receber quadro novo):
    # sem isso, o corte usaria os últimos segmentos DISPONÍVEIS, mesmo que
    # estejam velhos, e devolveria sucesso com um clipe que não é o
    # retroativo real do momento do trigger.
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
        # 1) Concatena os segmentos brutos selecionados (-c copy, rápido,
        #    sem reencode) num arquivo temporário.
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

        # 2) Corte final: pega só os últimos `duration_seconds` a partir do
        #    fim do arquivo concatenado. `-c copy` (sem reencode) — válido
        #    porque as câmeras agora entregam H.264 nativamente (configurado
        #    na própria câmera, ver README), então não existe mais o problema
        #    de compatibilidade de reprodução que HEVC dava (motivo pelo qual
        #    isso reencodava antes, ver nota no README). Trade-off aceito:
        #    com stream copy o corte cai no keyframe mais próximo antes do
        #    ponto pedido, não exatamente em `duration_seconds` — o clipe
        #    final pode sair um pouco mais longo (nunca mais curto). Se
        #    qualquer câmera voltar a entregar HEVC (ou outro codec não
        #    suportado pelos players alvo), isso precisa voltar a reencodar.
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
        # Se o ffmpeg do corte final falhar no meio da escrita, não deixa um
        # arquivo corrompido/incompleto pra trás em OUTPUT_DIR — esse diretório
        # é servido publicamente em /clips e listado em /quadra/{quadra_id}.
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
