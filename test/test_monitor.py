"""Verificação das lives diretas na subida (câmeras e ISAPI falsos)."""
from __future__ import annotations

import pytest

from api import broadcast, config, live, monitor

CAMS = {
    "q1": {"quadra_id": "q1", "input_url": "rtsp://u:p@10.0.0.1/s"},
    "q2": {"quadra_id": "q2", "input_url": "rtsp://u:p@10.0.0.2/s"},
    "q3": {"quadra_id": "q3", "input_url": "rtsp://u:p@10.0.0.3/s"},
    "dev": {"quadra_id": "dev", "input_url": "lavfi:testsrc"},  # fonte de desenvolvimento: não tem ISAPI
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIVE_STATE_FILE", tmp_path / "live_state.json")
    monkeypatch.setattr(config, "LIVE_MAX_SECONDS", 7200)
    monkeypatch.setattr(config, "BROADCAST_STATE_FILE", tmp_path / "bstate.json")
    monkeypatch.setattr(config, "BROADCAST_DIR", tmp_path / "bc")
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    monkeypatch.setattr(monitor, "_cache", {})
    camera_state: dict[str, object] = {}  # quadra -> True | False | LiveError

    def fake_status(input_url):
        value = camera_state[input_url.split("@")[1].split("/")[0]]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(live, "status", fake_status)
    monkeypatch.setattr(live, "_arm", lambda *a, **k: None)  # sem timers de verdade
    yield camera_state
    live._timers.clear()


def test_live_direta_sem_prazo_ganha_prazo_na_subida(env):
    env.update({"10.0.0.1": True, "10.0.0.2": False, "10.0.0.3": False})
    summary = monitor.run_once(CAMS, first=True)
    assert summary["direta"] == ["q1"] and summary["adotadas"] == ["q1"]
    assert "q1" in live._load_deadlines()
    assert monitor.get("q1")["no_ar"] is True and monitor.get("q2")["no_ar"] is False


def test_live_direta_com_prazo_nao_e_readotada_de_novo(env):
    env.update({"10.0.0.1": True, "10.0.0.2": False, "10.0.0.3": False})
    live._save_deadlines({"q1": 123.0})
    assert monitor.run_once(CAMS, first=True)["adotadas"] == []
    assert live._load_deadlines()["q1"] == 123.0


def test_prazo_de_live_que_nao_esta_mais_no_ar_e_esquecido(env):
    env.update({"10.0.0.1": False, "10.0.0.2": False, "10.0.0.3": False})
    live._save_deadlines({"q2": 123.0})
    assert monitor.run_once(CAMS, first=True)["prazos_obsoletos"] == ["q2"]
    assert live._load_deadlines() == {}


def test_so_a_primeira_rodada_reconcilia(env):
    env.update({"10.0.0.1": True, "10.0.0.2": False, "10.0.0.3": False})
    assert monitor.run_once(CAMS, first=False)["adotadas"] == []
    assert live._load_deadlines() == {}


def test_camera_que_nao_responde_vira_desconhecido_e_nao_derruba_nada(env):
    env.update({"10.0.0.1": live.LiveError("câmera não respondeu (ConnectTimeout)."), "10.0.0.2": False, "10.0.0.3": False})
    summary = monitor.run_once(CAMS, first=True)
    assert summary["sem_resposta"] == ["q1"]
    got = monitor.get("q1")
    assert got["no_ar"] is None and "não respondeu" in got["erro"]


def test_conflito_entre_placar_e_live_direta_e_apontado(env, monkeypatch):
    env.update({"10.0.0.1": True, "10.0.0.2": False, "10.0.0.3": False})
    monkeypatch.setattr(broadcast, "is_on_air", lambda q: q == "q1")
    summary = monitor.run_once(CAMS, first=True)
    assert summary["conflitos"] == ["q1"] and summary["placar"] == ["q1"]


def test_fonte_sem_rtsp_nao_e_consultada(env):
    env.update({"10.0.0.1": False, "10.0.0.2": False, "10.0.0.3": False})  # 'dev' não está aqui: consultar daria KeyError
    monitor.run_once(CAMS, first=True)
    assert monitor.get("dev")["verificado_ha_segundos"] is None


def test_mark_atualiza_o_cache_na_hora(env):
    monitor.mark("q1", True)
    assert monitor.get("q1")["no_ar"] is True
    monitor.mark("q1", False)
    assert monitor.get("q1")["no_ar"] is False
