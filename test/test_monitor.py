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


# --- stream key compartilhada (intencional): uma live por vez ----------------------

SHARED = {
    "a": {"quadra_id": "a", "input_url": "rtsp://u:p@10.0.0.1/s", "youtube_stream_key": "K"},
    "b": {"quadra_id": "b", "input_url": "rtsp://u:p@10.0.0.2/s", "youtube_stream_key": "K"},
    "c": {"quadra_id": "c", "input_url": "rtsp://u:p@10.0.0.3/s", "youtube_stream_key": "OUTRA"},
    "d": {"quadra_id": "d", "input_url": "rtsp://u:p@10.0.0.4/s"},  # sem key
}


def test_quadras_com_a_mesma_key(env):
    assert monitor.shared_key_with("a", SHARED) == ["b"]
    assert monitor.shared_key_with("c", SHARED) == [] and monitor.shared_key_with("d", SHARED) == []
    assert monitor.shared_key_with("nao-existe", SHARED) == []


def test_key_em_uso_por_transmissao_com_placar(env, monkeypatch):
    monkeypatch.setattr(broadcast, "is_on_air", lambda q: q == "b")
    assert monitor.key_in_use_by("a", SHARED) == "b"
    assert monitor.key_in_use_by("c", SHARED) is None  # key diferente não conflita
    monkeypatch.setattr(broadcast, "is_on_air", lambda q: False)
    assert monitor.key_in_use_by("a", SHARED) is None


def test_key_em_uso_por_live_direta_usa_o_cache_ou_pergunta_a_camera(env, monkeypatch):
    monkeypatch.setattr(broadcast, "is_on_air", lambda q: False)
    monitor.mark("b", True)
    assert monitor.key_in_use_by("a", SHARED) == "b"  # cache
    monitor.mark("b", False)  # cache diz que não, mas a câmera diz que sim (cache velho)
    env["10.0.0.2"] = True
    assert monitor.key_in_use_by("a", SHARED) is None
    assert monitor.key_in_use_by("a", SHARED, fresh=True) == "b"  # ao ligar pergunta na hora
    assert monitor.get("b")["no_ar"] is True  # e atualiza o cache


def test_camera_da_outra_quadra_sem_resposta_nao_bloqueia(env, monkeypatch):
    monkeypatch.setattr(broadcast, "is_on_air", lambda q: False)
    env["10.0.0.2"] = live.LiveError("câmera não respondeu.")
    assert monitor.key_in_use_by("a", SHARED, fresh=True) is None


def test_aviso_de_subida_para_a_mesma_key_no_ar_em_duas_quadras(env, monkeypatch):
    env.update({"10.0.0.1": True, "10.0.0.2": True, "10.0.0.3": False, "10.0.0.4": False})
    monkeypatch.setattr(broadcast, "is_on_air", lambda q: False)
    summary = monitor.run_once(SHARED, first=True)
    assert summary["chave_repetida_no_ar"] == ["a", "b"]
    env.update({"10.0.0.2": False})
    assert monitor.run_once(SHARED, first=False)["chave_repetida_no_ar"] == []
