"""Redução de ruído do áudio da transmissão com placar (AUDIO_DENOISE).

Os testes de cadeia são puros (sem ffmpeg). Os funcionais usam o ffmpeg de verdade com áudio sintético.
"""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from api import broadcast, config

MEDIO = "highpass=f=100,afftdn=nr=14:nf=-45:tn=1"


@pytest.fixture(autouse=True)
def ffmpeg_tem_o_filtro(monkeypatch):
    """Por padrão, finge que o ffmpeg tem o afftdn (os testes funcionais checam o de verdade)."""
    monkeypatch.setattr(broadcast, "_denoise_filter_ok", True)
    monkeypatch.setattr(config, "AUDIO_DENOISE", "medio")


# --- configuração ----------------------------------------------------------------------------

@pytest.mark.parametrize("raw,esperado", [
    (None, "medio"), ("", "medio"), ("   ", "medio"),
    ("medio", "medio"), ("MEDIO", "medio"), ("médio", "medio"), (" Médio ", "medio"),
    ("leve", "leve"), ("forte", "forte"),
    ("off", "off"), ("OFF", "off"), ("0", "off"), ("false", "off"), ("nao", "off"), ("não", "off"), ("desligado", "off"),
])
def test_parse_audio_denoise(raw, esperado):
    assert config.parse_audio_denoise(raw) == esperado


def test_valor_desconhecido_cai_no_padrao_com_aviso_e_nao_levanta(capsys):
    assert config.parse_audio_denoise("ultra") == "medio"
    assert "AUDIO_DENOISE='ultra' desconhecido" in capsys.readouterr().out


def test_o_padrao_e_o_nivel_medio():
    assert config.parse_audio_denoise("medio") == "medio"
    # sem a variável no ambiente, o padrão declarado no config é medio
    assert config.AUDIO_DENOISE in ("off", "leve", "medio", "forte")


# --- cadeia de filtros -----------------------------------------------------------------------

def test_os_tres_niveis_tem_as_cadeias_medidas():
    assert broadcast.DENOISE_LEVELS == {
        "leve": "highpass=f=100,afftdn=nr=10:nf=-50:tn=1",
        "medio": MEDIO,
        "forte": "highpass=f=100,afftdn=nr=25:nf=-35:tn=1",
    }


@pytest.mark.parametrize("nivel", ["leve", "medio", "forte"])
def test_cada_nivel_entra_no_comando(monkeypatch, nivel):
    monkeypatch.setattr(config, "AUDIO_DENOISE", nivel)
    cmd = broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100)
    assert broadcast.DENOISE_LEVELS[nivel] in cmd[cmd.index("-filter_complex") + 1]


def test_com_o_padrao_o_filtro_entra_antes_do_volume_e_do_limitador():
    graph = (lambda c: c[c.index("-filter_complex") + 1])(broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 120))
    audio = graph.split(";[0:a]")[1]
    assert audio.startswith(MEDIO + ",volume@vol=1.200:eval=frame,alimiter=limit=0.95,aresample=44100,")
    # ordem: grave cortado e redução de ruído, depois volume, depois limitador, depois o upmix para 44,1 kHz estéreo
    assert audio.index("highpass") < audio.index("afftdn") < audio.index("volume@vol") < audio.index("alimiter") < audio.index("aresample")


def test_desligado_o_comando_e_identico_ao_de_antes(monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DENOISE", "off")
    graph = (lambda c: c[c.index("-filter_complex") + 1])(broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100))
    assert ";[0:a]volume@vol=1.000:eval=frame,alimiter=limit=0.95,aresample=44100,aformat=channel_layouts=stereo[a]" in graph
    assert "afftdn" not in graph and "highpass" not in graph


def test_o_volume_ao_vivo_continua_enderecando_o_mesmo_filtro():
    """O comando de volume em tempo real fala com `volume@vol`: o estágio novo não pode mudar esse nome."""
    graph = (lambda c: c[c.index("-filter_complex") + 1])(broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100))
    assert graph.count("volume@vol=") == 1


def test_sem_audio_da_camera_nao_aplica_filtro_nenhum():
    cmd = broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100, has_audio=False)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "afftdn" not in graph and "highpass" not in graph and ";[1:a]volume@vol" in graph


def test_filtro_chamado_direto_sem_o_argumento_novo_nao_muda():
    assert ";[0:a]volume@vol=1.000" in broadcast._filter_graph("q1", 100)
    assert ";[1:a]volume@vol=1.000" in broadcast._filter_graph("q1", 100, "1:a")


# --- ffmpeg sem o filtro: a live não pode quebrar ----------------------------------------------

def test_ffmpeg_sem_afftdn_transmite_sem_filtro_e_avisa_uma_vez(monkeypatch, capsys):
    monkeypatch.setattr(broadcast, "_denoise_filter_ok", None)
    chamadas = []

    def fake_run(cmd, **kw):
        chamadas.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=" ... afwtdn ... anlmdn ...\n", stderr="")

    monkeypatch.setattr(broadcast.subprocess, "run", fake_run)
    for _ in range(3):
        cmd = broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100)
        assert "afftdn" not in cmd[cmd.index("-filter_complex") + 1]
    assert len(chamadas) == 1, "a checagem do ffmpeg roda uma vez só"
    assert capsys.readouterr().out.count("não tem o filtro afftdn") == 1


def test_ffmpeg_que_nao_responde_a_pergunta_nao_aplica_filtro(monkeypatch):
    monkeypatch.setattr(broadcast, "_denoise_filter_ok", None)

    def quebra(*a, **k):
        raise OSError("sem ffmpeg")

    monkeypatch.setattr(broadcast.subprocess, "run", quebra)
    assert broadcast._denoise_chain() is None


# --- ffmpeg de verdade ---------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None
    or " afftdn " not in subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout,
    reason="precisa de ffmpeg com afftdn",
)


def _mean_volume(source: str, chain: str) -> float:
    """Volume médio (dB) dos últimos 6 s de `source` depois de `chain` (dá tempo de o rastreio de ruído se ajustar)."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-f", "lavfi", "-i", source, "-af", f"{chain},atrim=start=6,volumedetect",
         "-f", "null", "-"], capture_output=True, text=True,
    ).stderr
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", out).group(1))


@needs_ffmpeg
def test_o_rumor_grave_e_cortado_e_um_tom_de_voz_passa_intacto():
    rumor = "sine=frequency=40:sample_rate=16000:duration=12,volume=0.5"
    tone = "sine=frequency=1000:sample_rate=16000:duration=12,volume=0.5"
    # highpass de 2 polos em 100 Hz: ~16 dB a 40 Hz (e ~9 dB a 60 Hz)
    assert _mean_volume(rumor, "anull") - _mean_volume(rumor, MEDIO) > 14, "o rumor de 40 Hz tem de cair mais de 14 dB"
    assert abs(_mean_volume(tone, "anull") - _mean_volume(tone, MEDIO)) < 0.5, "um tom de 1 kHz não pode mudar"


@needs_ffmpeg
@pytest.mark.parametrize("nivel", ["leve", "medio", "forte"])
def test_todos_os_niveis_rodam_no_ffmpeg_sem_erro(nivel):
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=amplitude=0.02:sample_rate=16000:duration=2",
         "-af", broadcast.DENOISE_LEVELS[nivel], "-f", "null", "-"], capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


@needs_ffmpeg
def test_o_grafo_completo_da_transmissao_e_aceito_pelo_ffmpeg(tmp_path, monkeypatch):
    """Roda o filter_complex real (vídeo com placar + áudio com a redução de ruído) em fontes sintéticas."""
    if not shutil.os.path.exists(config.BROADCAST_FONT):
        pytest.skip("fonte do placar não instalada")
    monkeypatch.setattr(config, "BROADCAST_DIR", tmp_path / "bc")
    monkeypatch.setattr(config, "BROADCAST_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    broadcast._write_overlay("q1")
    graph = broadcast._filter_graph("q1", 100, "1:a", broadcast._denoise_chain())
    assert "afftdn" in graph
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=640x360:d=2:r=25",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2", "-filter_complex", graph,
         "-map", "[v]", "-map", "[a]", "-t", "2", "-f", "null", "-"], capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
