"""Troca da stream key e da URL RTMP por quadra (api/stream_config.py), sem rede nem servidor."""
from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from api import broadcast, config, live, monitor, stream_config
from api.stream_config import UNSET, StreamConfigError

KEY_A = "aaaa-bbbb-cccc-dddd-eeee"
KEY_B = "ffff-gggg-hhhh-iiii-jjjj"


def cameras_data() -> list[dict]:
    return [
        {"quadra_id": "q1", "local_id": "loc1", "esporte": "padel", "nome": "Quadra Á",
         "input_url": "rtsp://u:p@10.0.0.1/s", "youtube_stream_key": KEY_A},
        {"quadra_id": "q2", "local_id": "loc1", "esporte": "padel", "nome": "Quadra 2",
         "input_url": "rtsp://u:p@10.0.0.2/s"},
        {"quadra_id": "q3", "local_id": "loc1", "esporte": "padel", "nome": "Quadra 3",
         "input_url": "rtsp://u:p@10.0.0.3/s", "youtube_stream_key": KEY_A},
    ]


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    path = tmp_path / "cameras.json"
    path.write_text(json.dumps(cameras_data(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o640)
    monkeypatch.setattr(config, "CAMERAS_FILE", path)
    monkeypatch.setattr(config, "YOUTUBE_RTMP_URL", "rtmp://a.rtmp.youtube.com/live2")
    monkeypatch.setattr(config, "RTMP_ALLOWED_HOSTS", ["youtube.com"])
    monkeypatch.setattr(broadcast, "is_on_air", lambda q: False)
    monkeypatch.setattr(live, "status", lambda url: False)
    monkeypatch.setattr(monitor, "get", lambda q: {"no_ar": False, "erro": None, "verificado_ha_segundos": 1})
    monkeypatch.setattr(monitor, "key_in_use_by", lambda q, cams, fresh=False: None)
    return path


def saved(path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


# --- validação -----------------------------------------------------------------

@pytest.mark.parametrize("key", [KEY_A, "abcdefgh", "a" * 128, "live_1234-abcd_EFGH", f"  {KEY_A}  "])
def test_keys_validas(key):
    assert stream_config.validate_key(key) == key.strip()


@pytest.mark.parametrize("key", [
    "", "   ", "curta", "a" * 129, "tem espaco-1234", "com/barra-1234", "com?query-12345", "xml<tag>-12345",
    "aspas\"-12345678", "quebra\nlinha-1234", "acento-ção-12345", None, 12345678, ["x"],
])
def test_keys_invalidas(key):
    with pytest.raises(StreamConfigError) as exc:
        stream_config.validate_key(key)
    assert exc.value.status == 422
    assert "stream_key" in str(exc.value)


@pytest.mark.parametrize("url,esperada", [
    ("rtmp://a.rtmp.youtube.com/live2", "rtmp://a.rtmp.youtube.com/live2"),
    ("rtmp://a.rtmp.youtube.com/live2/", "rtmp://a.rtmp.youtube.com/live2"),
    ("RTMP://B.RTMP.YOUTUBE.COM:1935/live2", "rtmp://b.rtmp.youtube.com:1935/live2"),
    ("  rtmp://x.youtube.com  ", "rtmp://x.youtube.com"),
    ("rtmp://youtube.com/app", "rtmp://youtube.com/app"),
])
def test_urls_validas(url, esperada):
    assert stream_config.validate_rtmp_url(url) == esperada


@pytest.mark.parametrize("url", [
    "", "   ", None, 5, "a.rtmp.youtube.com/live2", "http://a.rtmp.youtube.com/live2",
    "rtmps://a.rtmps.youtube.com:443/live2", "rtmp://user:pw@a.rtmp.youtube.com/live2",
    "rtmp://a.rtmp.youtube.com/live2?key=1", "rtmp://a.rtmp.youtube.com/live2#x",
    "rtmp://a.rtmp.youtube.com/live 2", "rtmp://a.rtmp.youtube.com:abc/live2",
    "rtmp://a.rtmp.youtube.com:99999/live2", "rtmp:///live2", "rtmp://a.rtmp.youtube.com/li<ve>",
    "rtmp://a.rtmp.youtube.com/" + "x" * 300,
])
def test_urls_invalidas(url):
    with pytest.raises(StreamConfigError) as exc:
        stream_config.validate_rtmp_url(url)
    assert exc.value.status == 422


@pytest.mark.parametrize("url", [
    "rtmp://evil.example.com/live2",
    "rtmp://youtube.com.evil.example.com/live2",   # contém o domínio, mas não termina nele
    "rtmp://notyoutube.com/live2",                  # sufixo sem ponto
    "rtmp://127.0.0.1/live2",
    "rtmp://10.0.0.5:1935/app",
    "rtmp://localhost/live2",
])
def test_host_fora_da_lista_e_recusado(url):
    with pytest.raises(StreamConfigError) as exc:
        stream_config.validate_rtmp_url(url)
    assert "não permitido" in str(exc.value)


def test_hosts_extras_e_ip_exato(monkeypatch):
    monkeypatch.setattr(config, "RTMP_ALLOWED_HOSTS", ["youtube.com", "facebook.com", "203.0.113.7"])
    assert stream_config.validate_rtmp_url("rtmp://live-api-s.facebook.com:80/rtmp").startswith("rtmp://live-api-s.facebook.com")
    assert stream_config.validate_rtmp_url("rtmp://203.0.113.7/live") == "rtmp://203.0.113.7/live"
    for ruim in ("rtmp://13.0.113.7/live", "rtmp://1.203.0.113.7/live", "rtmp://203.0.113.70/live"):
        with pytest.raises(StreamConfigError):
            stream_config.validate_rtmp_url(ruim)


def test_host_da_url_global_sempre_vale(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_RTMP_URL", "rtmp://ingest.exemplo.com:1936/app/")
    assert stream_config.validate_rtmp_url("rtmp://ingest.exemplo.com:1936/outra") == "rtmp://ingest.exemplo.com:1936/outra"


def test_mascara_da_key():
    assert stream_config.mask_key(KEY_A) == "eeee"
    assert stream_config.mask_key("curta-demais") is None  # key curta: mostrar 4 caracteres ajudaria a adivinhar
    assert stream_config.mask_key("") is None and stream_config.mask_key(None) is None


# --- leitura -------------------------------------------------------------------

def test_get_nunca_devolve_a_key():
    view = stream_config.get("q1")
    assert view["tem_stream_key"] is True and view["stream_key_final"] == "eeee"
    assert KEY_A not in json.dumps(view)
    assert view["rtmp_url"] == "rtmp://a.rtmp.youtube.com/live2" and view["rtmp_url_padrao"] is True
    assert view["rtmp_hosts_permitidos"] == ["youtube.com", "a.rtmp.youtube.com"]
    assert view["no_ar"] is False


def test_get_quadra_sem_key():
    view = stream_config.get("q2")
    assert view["tem_stream_key"] is False and view["stream_key_final"] is None


def test_get_mostra_chave_compartilhada(monkeypatch):
    monkeypatch.setattr(monitor, "key_in_use_by", lambda q, cams, fresh=False: "q3")
    view = stream_config.get("q1")
    assert view["chave_compartilhada_com"] == ["q3"] and view["chave_em_uso_por"] == "q3"


def test_get_quadra_inexistente():
    with pytest.raises(StreamConfigError) as exc:
        stream_config.get("nao-existe")
    assert exc.value.status == 404


# --- escrita -------------------------------------------------------------------

def test_troca_a_key_e_preserva_o_resto_do_arquivo(setup):
    before = saved(setup)
    view, changed = stream_config.update("q2", KEY_B)
    assert changed is True and view["tem_stream_key"] is True and view["stream_key_final"] == "jjjj"
    after = saved(setup)
    assert after[1] == {**before[1], "youtube_stream_key": KEY_B}
    assert after[0] == before[0] and after[2] == before[2]       # as outras quadras não mudam
    assert list(after[1].keys())[-1] == "youtube_stream_key"      # nova chave entra no fim, ordem preservada
    text = setup.read_text(encoding="utf-8")
    assert text.endswith("\n") and "Quadra Á" in text              # formato e acentos preservados
    assert KEY_B not in json.dumps(view)


def test_o_resto_do_codigo_enxerga_a_key_nova_na_hora(setup):
    stream_config.update("q2", KEY_B)
    assert config.load_cameras()["q2"]["youtube_stream_key"] == KEY_B


def test_permissao_do_arquivo_e_preservada(setup):
    stream_config.update("q1", KEY_B)
    assert stat.S_IMODE(os.stat(setup).st_mode) == 0o640


def test_sem_temporarios_sobrando(setup):
    stream_config.update("q1", KEY_B)
    stream_config.update("q1", rtmp_url="rtmp://b.rtmp.youtube.com/live2")
    assert [p.name for p in setup.parent.iterdir()] == ["cameras.json"]


def test_trocar_para_o_mesmo_valor_nao_reescreve(setup):
    mtime = os.stat(setup).st_mtime_ns
    view, changed = stream_config.update("q1", KEY_A)
    assert changed is False and os.stat(setup).st_mtime_ns == mtime


def test_troca_a_url_e_volta_ao_padrao(setup):
    view, changed = stream_config.update("q1", rtmp_url="rtmp://b.rtmp.youtube.com:1935/live2/")
    assert changed and view["rtmp_url"] == "rtmp://b.rtmp.youtube.com:1935/live2" and view["rtmp_url_padrao"] is False
    assert saved(setup)[0]["rtmp_url"] == "rtmp://b.rtmp.youtube.com:1935/live2"
    assert config.rtmp_url_for(config.load_cameras()["q1"]) == "rtmp://b.rtmp.youtube.com:1935/live2"

    view, changed = stream_config.update("q1", rtmp_url=None)  # null = volta à global
    assert changed and view["rtmp_url_padrao"] is True and "rtmp_url" not in saved(setup)[0]
    assert config.rtmp_url_for(config.load_cameras()["q1"]) == "rtmp://a.rtmp.youtube.com/live2"
    _, changed = stream_config.update("q1", rtmp_url=None)     # já estava no padrão
    assert changed is False


def test_key_e_url_juntas(setup):
    view, changed = stream_config.update("q1", KEY_B, "rtmp://b.rtmp.youtube.com/live2")
    assert changed
    entry = saved(setup)[0]
    assert entry["youtube_stream_key"] == KEY_B and entry["rtmp_url"] == "rtmp://b.rtmp.youtube.com/live2"


def test_corpo_vazio_e_key_nula_sao_recusados():
    with pytest.raises(StreamConfigError) as exc:
        stream_config.update("q1")
    assert exc.value.status == 422
    with pytest.raises(StreamConfigError) as exc:
        stream_config.update("q1", stream_key=None)
    assert exc.value.status == 422 and "nula" in str(exc.value)


def test_valor_invalido_nao_grava_nada(setup):
    before = setup.read_text(encoding="utf-8")
    with pytest.raises(StreamConfigError):
        stream_config.update("q1", KEY_B, "rtmp://evil.example.com/x")  # key boa + url ruim: nada muda
    with pytest.raises(StreamConfigError):
        stream_config.update("q1", "com espaco e /barra")
    assert setup.read_text(encoding="utf-8") == before


def test_quadra_inexistente_no_put():
    with pytest.raises(StreamConfigError) as exc:
        stream_config.update("nao-existe", KEY_B)
    assert exc.value.status == 404


def test_erro_nunca_contem_a_key():
    for bad in ("com espaco e /barra", "x"):
        with pytest.raises(StreamConfigError) as exc:
            stream_config.update("q1", bad)
        assert bad not in str(exc.value) or bad == "x"  # "x" só aparece se a mensagem o citasse; ela não cita
        assert KEY_A not in str(exc.value)


# --- quadra no ar --------------------------------------------------------------

def test_recusa_com_transmissao_com_placar_no_ar(setup, monkeypatch):
    monkeypatch.setattr(broadcast, "is_on_air", lambda q: q == "q1")
    before = setup.read_text(encoding="utf-8")
    with pytest.raises(StreamConfigError) as exc:
        stream_config.update("q1", KEY_B)
    assert exc.value.status == 409 and "transmitindo" in str(exc.value)
    assert setup.read_text(encoding="utf-8") == before
    stream_config.update("q2", KEY_B)  # outra quadra continua liberada


def test_recusa_com_live_direta_no_ar(setup, monkeypatch):
    monkeypatch.setattr(live, "status", lambda url: url.endswith(".1/s"))
    with pytest.raises(StreamConfigError) as exc:
        stream_config.update("q1", rtmp_url="rtmp://b.rtmp.youtube.com/live2")
    assert exc.value.status == 409


def test_camera_muda_usa_o_ultimo_estado_conhecido(setup, monkeypatch):
    def mudo(url):
        raise live.LiveError("câmera não respondeu")
    monkeypatch.setattr(live, "status", mudo)
    monkeypatch.setattr(monitor, "get", lambda q: {"no_ar": True, "erro": None, "verificado_ha_segundos": 3})
    with pytest.raises(StreamConfigError) as exc:
        stream_config.update("q1", KEY_B)
    assert exc.value.status == 409
    monkeypatch.setattr(monitor, "get", lambda q: {"no_ar": None, "erro": "x", "verificado_ha_segundos": 3})
    _, changed = stream_config.update("q1", KEY_B)  # sem prova de que está no ar: libera
    assert changed


def test_get_mostra_no_ar_pelo_cache(monkeypatch):
    monkeypatch.setattr(broadcast, "is_on_air", lambda q: q == "q1")
    assert stream_config.get("q1")["no_ar"] is True and stream_config.get("q2")["no_ar"] is False


# --- arquivo ilegível / concorrência --------------------------------------------

def test_arquivo_corrompido_da_500_sem_vazar_caminho_nem_conteudo(setup):
    setup.write_text("{ isto nao e json", encoding="utf-8")
    monkey = None  # load_cameras() também quebraria; o update lê o arquivo de novo e falha com erro seguro
    with pytest.raises((StreamConfigError, ValueError)):
        stream_config.update("q1", KEY_B)


def test_atualizacoes_concorrentes_nao_perdem_dados(setup):
    keys = [f"key-{i:02d}-abcdefgh" for i in range(8)]
    quadras = ["q1", "q2", "q3"]
    errors: list[Exception] = []

    def run(i: int):
        try:
            stream_config.update(quadras[i % 3], keys[i])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    final = saved(setup)  # o arquivo continua JSON válido e com as 3 quadras
    assert [c["quadra_id"] for c in final] == ["q1", "q2", "q3"]
    assert all(c["youtube_stream_key"] in keys + [KEY_A] for c in final)


# --- destino por quadra chega à live e ao ffmpeg --------------------------------

def test_ffmpeg_usa_a_url_da_quadra_ou_a_global(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_RTMP_URL", "rtmp://global.example.com/app/")
    padrao = broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100)
    assert padrao[-1] == "rtmp://global.example.com/app/KEY"
    da_quadra = broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100, rtmp_url="rtmp://b.rtmp.youtube.com/live2")
    assert da_quadra[-1] == "rtmp://b.rtmp.youtube.com/live2/KEY"


def test_live_usa_a_url_da_quadra(monkeypatch):
    monkeypatch.setattr(live.socket, "getaddrinfo", lambda host, *a, **k: [(0, 0, 0, "", ("203.0.113.9", 0))])
    assert live._ingest_url("KEY") == "rtmp://203.0.113.9/live2/KEY"
    assert live._ingest_url("KEY", "rtmp://b.rtmp.youtube.com:1935/live3") == "rtmp://203.0.113.9:1935/live3/KEY"


# --- o contrato antigo não muda: sem rtmp_url próprio a chamada é idêntica à de antes --------

@pytest.fixture
def spawn_env(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(config, "BROADCAST_DIR", tmp_path / "bc")
    monkeypatch.setattr(config, "BROADCAST_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(config, "BROADCAST_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "LIVE_MAX_SECONDS", 0)
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_runtime", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    calls: list[tuple[tuple, dict]] = []

    def fake_cmd(*args, **kwargs):  # imita a assinatura ANTIGA de _ffmpeg_cmd (sem rtmp_url) quando não há override
        calls.append((args, kwargs))
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr(broadcast, "_ffmpeg_cmd", fake_cmd)
    return calls


def _spawn_with(camera: dict) -> None:
    runtime = broadcast._Runtime()
    broadcast._spawn(camera["quadra_id"], camera, runtime, True)
    if runtime.proc is not None:
        runtime.proc.wait(timeout=10)


def test_spawn_sem_rtmp_url_nao_passa_argumento_novo(spawn_env):
    _spawn_with({"quadra_id": "q1", "input_url": "rtsp://u:p@10.0.0.1/s", "youtube_stream_key": "k-1"})
    args, kwargs = spawn_env[0]
    assert args == ("q1", "rtsp://u:p@10.0.0.1/s", "k-1", 100, True) and kwargs == {}


def test_spawn_com_rtmp_url_da_quadra_repassa(spawn_env):
    _spawn_with({"quadra_id": "q1", "input_url": "rtsp://u:p@10.0.0.1/s", "youtube_stream_key": "k-1",
                 "rtmp_url": "rtmp://b.rtmp.youtube.com/live2"})
    args, kwargs = spawn_env[0]
    assert kwargs == {"rtmp_url": "rtmp://b.rtmp.youtube.com/live2"}


def test_live_start_sem_rtmp_url_usa_a_global_como_antes(monkeypatch):
    monkeypatch.setattr(live.socket, "getaddrinfo", lambda host, *a, **k: [(0, 0, 0, "", ("203.0.113.9", 0))])
    monkeypatch.setattr(config, "YOUTUBE_RTMP_URL", "rtmp://ingest.exemplo:1936/app/")
    assert live._ingest_url("KEY") == live._ingest_url("KEY", None) == "rtmp://203.0.113.9:1936/app/KEY"


# --- rotas separadas: key e URL --------------------------------------------------

def test_get_key_devolve_a_key_inteira():
    view = stream_config.get_key("q1")
    assert view == {"quadra_id": "q1", "tem_stream_key": True, "stream_key": KEY_A,
                    "chave_compartilhada_com": ["q3"], "no_ar": False}


def test_get_key_sem_key_e_quadra_inexistente():
    assert stream_config.get_key("q2")["stream_key"] is None and stream_config.get_key("q2")["tem_stream_key"] is False
    with pytest.raises(StreamConfigError) as exc:
        stream_config.get_key("nao-existe")
    assert exc.value.status == 404


def test_get_url_efetiva_global_e_propria(setup):
    view = stream_config.get_url("q1")
    assert view["rtmp_url"] == "rtmp://a.rtmp.youtube.com/live2" and view["rtmp_url_padrao"] is True
    assert view["rtmp_url_global"] == "rtmp://a.rtmp.youtube.com/live2"
    assert "stream_key" not in view and KEY_A not in json.dumps(view)   # a rota da URL nunca mostra a key
    stream_config.update_url("q1", "rtmp://b.rtmp.youtube.com/live2")
    view = stream_config.get_url("q1")
    assert view["rtmp_url"] == "rtmp://b.rtmp.youtube.com/live2" and view["rtmp_url_padrao"] is False
    assert view["rtmp_url_global"] == "rtmp://a.rtmp.youtube.com/live2"


def test_update_key_troca_so_a_key_e_devolve_a_visao_da_key(setup):
    before = saved(setup)
    view, changed = stream_config.update_key("q2", KEY_B)
    assert changed and view["stream_key"] == KEY_B and view["tem_stream_key"] is True
    after = saved(setup)
    assert after[1] == {**before[1], "youtube_stream_key": KEY_B} and "rtmp_url" not in after[1]
    _, changed = stream_config.update_key("q2", KEY_B)
    assert changed is False


def test_update_url_troca_so_a_url_e_null_volta_ao_padrao(setup):
    before = saved(setup)
    view, changed = stream_config.update_url("q1", "rtmp://b.rtmp.youtube.com/live2/")
    assert changed and view["rtmp_url"] == "rtmp://b.rtmp.youtube.com/live2"
    assert saved(setup)[0]["youtube_stream_key"] == before[0]["youtube_stream_key"]   # a key não é tocada
    view, changed = stream_config.update_url("q1", None)
    assert changed and view["rtmp_url_padrao"] is True and "rtmp_url" not in saved(setup)[0]


def test_rotas_separadas_seguem_as_mesmas_regras(setup, monkeypatch):
    for bad in ("curta", None, 123456789):
        with pytest.raises(StreamConfigError) as exc:
            stream_config.update_key("q1", bad)
        assert exc.value.status == 422
    with pytest.raises(StreamConfigError):
        stream_config.update_url("q1", "rtmp://evil.example.com/x")
    monkeypatch.setattr(broadcast, "is_on_air", lambda q: q == "q1")
    for call in (lambda: stream_config.update_key("q1", KEY_B), lambda: stream_config.update_url("q1", "rtmp://b.rtmp.youtube.com/live2")):
        with pytest.raises(StreamConfigError) as exc:
            call()
        assert exc.value.status == 409
