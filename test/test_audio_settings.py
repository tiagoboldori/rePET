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
def lines():  # lê byte a byte, como o ffmpeg: o que não foi lido continua no FIFO (o que _fifo_queued mede)
    buf = b""
    while True:
        ch = os.read(0, 1)
        if not ch:
            return
        buf += ch
        if ch == b"\\n":
            yield buf.decode()
            buf = b""

for line in lines():
    log.write(line)
    if line.strip() == "q":
        break
    if line.startswith("c"):  # comando de filtro: responde como o ffmpeg de verdade
        if os.path.exists({silent!r}):
            continue
        if os.path.exists({delay!r}):  # simula o -stats_period: o ffmpeg lê um comando por período
            time.sleep(float(open({delay!r}).read()))
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
    monkeypatch.setattr(broadcast, "AUDIO_APPLY_MARGIN", 0.0)
    monkeypatch.setattr(broadcast, "STATS_PERIOD_SECONDS", 0.2)       # o ffmpeg falso "lê" um comando a cada 0,2 s (mais o atraso do teste)
    monkeypatch.setattr(broadcast, "AUDIO_RECONCILE_FACTOR", 1.0)
    monkeypatch.setattr(broadcast, "AUDIO_RECONCILE_BASE", 2.0)
    monkeypatch.setattr(broadcast, "_audio_gen", {})
    monkeypatch.setattr(broadcast, "_channels", {})
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
            delay=str(tmp_path / f"{quadra_id}.delay"),
        ))
        script.chmod(0o755)
        return [str(script), "-progress", str(broadcast._progress(quadra_id)), "-stats_period", "0.2"]

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
    assert v["no_ar"] is False and v["aplicado_ao_vivo"] is None and v["confirmacao_pendente"] is False
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


def test_se_o_ffmpeg_nao_confirma_nada_nao_afirma_que_aplicou_e_depois_marca_nao_confirmado(bc, monkeypatch):
    monkeypatch.setattr(broadcast, "AUDIO_RECONCILE_FACTOR", 0.0)
    monkeypatch.setattr(broadcast, "AUDIO_RECONCILE_BASE", 0.4)
    broadcast.start("q1", CAMERA)
    (bc / "q1.silent").write_text("")
    t0 = time.time()
    view, _ = broadcast.set_audio("q1", corte_agudos="maximo")
    assert view["aplicado_ao_vivo"] is False and view["confirmacao_pendente"] is True  # enviado, resposta ainda não veio
    assert time.time() - t0 < 4.0                                       # espera limitada, não trava a API
    assert _wait(lambda: broadcast.audio_view("q1")["confirmacao_pendente"] is False, 8)
    assert broadcast.audio_view("q1")["aplicado_ao_vivo"] is False      # nunca confirmou: segue "não confirmou"


def test_resposta_tardia_vira_confirmada_em_segundo_plano(bc):
    """O ffmpeg de produção lê um comando por -stats_period (5 s nas transmissões antigas): a resposta passa do prazo da
    requisição, mas chega, e a API tem de passar de 'pendente' para confirmado sozinha."""
    broadcast.start("q1", CAMERA)
    (bc / "q1.delay").write_text("0.6")                                  # 0,6 s por comando; 2 comandos (1,2 s) > prazo (0,6 + 2*0,2)
    t0 = time.time()
    view, changed = broadcast.set_audio("q1", corte_agudos="forte")
    assert changed and view["aplicado_ao_vivo"] is False and view["confirmacao_pendente"] is True
    assert time.time() - t0 < 2.0
    assert _wait(lambda: broadcast.audio_view("q1")["aplicado_ao_vivo"] is True, 8), "a confirmação tardia tem de chegar"
    assert broadcast.audio_view("q1")["confirmacao_pendente"] is False
    antes = len(_stdin(bc))
    broadcast.set_audio("q1", corte_agudos="forte")                      # já confirmado: repetir não manda nada
    assert len(_stdin(bc)) == antes


def test_recusa_para_a_espera_na_hora(bc):
    broadcast.start("q1", CAMERA)
    (bc / "q1.fail").write_text("")
    t0 = time.time()
    view, _ = broadcast.set_audio("q1", corte_agudos="suave")
    assert view["aplicado_ao_vivo"] is False and view["confirmacao_pendente"] is False
    assert time.time() - t0 < 1.5, "a primeira recusa (ret:-38) encerra a espera, sem esperar o prazo todo"


def test_o_comando_do_ffmpeg_usa_stats_period_de_1_segundo():
    cmd = broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100)
    assert cmd[cmd.index("-stats_period") + 1] == "1"
    assert broadcast.STATS_PERIOD_SECONDS == 1.0 and broadcast.LEGACY_STATS_PERIOD_SECONDS == 5.0


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
    assert broadcast.audio_view("q1")["confirmacao_pendente"] is True  # a sincronização roda em segundo plano
    assert _wait(lambda: broadcast.audio_view("q1")["aplicado_ao_vivo"] is True)
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
    assert _wait(lambda: broadcast.audio_view("q1")["confirmacao_pendente"] is False)
    assert broadcast.audio_view("q1")["aplicado_ao_vivo"] is False
    saida = []

    def viu_a_mensagem():
        saida.append(capsys.readouterr().out)
        return "valem quando a transmissão for relançada" in "".join(saida)

    assert _wait(viu_a_mensagem), "a API tem de avisar no log que o ffmpeg readotado não aceitou os ajustes"


# --- fila: várias trocas seguidas, duas abas, volume na mesma fila -----------------------------------

def _chain_state_from_log(bc) -> dict:
    """Aplica, na ordem, os comandos que o ffmpeg falso recebeu a uma cadeia desligada: o estado em que o som termina."""
    state = {("hp", "mix"): "0", ("dn", "om"): "i", ("lp1", "mix"): "0", ("lp2", "mix"): "0"}
    apelido = {"frequency": "f", "mix": "mix", "om": "om", "nr": "nr", "nf": "nf"}
    for line in _stdin(bc):
        if not line.startswith("c") or "volume@vol" in line:
            continue
        alvo, _, param, valor = line[1:].split()
        state[(alvo.split("@")[1], apelido[param])] = valor
    return state


def _esperado(settings: dict) -> dict:
    cadeia = broadcast._audio_chain(settings)
    st = {}
    for el in cadeia.split(","):
        m = __import__("re").match(r"(\w+)@(\w+)=(.*)", el)
        if m:
            for kv in m.group(3).split(":"):
                k, v = kv.split("=")
                st[(m.group(2), k)] = v
    ef = {("hp", "mix"): st[("hp", "mix")], ("dn", "om"): st[("dn", "om")], ("lp1", "mix"): st[("lp1", "mix")], ("lp2", "mix"): st[("lp2", "mix")]}
    return ef


def _efetivo(state: dict) -> dict:
    return {k: state[k] for k in (("hp", "mix"), ("dn", "om"), ("lp1", "mix"), ("lp2", "mix"))}


def test_varias_trocas_seguidas_so_mandam_delta_e_todas_acabam_confirmadas(bc):
    """O caso real: o operador clica várias vezes enquanto o ffmpeg ainda lê comandos devagar. Cada troca manda o MÍNIMO
    (delta, mesmo com a anterior pendente) e a confirmação espera a FILA, não só os comandos dela."""
    broadcast.start("q1", CAMERA)
    (bc / "q1.delay").write_text("0.25")
    base = len(_stdin(bc))
    for corte in ("forte", "suave", "maximo", "medio"):
        broadcast.set_audio("q1", corte_agudos=corte)
    enviados = len(_stdin(bc)) - base
    cadeia, esperado = {"reducao_ruido": "medio", "corte_agudos": "medio"}, 0
    for corte in ("forte", "suave", "maximo", "medio"):
        novo = {"reducao_ruido": "medio", "corte_agudos": corte}
        esperado += len(broadcast._audio_commands(novo, cadeia))
        cadeia = novo
    assert enviados == esperado, f"só o delta de cada troca ({esperado} comandos), mandou {enviados}"
    assert _wait(lambda: broadcast.audio_view("q1")["aplicado_ao_vivo"] is True, 10), "tudo tem de terminar confirmado"
    assert broadcast.audio_view("q1")["confirmacao_pendente"] is False
    assert broadcast.audio_view("q1")["corte_agudos"] == "medio"


def test_a_confirmacao_conta_a_fila_inclusive_o_volume(bc):
    """Um comando de volume na frente atrasa a confirmação do áudio: a conta tem de incluí-lo (antes, não incluía)."""
    broadcast.start("q1", CAMERA)
    (bc / "q1.delay").write_text("0.4")
    broadcast.set_volume("q1", 80)                                       # 1 comando na fila, 0,4 s
    view, _ = broadcast.set_audio("q1", corte_agudos="forte")            # +2 comandos: só termina em ~1,2 s
    assert view["aplicado_ao_vivo"] is False and view["confirmacao_pendente"] is True, "ainda não pode afirmar que aplicou"
    assert _wait(lambda: broadcast.audio_view("q1")["aplicado_ao_vivo"] is True, 8)
    assert _stdin(bc)[-3:][0] == "cvolume@vol -1 volume 0.800"


def test_duas_abas_ao_mesmo_tempo_deixam_o_ffmpeg_exatamente_no_ultimo_ajuste_gravado(bc):
    """Duas sessões mandando ajustes ao mesmo tempo: a ordem na fila tem de ser a ordem dos ajustes gravados."""
    import random
    import threading as th
    broadcast.start("q1", CAMERA)
    rnd = random.Random(7)
    ops = [{"reducao_ruido": rnd.choice(broadcast.REDUCAO_RUIDO_NIVEIS), "corte_agudos": rnd.choice(broadcast.CORTE_AGUDOS_NIVEIS)} for _ in range(24)]

    def aba(lote):
        for o in lote:
            broadcast.set_audio("q1", **o)

    ths = [th.Thread(target=aba, args=(ops[0::2],)), th.Thread(target=aba, args=(ops[1::2],))]
    [x.start() for x in ths]
    [x.join(20) for x in ths]
    final = broadcast.audio_view("q1")
    assert _wait(lambda: broadcast.audio_view("q1")["aplicado_ao_vivo"] is True, 15)
    efetivo = _efetivo(_chain_state_from_log(bc))
    assert efetivo == _esperado({"reducao_ruido": final["reducao_ruido"], "corte_agudos": final["corte_agudos"]}), \
        f"o ffmpeg terminou em {efetivo}, mas o ajuste gravado é {final['reducao_ruido']}/{final['corte_agudos']}"


def test_uma_recusa_na_fila_derruba_a_confirmacao_e_o_proximo_put_manda_tudo_de_novo(bc):
    broadcast.start("q1", CAMERA)
    (bc / "q1.fail").write_text("")
    view, _ = broadcast.set_audio("q1", corte_agudos="suave")
    assert view["aplicado_ao_vivo"] is False and view["confirmacao_pendente"] is False
    (bc / "q1.fail").unlink()
    antes = len(_stdin(bc))
    view, _ = broadcast.set_audio("q1", corte_agudos="suave")           # mesmo valor, mas não estava em dia: conjunto completo
    assert len(_stdin(bc)) - antes == len(broadcast._audio_commands({"reducao_ruido": "medio", "corte_agudos": "suave"})) and view["aplicado_ao_vivo"] is True
    # e a recusa antiga já não conta: o próximo delta fica confirmado
    view, _ = broadcast.set_audio("q1", corte_agudos="forte")
    assert view["aplicado_ao_vivo"] is True


def test_o_periodo_do_ffmpeg_readotado_e_lido_do_processo(bc):
    broadcast.start("q1", CAMERA)
    pid = broadcast._state["q1"]["pid"]
    assert broadcast._proc_stats_period(pid) == 0.2                      # o falso foi lançado com -stats_period 0.2
    import os
    assert broadcast._proc_stats_period(os.getpid()) == broadcast.LEGACY_STATS_PERIOD_SECONDS   # sem a opção: assume o antigo
    assert broadcast._proc_stats_period(None) == broadcast.LEGACY_STATS_PERIOD_SECONDS


def test_a_fila_nao_lida_do_fifo_e_estimada_ao_readotar(bc):
    broadcast.start("q1", CAMERA)
    (bc / "q1.delay").write_text("1.0")                                   # o falso consome devagar
    broadcast._write_commands("q1", ["afftdn@dn -1 nr 14"] * 4)
    time.sleep(0.2)
    assert broadcast._fifo_queued("q1") >= 2
