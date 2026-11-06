"""Corte do clipe no instante do acionamento.

A fonte sintética grava o número do frame como bits na primeira linha da imagem; como o
`-re` segura o ritmo em tempo real, o número do último frame do clipe diz em que instante
ele termina. Pulam sozinhos se não houver ffmpeg/ffprobe.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from clipper.clip_generator import (
    compute_cut_window,
    generate_clip,
    probe_duration_seconds,
    wait_for_segment_after,
)

FPS = 15
SEGMENT_TIME = 2
WIDTH, HEIGHT = 320, 180
BIT_WIDTH = 20  # 16 blocos de 20px = 16 bits do número do frame

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="precisa de ffmpeg e ffprobe",
)


def test_compute_cut_window_termina_no_acionamento():
    fim_do_arquivo = datetime(2026, 1, 1, 12, 0, 10)
    trigger = fim_do_arquivo - timedelta(seconds=1.5)
    assert compute_cut_window(20.0, fim_do_arquivo, trigger, 8.0) == (10.5, 18.5)


def test_compute_cut_window_acionamento_mais_novo_que_o_gravado_usa_o_fim_do_arquivo():
    fim_do_arquivo = datetime(2026, 1, 1, 12, 0, 10)
    trigger = fim_do_arquivo + timedelta(seconds=3)
    assert compute_cut_window(20.0, fim_do_arquivo, trigger, 8.0) == (12.0, 20.0)


def test_compute_cut_window_buffer_curto_comeca_em_zero():
    fim_do_arquivo = datetime(2026, 1, 1, 12, 0, 10)
    assert compute_cut_window(5.0, fim_do_arquivo, fim_do_arquivo, 35.0) == (0.0, 5.0)


def test_wait_for_segment_after_estoura_o_prazo_sem_segmento_novo(tmp_path):
    (tmp_path / "seg_20260101120000.mp4").touch()
    trigger = datetime(2026, 1, 1, 12, 0, 1)
    assert wait_for_segment_after(tmp_path, trigger, timeout_seconds=0.3) is False


def test_wait_for_segment_after_ve_segmento_posterior_ao_acionamento(tmp_path):
    (tmp_path / "seg_20260101120000.mp4").touch()
    (tmp_path / "seg_20260101120002.mp4").touch()
    trigger = datetime(2026, 1, 1, 12, 0, 1, 900_000)
    assert wait_for_segment_after(tmp_path, trigger, timeout_seconds=0.3) is True


def _start_capture(buffer_dir: Path) -> tuple[subprocess.Popen, float]:
    source = (
        f"color=c=gray:size={WIDTH}x{HEIGHT}:rate={FPS},format=yuv420p,"
        f"geq=lum='if(lt(Y,16),255*mod(floor(N/pow(2,floor(X/{BIT_WIDTH}))),2),128)':cb=128:cr=128"
    )
    started = time.time()
    proc = subprocess.Popen(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-re", "-f", "lavfi", "-i", source,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-g", str(SEGMENT_TIME * FPS), "-an",
            "-f", "segment", "-segment_time", str(SEGMENT_TIME), "-reset_timestamps", "1",
            "-strftime", "1", str(buffer_dir / "seg_%Y%m%d%H%M%S.mp4"),
        ]
    )
    return proc, started


def _last_frame_number(clip: Path) -> int:
    raw = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-sseof", "-0.2", "-i", str(clip),
            "-vf", "format=gray", "-f", "rawvideo", "-",
        ],
        capture_output=True, check=True,
    ).stdout
    frame = raw[-WIDTH * HEIGHT:]
    row = frame[: WIDTH]
    return sum(
        (1 << bit) for bit in range(WIDTH // BIT_WIDTH)
        if row[bit * BIT_WIDTH + BIT_WIDTH // 2] > 128
    )


@needs_ffmpeg
def test_clipe_termina_no_instante_do_acionamento(tmp_path):
    buffer_root = tmp_path / "buffer"
    buffer_dir = buffer_root / "quadra-teste"
    buffer_dir.mkdir(parents=True)
    proc, started = _start_capture(buffer_dir)
    try:
        time.sleep(9)  # buffer com mais que a duração pedida
        trigger = datetime.now()
        trigger_epoch = trigger.timestamp()
        clip = generate_clip(
            "quadra-teste", buffer_root, tmp_path / "out",
            duration_seconds=5, segment_time=SEGMENT_TIME, trigger_time=trigger,
        )
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    end_epoch = started + _last_frame_number(clip) / FPS
    # Antes (só segmentos fechados) o clipe terminava 1-3 s antes do botão.
    assert abs(end_epoch - trigger_epoch) < 0.5, f"clipe termina {end_epoch - trigger_epoch:+.2f}s do botão"
    # Início cai no keyframe anterior: no máximo um segmento a mais.
    assert 5 - 0.5 <= probe_duration_seconds(clip) <= 5 + SEGMENT_TIME + 0.5
