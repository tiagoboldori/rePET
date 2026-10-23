"""Mixagem de áudio com o ffmpeg de verdade (os testes com mock não pegam erro de filtergraph).

O "som da câmera" é um tom de 440 Hz (mono, 16 kHz, AAC, como a câmera entrega) e a música é
um tom de 1000 Hz; medir cada faixa de frequência no arquivo final mostra o que entrou.
Pulam sozinhos se não houver ffmpeg/ffprobe.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from clipper.clip_generator import ClipGenerationError, probe_has_audio
from integrations.audio import apply_audio

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="precisa de ffmpeg e ffprobe",
)

CAMERA_TONE_HZ = 440
MUSIC_TONE_HZ = 1000


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *args], check=True)


def _make_clip(path: Path, *, camera_audio: bool, seconds: int = 5) -> Path:
    video = ["-f", "lavfi", "-i", f"testsrc=size=320x180:rate=20:duration={seconds}"]
    codec = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if camera_audio:
        _ffmpeg(
            *video, "-f", "lavfi", "-i", f"sine=frequency={CAMERA_TONE_HZ}:sample_rate=16000:duration={seconds}",
            "-ac", "1", *codec, "-c:a", "aac", "-b:a", "64k", "-shortest", str(path),
        )
    else:
        _ffmpeg(*video, *codec, str(path))
    return path


def _make_music_dir(tmp_path: Path) -> Path:
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency={MUSIC_TONE_HZ}:sample_rate=44100:duration=10",
            "-ac", "2", str(music_dir / "tom.wav"))
    return music_dir


def _band_level_db(path: Path, hz: int) -> float:
    """Volume médio (dB) do miolo do arquivo (sem os fades) numa faixa estreita em torno de `hz`."""
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-ss", "1", "-t", "3", "-i", str(path), "-vn",
         "-af", f"bandpass=f={hz}:width_type=h:w=40,volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    return float(re.search(r"mean_volume: ([-\d.]+) dB", result.stderr).group(1))


def _audio_stream(path: Path) -> list[str]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_name,sample_rate,channels", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip().split(",")


def test_probe_has_audio_tells_clips_with_and_without_camera_audio_apart(tmp_path):
    assert probe_has_audio(_make_clip(tmp_path / "com.mp4", camera_audio=True)) is True
    assert probe_has_audio(_make_clip(tmp_path / "sem.mp4", camera_audio=False)) is False


def test_probe_has_audio_raises_for_a_file_ffprobe_cannot_read(tmp_path):
    broken = tmp_path / "quebrado.mp4"
    broken.write_bytes(b"isto nao e um video")

    with pytest.raises(ClipGenerationError):
        probe_has_audio(broken)


def test_camera_audio_stays_under_the_music_in_the_final_clip(tmp_path):
    clip = _make_clip(tmp_path / "clip.mp4", camera_audio=True)
    music_dir = _make_music_dir(tmp_path)

    final = apply_audio(clip, music_dir, volume=0.4, fade_seconds=0.5, ambient_volume=0.2)

    assert _audio_stream(final) == ["aac", "44100", "2"]
    music = _band_level_db(final, MUSIC_TONE_HZ)
    camera = _band_level_db(final, CAMERA_TONE_HZ)
    assert camera > -45  # o som da câmera está lá (sem ele a faixa de 440 Hz mede ~-58 dB, só vazamento)
    assert music > -45
    # os dois tons têm o mesmo nível de origem; música a 0.4 e câmera a 0.2 = 6 dB de diferença
    assert 4 <= music - camera <= 8


def test_music_keeps_its_configured_volume_when_camera_audio_is_mixed_in(tmp_path):
    """amix com normalize=0: sem isso o padrão do ffmpeg abaixaria a música em 6 dB."""
    music_dir = _make_music_dir(tmp_path)
    with_camera = apply_audio(_make_clip(tmp_path / "a.mp4", camera_audio=True), music_dir,
                              volume=0.4, fade_seconds=0.5, ambient_volume=0.2)
    music_only = apply_audio(_make_clip(tmp_path / "b.mp4", camera_audio=False), music_dir,
                             volume=0.4, fade_seconds=0.5, ambient_volume=0.2)

    assert abs(_band_level_db(with_camera, MUSIC_TONE_HZ) - _band_level_db(music_only, MUSIC_TONE_HZ)) < 0.5


def test_clip_without_camera_audio_gets_only_the_music(tmp_path):
    clip = _make_clip(tmp_path / "clip.mp4", camera_audio=False)
    music_dir = _make_music_dir(tmp_path)

    final = apply_audio(clip, music_dir, volume=0.4, fade_seconds=0.5)

    assert _audio_stream(final) == ["aac", "44100", "2"]
    assert _band_level_db(final, MUSIC_TONE_HZ) > -45
    assert _band_level_db(final, CAMERA_TONE_HZ) < -50  # só vazamento do filtro, sem tom de 440 Hz


def test_ambient_volume_zero_switches_the_camera_audio_off(tmp_path):
    clip = _make_clip(tmp_path / "clip.mp4", camera_audio=True)
    music_dir = _make_music_dir(tmp_path)

    final = apply_audio(clip, music_dir, volume=0.4, fade_seconds=0.5, ambient_volume=0)

    assert _band_level_db(final, CAMERA_TONE_HZ) < -50


def test_final_clip_keeps_the_duration_and_the_original_video(tmp_path):
    clip = _make_clip(tmp_path / "clip.mp4", camera_audio=True, seconds=5)
    music_dir = _make_music_dir(tmp_path)

    final = apply_audio(clip, music_dir, fade_seconds=0.5)

    def _probe(path, entries):
        return subprocess.run(["ffprobe", "-v", "error", *entries, "-of", "csv=p=0", str(path)],
                              capture_output=True, text=True, check=True).stdout.strip()

    assert abs(float(_probe(final, ["-show_entries", "format=duration"])) - 5.0) < 0.2
    assert _probe(final, ["-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height"]) == "h264,320,180"
