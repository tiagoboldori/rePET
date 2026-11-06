"""Testes da transmissão com placar, com um 'ffmpeg' falso (sem câmera nem rede).

O falso é um script que registra cada linha lida do stdin (o FIFO de comandos) e sai
ao receber 'q' — o mesmo contrato que a API usa com o ffmpeg de verdade.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from api import broadcast, config

CAMERA = {"quadra_id": "q1", "input_url": "rtsp://u:p@10.0.0.1/s", "youtube_stream_key": "k-1"}
CAMERA2 = {"quadra_id": "q2", "input_url": "rtsp://u:p@10.0.0.2/s", "youtube_stream_key": "k-2"}
CAMERA3 = {"quadra_id": "q3", "input_url": "rtsp://u:p@10.0.0.3/s", "youtube_stream_key": "k-3"}

FAKE = """#!{python}
import os, sys, threading, time
log = open({log!r}, "a", buffering=1)
log.write("start\\n")
progress = sys.argv[sys.argv.index("-progress") + 1]

def tick():
    n = 0
    while True:
        if not os.path.exists({freeze!r}):  # arquivo-sinal = "congelado": vivo, mas sem quadros
            n += 1
            with open(progress, "w") as fh:
                fh.write(f"frame={{n}}\\nprogress=continue\\n")
        time.sleep(0.2)

threading.Thread(target=tick, daemon=True).start()
for line in sys.stdin:
    log.write(line)
    if line.strip() == "q":
        break
"""


@pytest.fixture
def bc(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROADCAST_DIR", tmp_path / "bc")
    monkeypatch.setattr(config, "BROADCAST_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(config, "BROADCAST_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LIVE_MAX_SECONDS", 0)
    monkeypatch.setattr(broadcast, "RESTART_BACKOFF_MIN", 0.2)
    monkeypatch.setattr(broadcast, "STOP_GRACE_SECONDS", 3.0)
    monkeypatch.setattr(broadcast, "STARTUP_GRACE_SECONDS", 1000.0)  # os testes de travamento baixam isto
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_runtime", {})
    monkeypatch.setattr(broadcast, "_loaded", False)

    def fake_cmd(quadra_id, input_url, stream_key, volume, has_audio=True):
        fakes = tmp_path / "fakes" / quadra_id
        fakes.mkdir(parents=True, exist_ok=True)
        script = fakes / "ffmpeg"
        script.write_text(FAKE.format(
            python=sys.executable, log=str(tmp_path / f"{quadra_id}.stdin.log"),
            freeze=str(tmp_path / f"{quadra_id}.freeze"),
        ))
        script.chmod(0o755)
        return [str(script), str(broadcast._dir(quadra_id)), f"vol={volume}",
                "-progress", str(broadcast._progress(quadra_id))]

    monkeypatch.setattr(broadcast, "_ffmpeg_cmd", fake_cmd)
    monkeypatch.setattr(broadcast, "_probe_audio", lambda url: True)  # sem rede nos testes
    yield tmp_path
    for q in list(broadcast._state):
        try:
            broadcast.stop(q)
        except Exception:
            pass
    broadcast.detach()


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _stdin_log(tmp_path, q="q1"):
    path = tmp_path / f"{q}.stdin.log"
    return path.read_text().splitlines() if path.exists() else []


def _overlay(q="q1"):
    """(nomes, sets, pontos) como o ffmpeg os lê: um arquivo de uma linha por lado."""
    d = broadcast._dir(q)
    return [[(d / f"{kind}_{s}.txt").read_text() for s in "ab"] for kind in ("nome", "sets", "pontos")]


# --- placar ------------------------------------------------------------------

def test_placar_pontos_sets_e_zeragem(bc):
    broadcast.add_point("q1", "a")
    broadcast.add_point("q1", "a")
    broadcast.add_point("q1", "b")
    assert _overlay() == [["DUPLA A", "DUPLA B"], ["0", "0"], ["2", "1"]]  # sem '\n' (viraria glifo)
    snap = broadcast.add_set("q1", "b")  # +1 set e zera os pontos dos DOIS lados
    assert snap["placar"] == {"a": {"pontos": 0, "sets": 0}, "b": {"pontos": 0, "sets": 1}}
    assert _overlay()[1:] == [["0", "1"], ["0", "0"]]


def test_correcoes_nao_ficam_negativas_e_set_negativo_nao_zera_pontos(bc):
    broadcast.add_point("q1", "a", -1)
    assert broadcast.snapshot("q1")["placar"]["a"]["pontos"] == 0
    broadcast.add_point("q1", "b", 3)
    broadcast.add_set("q1", "a", 1)
    broadcast.add_point("q1", "b", 2)
    snap = broadcast.add_set("q1", "a", -1)
    assert snap["placar"]["a"]["sets"] == 0 and snap["placar"]["b"]["pontos"] == 2


def test_lado_invalido(bc):
    with pytest.raises(broadcast.BroadcastError) as exc:
        broadcast.add_point("q1", "c")
    assert exc.value.status == 422


def test_quadra_id_invalido_nao_vira_caminho(bc):
    with pytest.raises(broadcast.BroadcastError):
        broadcast.snapshot("../etc")


def test_nomes_sao_saneados(bc):
    snap = broadcast.set_names("q1", "  joão   e maria  \n%{x}", "Um nome muito mais comprido que cabe")
    assert snap["nomes"]["a"] == "JOÃO E MARIA X"[:14].strip()
    assert len(snap["nomes"]["b"]) <= broadcast.MAX_NAME_LENGTH
    assert "\n" not in snap["nomes"]["a"] and "%" not in snap["nomes"]["a"]
    assert broadcast.set_names("q1", "   ", None)["nomes"]["a"] == "DUPLA A"


def test_correcao_manual_do_placar(bc):
    snap = broadcast.set_score("q1", {"pontos_a": 40, "pontos_b": 15, "sets_a": 1, "sets_b": None})
    assert snap["placar"] == {"a": {"pontos": 40, "sets": 1}, "b": {"pontos": 15, "sets": 0}}


# --- ciclo de vida -----------------------------------------------------------

def test_start_stop_e_idempotencia(bc):
    assert broadcast.start("q1", CAMERA) is True
    assert broadcast.start("q1", CAMERA) is False
    assert _wait(lambda: _stdin_log(bc) == ["start"])
    snap = broadcast.snapshot("q1")
    assert snap["no_ar"] and snap["no_ar_ha_segundos"] is not None
    pid = broadcast._state["q1"]["pid"]
    assert broadcast.stop("q1") is True
    assert _wait(lambda: not Path(f"/proc/{pid}").exists())
    assert _stdin_log(bc)[-1] == "q"  # encerrou pelo caminho gracioso
    assert broadcast.stop("q1") is False
    assert not broadcast.snapshot("q1")["no_ar"]


def test_start_sem_stream_key_e_limite_de_simultaneas(bc):
    with pytest.raises(broadcast.BroadcastError) as exc:
        broadcast.start("q1", {"quadra_id": "q1", "input_url": "x"})
    assert exc.value.status == 422
    broadcast.start("q1", CAMERA)
    broadcast.start("q2", CAMERA2)
    with pytest.raises(broadcast.BroadcastError, match="limite de 2"):
        broadcast.start("q3", CAMERA3)
    broadcast.stop("q1")
    assert broadcast.start("q3", CAMERA3) is True  # liberou uma vaga


def test_start_zera_o_placar_a_menos_que_reset_false(bc):
    broadcast.add_point("q1", "a", 5)
    broadcast.start("q1", CAMERA, {"a": "Ana", "b": "Bia"})
    snap = broadcast.snapshot("q1")
    assert snap["placar"]["a"]["pontos"] == 0 and snap["nomes"] == {"a": "ANA", "b": "BIA"}
    broadcast.stop("q1")
    broadcast.add_point("q1", "a", 5)
    broadcast.start("q1", CAMERA, reset=False)
    assert broadcast.snapshot("q1")["placar"]["a"]["pontos"] == 5


def test_volume_chega_ao_stdin_do_ffmpeg_com_a_live_no_ar(bc):
    broadcast.start("q1", CAMERA)
    assert _wait(lambda: _stdin_log(bc) == ["start"])
    snap = broadcast.change_volume("q1", +1)
    assert snap["volume"] == 110
    broadcast.change_volume("q1", -1, 30)
    broadcast.set_volume("q1", 9999)  # corta no máximo
    assert broadcast.snapshot("q1")["volume"] == config.BROADCAST_VOLUME_MAX
    assert _wait(lambda: len(_stdin_log(bc)) == 4)
    assert _stdin_log(bc)[1:] == [
        "cvolume@vol -1 volume 1.100",
        "cvolume@vol -1 volume 0.800",
        "cvolume@vol -1 volume 4.000",
    ]


def test_volume_fora_do_ar_so_guarda_e_o_proximo_ffmpeg_nasce_com_ele(bc):
    assert broadcast.set_volume("q1", 50)["volume"] == 50
    assert broadcast.set_volume("q1", -5)["volume"] == 0
    broadcast.set_volume("q1", 150)
    broadcast.start("q1", CAMERA, reset=False)  # mesmo jogo: mantém o volume escolhido
    assert broadcast._state["q1"]["volume"] == 150


def test_jogo_novo_nao_nasce_mudo_por_causa_do_volume_do_jogo_anterior(bc):
    broadcast.set_volume("q1", 0)  # o operador mutou no jogo anterior
    broadcast.start("q1", CAMERA)  # reset=True é o padrão
    assert broadcast.snapshot("q1")["volume"] == 100
    broadcast.stop("q1")
    broadcast.start("q1", CAMERA, volume=70)  # ...ou escolhe outro já na largada
    assert broadcast.snapshot("q1")["volume"] == 70


def test_supervisor_relanca_o_ffmpeg_que_morre(bc):
    broadcast.start("q1", CAMERA)
    pid = broadcast._state["q1"]["pid"]
    os.kill(pid, signal.SIGKILL)
    assert _wait(lambda: broadcast._state["q1"]["pid"] not in (None, pid), 8)
    assert broadcast.snapshot("q1")["reinicios"] >= 1
    assert broadcast.snapshot("q1")["no_ar"]


def test_prazo_encerra_a_transmissao(bc, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_SECONDS", 1.5)
    broadcast.start("q1", CAMERA)
    pid = broadcast._state["q1"]["pid"]
    assert _wait(lambda: not broadcast.snapshot("q1")["no_ar"], 8)
    assert _wait(lambda: not Path(f"/proc/{pid}").exists())


def test_reinicio_da_api_readota_o_ffmpeg_sem_derrubar_e_o_estado_volta(bc, monkeypatch):
    broadcast.start("q1", CAMERA)
    broadcast.add_point("q1", "a", 3)
    pid = broadcast._state["q1"]["pid"]
    assert _wait(lambda: _stdin_log(bc) == ["start"])
    # "reinício": a API perde a memória; o ffmpeg falso segue vivo
    broadcast.detach()
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    broadcast.resume({"q1": CAMERA})
    assert broadcast.snapshot("q1")["placar"]["a"]["pontos"] == 3
    assert broadcast._state["q1"]["pid"] == pid  # readotado, não relançado
    broadcast.set_volume("q1", 120)  # o FIFO continua funcionando depois do "reinício"
    assert _wait(lambda: _stdin_log(bc)[-1] == "cvolume@vol -1 volume 1.200")
    assert _stdin_log(bc).count("start") == 1
    broadcast.stop("q1")
    # (aqui o falso é filho do pytest e vira zumbi; numa API reiniciada o init o recolhe)
    assert _wait(lambda: not broadcast._pid_is_ours(pid, "q1"))


def test_resume_relanca_se_o_ffmpeg_morreu_durante_o_reinicio(bc, monkeypatch):
    broadcast.start("q1", CAMERA)
    pid = broadcast._state["q1"]["pid"]
    broadcast.detach()
    os.kill(pid, signal.SIGKILL)
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    broadcast.resume({"q1": CAMERA})
    assert _wait(lambda: broadcast._state["q1"]["pid"] not in (None, pid), 8)


def test_comando_real_do_ffmpeg_tem_o_overlay_e_o_volume():
    cmd = broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 120)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.startswith("[0:v]scale=1920:1080,") and "volume@vol=1.200" in graph and "reload=1" in graph and "expansion=none" in graph
    assert cmd[-1].endswith("/KEY") and "libx264" in cmd


def test_camera_sem_audio_gera_trilha_de_silencio_em_vez_de_falhar():
    cmd = broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100, has_audio=False)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert any(a.startswith("anullsrc") for a in cmd) and "-shortest" in cmd and "-re" in cmd
    assert ";[1:a]volume@vol" in graph and "[0:a]" not in graph
    with_audio = broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100)
    assert not any(a.startswith("anullsrc") for a in with_audio) and "-shortest" not in with_audio
    assert ";[0:a]volume@vol" in with_audio[with_audio.index("-filter_complex") + 1]


def test_probe_audio(monkeypatch):
    from types import SimpleNamespace

    def fake(stdout="", returncode=0, exc=None):
        def run(*a, **k):
            if exc:
                raise exc
            return SimpleNamespace(stdout=stdout, returncode=returncode)
        return run

    monkeypatch.undo()  # volta ao _probe_audio de verdade (a fixture o trocou)
    monkeypatch.setattr(broadcast.subprocess, "run", fake("audio\n"))
    assert broadcast._probe_audio("rtsp://x") is True
    monkeypatch.setattr(broadcast.subprocess, "run", fake(""))
    assert broadcast._probe_audio("rtsp://x") is False  # só vídeo
    monkeypatch.setattr(broadcast.subprocess, "run", fake("", returncode=1))
    assert broadcast._probe_audio("rtsp://x") is True  # câmera não respondeu: na dúvida, assume que tem
    monkeypatch.setattr(broadcast.subprocess, "run", fake(exc=broadcast.subprocess.TimeoutExpired("ffprobe", 20)))
    assert broadcast._probe_audio("rtsp://x") is True


# --- edge cases da stream ----------------------------------------------------

def _fast_stall(monkeypatch):
    monkeypatch.setattr(broadcast, "STARTUP_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(broadcast, "STALL_SECONDS", 1.5)


def test_ffmpeg_saudavel_nao_e_morto_e_aparece_como_transmitindo(bc, monkeypatch):
    _fast_stall(monkeypatch)
    broadcast.start("q1", CAMERA)
    pid = broadcast._state["q1"]["pid"]
    assert _wait(lambda: broadcast.snapshot("q1")["transmitindo"], 5)
    time.sleep(3.5)  # bem além do prazo de travamento: quadros avançando, nada a matar
    snap = broadcast.snapshot("q1")
    assert snap["transmitindo"] and snap["travamentos"] == 0 and broadcast._state["q1"]["pid"] == pid


def test_ffmpeg_vivo_mas_sem_quadros_e_morto_e_relancado(bc, monkeypatch):
    _fast_stall(monkeypatch)
    broadcast.start("q1", CAMERA)
    pid = broadcast._state["q1"]["pid"]
    assert _wait(lambda: broadcast.snapshot("q1")["transmitindo"], 5)
    (bc / "q1.freeze").touch()  # congela: continua vivo, mas não produz quadro
    assert _wait(lambda: broadcast._state["q1"]["pid"] not in (None, pid), 10)
    snap = broadcast.snapshot("q1")
    assert snap["travamentos"] == 1 and snap["no_ar"]
    (bc / "q1.freeze").unlink()
    assert _wait(lambda: broadcast.snapshot("q1")["transmitindo"], 5)  # voltou a transmitir


def test_ffmpeg_parado_por_sigstop_tambem_e_detectado(bc, monkeypatch):
    _fast_stall(monkeypatch)
    broadcast.start("q1", CAMERA)
    pid = broadcast._state["q1"]["pid"]
    assert _wait(lambda: broadcast.snapshot("q1")["transmitindo"], 5)
    os.kill(pid, signal.SIGSTOP)  # nem o 'q' ele atende parado; só o SIGKILL resolve
    assert _wait(lambda: broadcast._state["q1"]["pid"] not in (None, pid), 10)
    assert broadcast.snapshot("q1")["travamentos"] == 1


def test_ffmpeg_que_morre_na_largada_aparece_como_tentando_e_nao_transmitindo(bc, monkeypatch):
    monkeypatch.setattr(broadcast, "_ffmpeg_cmd", lambda *a, **k: [sys.executable, "-c", "import sys; sys.exit(1)"])
    broadcast.start("q1", CAMERA)  # ex.: YouTube/câmera fora: o ffmpeg sai na hora, de novo e de novo
    assert _wait(lambda: broadcast.snapshot("q1")["reinicios"] >= 2, 8)
    snap = broadcast.snapshot("q1")
    assert snap["no_ar"] is True and snap["transmitindo"] is False


def test_sondagem_de_audio_nao_prende_o_lock_das_outras_quadras(bc, monkeypatch):
    import threading

    monkeypatch.setattr(broadcast, "_probe_audio", lambda url: time.sleep(2) or True)  # câmera lenta
    t = threading.Thread(target=broadcast.start, args=("q1", CAMERA))
    t.start()
    time.sleep(0.3)
    begin = time.time()
    broadcast.add_point("q2", "a")  # outra quadra, enquanto q1 ainda sonda
    assert time.time() - begin < 0.5
    t.join()
    assert broadcast.snapshot("q1")["no_ar"]


def test_estado_corrompido_nao_derruba_a_subida(bc):
    config.BROADCAST_STATE_FILE.write_text("{isto nao e json")
    broadcast.resume({"q1": CAMERA})  # não pode levantar exceção
    assert broadcast.snapshot("q1")["no_ar"] is False
    assert config.BROADCAST_STATE_FILE.with_suffix(".corrupt").exists()


def test_estado_com_campos_estragados_e_saneado(bc):
    config.BROADCAST_STATE_FILE.write_text(json.dumps({
        "q1": {"no_ar": "talvez", "nomes": "x", "placar": {"a": [1], "b": {"pontos": -4, "sets": "dois"}},
               "volume": "alto", "pid": -3, "prazo": "amanha"},
        "q2": ["lista"], "../x": {"no_ar": True},
    }))
    snap = broadcast.snapshot("q1")
    assert snap["no_ar"] is False and snap["nomes"] == {"a": "DUPLA A", "b": "DUPLA B"}
    assert snap["placar"] == {"a": {"pontos": 0, "sets": 0}, "b": {"pontos": 0, "sets": 0}}
    assert snap["volume"] == 100
    assert "q2" not in broadcast._state and "../x" not in broadcast._state


def test_ffmpeg_sem_registro_e_readotado_com_o_placar_do_overlay(bc, monkeypatch):
    broadcast.start("q1", CAMERA, {"a": "Ana", "b": "Bia"})
    broadcast.add_point("q1", "a", 4)
    broadcast.add_set("q1", "b")
    broadcast.add_point("q1", "b", 2)
    pid = broadcast._state["q1"]["pid"]
    assert _wait(lambda: broadcast.snapshot("q1")["transmitindo"], 5)
    broadcast.detach()
    config.BROADCAST_STATE_FILE.unlink()  # perdeu o arquivo de estado; o ffmpeg segue publicando
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    summary = broadcast.resume({"q1": CAMERA})
    assert summary["orfas_adotadas"] == ["q1"]
    snap = broadcast.snapshot("q1")
    assert snap["no_ar"] and broadcast._state["q1"]["pid"] == pid
    assert snap["nomes"] == {"a": "ANA", "b": "BIA"}
    assert snap["placar"] == {"a": {"pontos": 0, "sets": 0}, "b": {"pontos": 2, "sets": 1}}


def test_quadra_removida_do_cameras_json_tem_o_ffmpeg_encerrado(bc, monkeypatch):
    broadcast.start("q1", CAMERA)
    pid = broadcast._state["q1"]["pid"]
    broadcast.detach()
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    summary = broadcast.resume({})  # cameras.json sem a quadra
    assert "q1" in summary["encerradas"]
    assert _wait(lambda: not broadcast._pid_is_ours(pid, "q1"), 5)
    assert not broadcast.snapshot("q1")["no_ar"]


def test_prazo_vencido_enquanto_a_api_estava_fora_encerra_na_subida(bc, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_SECONDS", 3600)
    broadcast.start("q1", CAMERA)
    pid = broadcast._state["q1"]["pid"]
    broadcast.detach()
    saved = json.loads(config.BROADCAST_STATE_FILE.read_text())
    saved["q1"]["prazo"] = time.time() - 5  # venceu durante a queda
    config.BROADCAST_STATE_FILE.write_text(json.dumps(saved))
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    broadcast.resume({"q1": CAMERA})
    assert _wait(lambda: not broadcast.snapshot("q1")["no_ar"], 8)
    assert _wait(lambda: not broadcast._pid_is_ours(pid, "q1"), 5)


def test_log_grande_e_rotacionado_na_largada(bc, monkeypatch):
    monkeypatch.setattr(broadcast, "LOG_MAX_BYTES", 100)
    config.BROADCAST_LOG_DIR.mkdir(parents=True)
    log = config.BROADCAST_LOG_DIR / "broadcast_q1.log"
    log.write_text("x" * 500)
    broadcast.start("q1", CAMERA)
    assert log.with_suffix(".log.1").read_text() == "x" * 500
