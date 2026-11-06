"""Ajustes de áudio por quadra (redução de ruído e corte de agudos): estado, persistência e troca AO VIVO.

Usa um ffmpeg falso que lê comandos pela entrada padrão e responde como o real: `Command reply ... ret:0` quando
aceita, `ret:-38` quando o filtro não existe (transmissão que subiu com a cadeia antiga) ou nada (não confirma).
"""
from __future__ import annotations

import json
import sys
import time

import pytest

from api import broadcast, config

CAMERA = {"quadra_id": "q1", "input_url": "rtsp://u:p@10.0.0.1/s", "youtube_stream_key": "k-1"}
PADRAO = {"reducao_ruido": "medio", "corte_agudos": "medio"}

FAKE = """#!{python}
import os, sys, threading, time
log = open({log!r}, "a", buffering=1)
log.write("start\\n")
progress = sys.argv[sys.argv.index("-progress") + 1]

def tick():
    n = 0
    while True:
        n += 1
        with open(progress, "w") as fh:
            fh.write(f"frame={{n}}\\nprogress=continue\\n")
        time.sleep(0.2)

threading.Thread(target=tick, daemon=True).start()
for line in sys.stdin:
    log.write(line)
    if line.strip() == "q":
        break
    if line.startswith("c"):  # comando de filtro: responde como o ffmpeg de verdade
        if os.path.exists({silent!r}):
            continue
        ret = -38 if os.path.exists({fail!r}) else 0
        sys.stderr.write(f"\\nEnter command: <target>|all <time>|-1 <command>[ <argument>]\\n\\nCommand reply for stream 0: ret:{{ret}} res:\\n")
        sys.stderr.flush()
"""


@pytest.fixture
def bc(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROADCAST_DIR", tmp_path / "bc")
    monkeypatch.setattr(config, "BROADCAST_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(config, "BROADCAST_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LIVE_MAX_SECONDS", 0)
    monkeypatch.setattr(config, "AUDIO_DENOISE", "medio")
    monkeypatch.setattr(config, "AUDIO_CORTE_AGUDOS", "medio")
    monkeypatch.setattr(broadcast, "RESTART_BACKOFF_MIN", 0.2)
    monkeypatch.setattr(broadcast, "STOP_GRACE_SECONDS", 3.0)
    monkeypatch.setattr(broadcast, "STARTUP_GRACE_SECONDS", 1000.0)
    monkeypatch.setattr(broadcast, "AUDIO_APPLY_TIMEOUT", 0.6)
    monkeypatch.setattr(broadcast, "AUDIO_APPLY_PER_COMMAND", 0.2)
    monkeypatch.setattr(broadcast, "_denoise_filter_ok", True)
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_runtime", {})
    monkeypatch.setattr(broadcast, "_audio_live", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    launches: list[dict | None] = []

    def fake_cmd(quadra_id, input_url, stream_key, volume, has_audio=True, rtmp_url=None, audio=None):
        launches.append(audio)
        script = tmp_path / "fakes" / quadra_id / "ffmpeg"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(FAKE.format(
            python=sys.executable, log=str(tmp_path / f"{quadra_id}.stdin.log"),
            silent=str(tmp_path / f"{quadra_id}.silent"), fail=str(tmp_path / f"{quadra_id}.fail"),
        ))
        script.chmod(0o755)
        return [str(script), "-progress", str(broadcast._progress(quadra_id))]

    monkeypatch.setattr(broadcast, "_ffmpeg_cmd", fake_cmd)
    monkeypatch.setattr(broadcast, "_probe_audio", lambda url: True)
    broadcast.launches = launches  # type: ignore[attr-defined]
    yield tmp_path
    for q in list(broadcast._state):
        try:
            broadcast.stop(q)
        except Exception:
            pass
    broadcast.detach()


def _wait(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _stdin(bc) -> list[str]:
    try:
        return [l for l in (bc / "q1.stdin.log").read_text().splitlines() if l and l != "start"]
    except OSError:
        return []


# --- fora do ar: padrão, validação e persistência ---------------------------------------------

def test_sem_ajuste_gravado_vale_o_padrao_do_servidor(bc, monkeypatch):
    v = broadcast.audio_view("q1")
    assert (v["reducao_ruido"], v["corte_agudos"]) == ("medio", "medio") and v["padrao"] == PADRAO
    assert v["niveis"] == {"reducao_ruido": ["off", "leve", "medio", "forte"],
                           "corte_agudos": ["off", "suave", "medio", "forte", "maximo"]}
    assert v["no_ar"] is False and v["aplicado_ao_vivo"] is None
    monkeypatch.setattr(config, "AUDIO_CORTE_AGUDOS", "forte")  # o padrão mudou no .env: quem não tem ajuste acompanha
    assert broadcast.audio_view("q1")["corte_agudos"] == "forte"


def test_o_estado_da_transmissao_traz_o_audio(bc):
    assert broadcast.snapshot("q1")["audio"] == PADRAO
    broadcast.set_audio("q1", corte_agudos="maximo")
    assert broadcast.snapshot("q1")["audio"] == {"reducao_ruido": "medio", "corte_agudos": "maximo"}


def test_fora_do_ar_so_grava_e_atualiza_so_o_que_veio(bc):
    view, changed = broadcast.set_audio("q1", corte_agudos="forte")
    assert changed and view["corte_agudos"] == "forte" and view["reducao_ruido"] == "medio"
    assert view["aplicado_ao_vivo"] is None and _stdin(bc) == []
    view, _ = broadcast.set_audio("q1", reducao_ruido="leve")
    assert (view["reducao_ruido"], view["corte_agudos"]) == ("leve", "forte")  # o outro campo ficou


def test_sobrevive_a_reinicio_da_api(bc, monkeypatch):
    broadcast.set_audio("q1", reducao_ruido="off", corte_agudos="suave")
    saved = json.loads((bc / "state.json").read_text())["q1"]["audio"]
    assert saved == {"reducao_ruido": "off", "corte_agudos": "suave"}
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    v = broadcast.audio_view("q1")
    assert (v["reducao_ruido"], v["corte_agudos"]) == ("off", "suave")


def test_gravar_explicitamente_o_valor_do_padrao_fixa_a_escolha(bc, monkeypatch):
    view, changed = broadcast.set_audio("q1", corte_agudos="medio")  # igual ao padrão, mas escolhido
    assert changed is True and broadcast._state["q1"]["audio"] == {"corte_agudos": "medio"}
    monkeypatch.setattr(config, "AUDIO_CORTE_AGUDOS", "forte")
    assert broadcast.audio_view("q1")["corte_agudos"] == "medio"   # a escolha não acompanha mais o padrão
    _, changed = broadcast.set_audio("q1", corte_agudos="medio")
    assert changed is False                                         # repetir o mesmo valor não é mudança


def test_valores_invalidos_e_corpo_vazio_dao_422_sem_gravar(bc):
    for kw in ({"corte_agudos": "ultra"}, {"reducao_ruido": "enorme"}, {"corte_agudos": ""}, {"reducao_ruido": "OFFF"}):
        with pytest.raises(broadcast.BroadcastError) as exc:
            broadcast.set_audio("q1", **kw)
        assert exc.value.status == 422 and "deve ser um de:" in str(exc.value)
    with pytest.raises(broadcast.BroadcastError) as exc:
        broadcast.set_audio("q1")
    assert exc.value.status == 422 and "informe" in str(exc.value)
    assert broadcast.audio_view("q1")["corte_agudos"] == "medio" and broadcast._state["q1"]["audio"] == {}  # nada foi gravado


@pytest.mark.parametrize("bruto,esperado", [("médio", "medio"), ("MÁXIMO", "maximo"), (" Suave ", "suave"), ("desligado", "off"), ("não", "off")])
def test_apelidos_e_maiusculas_sao_aceitos(bc, bruto, esperado):
    assert broadcast.set_audio("q1", corte_agudos=bruto)[0]["corte_agudos"] == esperado


def test_estado_gravado_estragado_cai_no_padrao(bc):
    (bc / "state.json").write_text(json.dumps({"q1": {"audio": {"corte_agudos": "ultra", "reducao_ruido": 7}}, "q2": {"audio": "x"}}))
    assert broadcast.audio_view("q1")["corte_agudos"] == "medio" and broadcast.audio_view("q2")["reducao_ruido"] == "medio"


def test_iniciar_um_jogo_novo_nao_zera_o_ajuste_de_audio(bc):
    broadcast.set_audio("q1", corte_agudos="forte")
    broadcast.start("q1", CAMERA, reset=True)
    assert broadcast.snapshot("q1")["audio"]["corte_agudos"] == "forte"


# --- ao subir: usa os ajustes gravados ------------------------------------------------------------

def test_ao_iniciar_passa_ao_ffmpeg_so_o_que_difere_do_padrao(bc):
    broadcast.start("q1", CAMERA)
    assert broadcast.launches[-1] is None                      # padrão: chamada idêntica à de antes
    broadcast.stop("q1")
    broadcast.set_audio("q1", corte_agudos="forte")
    broadcast.start("q1", CAMERA)
    assert broadcast.launches[-1] == {"reducao_ruido": "medio", "corte_agudos": "forte"}
    assert broadcast.audio_view("q1")["aplicado_ao_vivo"] is True   # subiu já com a cadeia dos ajustes gravados


# --- no ar: troca ao vivo, com confirmação --------------------------------------------------------------

def test_no_ar_manda_so_o_delta_e_confirma(bc):
    broadcast.start("q1", CAMERA)
    view, changed = broadcast.set_audio("q1", corte_agudos="forte")
    assert changed and view["aplicado_ao_vivo"] is True
    assert _wait(lambda: _stdin(bc)[-2:] == ["clowpass@lp2 -1 frequency 4000", "clowpass@lp2 -1 mix 1"])
    antes = len(_stdin(bc))
    view, changed = broadcast.set_audio("q1", corte_agudos="forte")     # repetir não manda nada
    assert changed is False and len(_stdin(bc)) == antes and view["aplicado_ao_vivo"] is True


def test_no_ar_corte_e_ruido_juntos(bc):
    broadcast.start("q1", CAMERA)
    broadcast.set_audio("q1", reducao_ruido="off", corte_agudos="off")
    assert _wait(lambda: _stdin(bc)[-3:] == ["clowpass@lp1 -1 mix 0", "cafftdn@dn -1 om i", "chighpass@hp -1 mix 0"])
    assert broadcast.audio_view("q1")["aplicado_ao_vivo"] is True


def test_se_o_ffmpeg_recusa_fica_gravado_e_diz_que_nao_aplicou_e_repetir_tenta_de_novo(bc):
    broadcast.start("q1", CAMERA)
    (bc / "q1.fail").write_text("")                                     # ffmpeg que subiu com a cadeia antiga: ret:-38
    view, _ = broadcast.set_audio("q1", corte_agudos="suave")
    assert view["aplicado_ao_vivo"] is False and view["corte_agudos"] == "suave"
    assert broadcast._state["q1"]["audio"]["corte_agudos"] == "suave"   # vale ao relançar
    (bc / "q1.fail").unlink()
    view, changed = broadcast.set_audio("q1", corte_agudos="suave")     # mesmo valor, mas a última tentativa falhou
    assert changed is False and view["aplicado_ao_vivo"] is True


def test_se_o_ffmpeg_nao_confirma_nada_nao_afirma_que_aplicou(bc):
    broadcast.start("q1", CAMERA)
    (bc / "q1.silent").write_text("")
    t0 = time.time()
    view, _ = broadcast.set_audio("q1", corte_agudos="maximo")
    assert view["aplicado_ao_vivo"] is False
    assert time.time() - t0 < 3.0                                       # espera limitada, não trava a API


def test_sem_ffmpeg_lendo_comandos_nao_aplica(bc):
    broadcast.start("q1", CAMERA)
    broadcast._fifo("q1").unlink()                                      # ninguém lendo
    view, _ = broadcast.set_audio("q1", corte_agudos="forte")
    assert view["aplicado_ao_vivo"] is False and view["corte_agudos"] == "forte"


def test_reiniciar_a_api_readota_e_sincroniza_os_ajustes_gravados(bc, monkeypatch):
    broadcast.start("q1", CAMERA)
    broadcast.set_audio("q1", corte_agudos="forte")
    pid = broadcast._state["q1"]["pid"]
    broadcast.detach()
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_audio_live", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    broadcast.resume({"q1": CAMERA})
    assert broadcast._state["q1"]["pid"] == pid                         # readotado, não relançado
    assert broadcast.audio_view("q1")["aplicado_ao_vivo"] is True
    # o ffmpeg readotado recebeu a configuração completa (não sabemos com que ajustes ele subiu)
    assert _wait(lambda: "clowpass@lp2 -1 mix 1" in _stdin(bc)[3:] and "cafftdn@dn -1 om o" in _stdin(bc)[3:])


def test_ffmpeg_readotado_com_cadeia_antiga_fica_marcado_como_nao_aplicado(bc, monkeypatch, capsys):
    broadcast.start("q1", CAMERA)
    pid = broadcast._state["q1"]["pid"]
    broadcast.detach()
    (bc / "q1.fail").write_text("")                                     # a cadeia antiga não tem os filtros nomeados
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_audio_live", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    broadcast.resume({"q1": CAMERA})
    assert broadcast._state["q1"]["pid"] == pid
    assert broadcast.audio_view("q1")["aplicado_ao_vivo"] is False
    assert "valem quando a transmissão for relançada" in capsys.readouterr().out
