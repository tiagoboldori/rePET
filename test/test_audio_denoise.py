"""Cadeia de áudio da transmissão com placar: redução de ruído (afftdn) e corte de agudos (lowpass).

Os testes de cadeia são puros (sem ffmpeg). Os funcionais usam o ffmpeg de verdade com áudio sintético, inclusive a
troca dos ajustes COM o ffmpeg rodando, pelos comandos (o mesmo canal do volume).
"""
from __future__ import annotations

import itertools
import math
import re
import shutil
import struct
import subprocess
import threading
import time

import pytest

from api import broadcast, config

PADRAO = {"reducao_ruido": "medio", "corte_agudos": "medio"}
TODOS = [{"reducao_ruido": r, "corte_agudos": c}
         for r, c in itertools.product(broadcast.REDUCAO_RUIDO_NIVEIS, broadcast.CORTE_AGUDOS_NIVEIS)]


@pytest.fixture(autouse=True)
def ffmpeg_tem_o_filtro(monkeypatch):
    """Por padrão, finge que o ffmpeg tem o afftdn (os testes funcionais checam o de verdade)."""
    monkeypatch.setattr(broadcast, "_denoise_filter_ok", True)
    monkeypatch.setattr(config, "AUDIO_DENOISE", "medio")
    monkeypatch.setattr(config, "AUDIO_CORTE_AGUDOS", "medio")


# --- configuração ----------------------------------------------------------------------------

@pytest.mark.parametrize("raw,esperado", [
    (None, "medio"), ("", "medio"), ("   ", "medio"),
    ("medio", "medio"), ("MEDIO", "medio"), ("médio", "medio"), (" Médio ", "medio"),
    ("leve", "leve"), ("forte", "forte"),
    ("off", "off"), ("OFF", "off"), ("0", "off"), ("false", "off"), ("nao", "off"), ("não", "off"), ("desligado", "off"),
])
def test_parse_audio_denoise(raw, esperado):
    assert config.parse_audio_denoise(raw) == esperado


@pytest.mark.parametrize("raw,esperado", [
    (None, "medio"), ("", "medio"), ("medio", "medio"), ("MÉDIO", "medio"),
    ("off", "off"), ("não", "off"), ("desligado", "off"),
    ("suave", "suave"), ("forte", "forte"), ("maximo", "maximo"), ("máximo", "maximo"),
])
def test_parse_audio_corte_agudos(raw, esperado):
    assert config.parse_audio_corte_agudos(raw) == esperado


def test_valor_desconhecido_cai_no_padrao_com_aviso_e_nao_levanta(capsys):
    assert config.parse_audio_denoise("ultra") == "medio"
    assert config.parse_audio_corte_agudos("ultra") == "medio"
    saida = capsys.readouterr().out
    assert "AUDIO_DENOISE='ultra' desconhecido" in saida and "AUDIO_CORTE_AGUDOS='ultra' desconhecido" in saida


def test_o_padrao_do_servidor_e_medio_nos_dois():
    assert broadcast.audio_defaults() == PADRAO


# --- cadeia de filtros -----------------------------------------------------------------------

def test_cadeia_padrao_e_a_medida_em_audio_real():
    assert broadcast._audio_chain() == (
        "aresample=16000,highpass@hp=f=100:mix=1,afftdn@dn=nr=14:nf=-45:tn=1:om=o,"
        "lowpass@lp1=f=4000:mix=1,lowpass@lp2=f=7900:mix=0"
    )


def test_cadeia_desligada_continua_la_mas_transparente():
    chain = broadcast._audio_chain({"reducao_ruido": "off", "corte_agudos": "off"})
    assert "highpass@hp=f=100:mix=0" in chain and "om=i" in chain
    assert "lowpass@lp1=f=7900:mix=0" in chain and "lowpass@lp2=f=7900:mix=0" in chain


def _estado_da_cadeia(chain: str) -> dict:
    """Lê os parâmetros efetivos de uma cadeia montada: {(filtro, opção): valor}."""
    out = {}
    for el in chain.split(","):
        m = re.match(r"(\w+)@(\w+)=(.*)", el)
        if m:
            for kv in m.group(3).split(":"):
                k, v = kv.split("=")
                out[(m.group(2), k)] = v
    return out


def _aplicar(estado: dict, comandos: list[str]) -> dict:
    """Aplica comandos do ffmpeg ('alvo -1 param valor') sobre o estado de uma cadeia."""
    estado = dict(estado)
    apelido = {"frequency": "f", "mix": "mix", "om": "om", "nr": "nr", "nf": "nf"}
    for c in comandos:
        alvo, tempo, param, valor = c.split()
        assert tempo == "-1"
        estado[(alvo.split("@")[1], apelido[param])] = valor
    return estado


def _efetivo(estado: dict, s: dict) -> dict:
    """Só o que importa para o som: ignora parâmetros de um filtro que está transparente."""
    ef = {("hp", "mix"): estado[("hp", "mix")], ("dn", "om"): estado[("dn", "om")]}
    if estado[("dn", "om")] == "o":
        ef[("dn", "nr")], ef[("dn", "nf")] = float(estado[("dn", "nr")]), float(estado[("dn", "nf")])
    for lp in ("lp1", "lp2"):
        ef[(lp, "mix")] = estado[(lp, "mix")]
        if estado[(lp, "mix")] == "1":
            ef[(lp, "f")] = float(estado[(lp, "f")])
    return ef


@pytest.mark.parametrize("alvo", TODOS, ids=lambda s: f"{s['reducao_ruido']}-{s['corte_agudos']}")
def test_trocar_ao_vivo_leva_a_cadeia_exatamente_ao_mesmo_estado_de_um_lancamento_novo(alvo):
    """A cadeia montada ao lançar com os ajustes X e a cadeia que recebeu os comandos para X têm de soar igual, partindo
    de QUALQUER outro ajuste, seja mandando só o delta (quando se sabe a origem) ou tudo (origem desconhecida)."""
    esperado = _efetivo(_estado_da_cadeia(broadcast._audio_chain(alvo)), alvo)
    for origem in TODOS:
        partida = _estado_da_cadeia(broadcast._audio_chain(origem))
        com_delta = _aplicar(partida, broadcast._audio_commands(alvo, origem))
        completo = _aplicar(partida, broadcast._audio_commands(alvo))
        assert _efetivo(com_delta, alvo) == esperado, f"delta {origem} -> {alvo}"
        assert _efetivo(completo, alvo) == esperado, f"completo {origem} -> {alvo}"


def test_o_delta_manda_so_o_que_mudou_e_o_corte_de_agudos_vai_primeiro():
    P = {"reducao_ruido": "medio", "corte_agudos": "medio"}
    assert broadcast._audio_commands(P, P) == []
    assert broadcast._audio_commands({**P, "corte_agudos": "forte"}, P) == ["lowpass@lp2 -1 frequency 4000", "lowpass@lp2 -1 mix 1"]
    assert broadcast._audio_commands({**P, "corte_agudos": "suave"}, {**P, "corte_agudos": "forte"}) == [
        "lowpass@lp1 -1 frequency 5000", "lowpass@lp2 -1 mix 0"]
    assert broadcast._audio_commands({**P, "reducao_ruido": "forte"}, P) == ["afftdn@dn -1 nr 25", "afftdn@dn -1 nf -35"]
    cmds = broadcast._audio_commands(P, {"reducao_ruido": "off", "corte_agudos": "off"})
    assert cmds[0].startswith("lowpass@lp1") and cmds.index("lowpass@lp1 -1 mix 1") < cmds.index("highpass@hp -1 mix 1")


def test_os_cortes_tem_as_frequencias_medidas():
    for corte, esperado in {"suave": [5000], "medio": [4000], "forte": [4000, 4000], "maximo": [3500, 3500]}.items():
        st = _estado_da_cadeia(broadcast._audio_chain({"reducao_ruido": "off", "corte_agudos": corte}))
        ligados = [float(st[(lp, "f")]) for lp in ("lp1", "lp2") if st[(lp, "mix")] == "1"]
        assert ligados == esperado, corte


def test_os_niveis_de_reducao_tem_os_parametros_medidos():
    for nivel, (nr, nf) in {"leve": (10, -50), "medio": (14, -45), "forte": (25, -35)}.items():
        st = _estado_da_cadeia(broadcast._audio_chain({"reducao_ruido": nivel, "corte_agudos": "off"}))
        assert (float(st[("dn", "nr")]), float(st[("dn", "nf")])) == (nr, nf) and st[("dn", "om")] == "o"


def _graph(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def test_com_o_padrao_o_audio_entra_antes_do_volume_e_do_limitador():
    audio = _graph(broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 120)).split(";[0:a]")[1]
    assert audio.startswith(broadcast._audio_chain() + ",volume@vol=1.200:eval=frame,alimiter=limit=0.95,aresample=44100,")
    assert audio.index("highpass@hp") < audio.index("afftdn@dn") < audio.index("lowpass@lp1") < audio.index("lowpass@lp2") \
        < audio.index("volume@vol") < audio.index("alimiter") < audio.rindex("aresample=44100")


def test_ajustes_da_quadra_entram_no_comando_quando_passados():
    graph = _graph(broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100, audio={"reducao_ruido": "leve", "corte_agudos": "forte"}))
    assert "afftdn@dn=nr=10:nf=-50:tn=1:om=o" in graph and "lowpass@lp1=f=4000:mix=1,lowpass@lp2=f=4000:mix=1" in graph


def test_o_volume_ao_vivo_continua_enderecando_o_mesmo_filtro():
    """O comando de volume em tempo real fala com `volume@vol`: a cadeia nova não pode mudar esse nome nem duplicá-lo."""
    assert _graph(broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100)).count("volume@vol=") == 1


def test_sem_audio_da_camera_nao_aplica_filtro_nenhum():
    graph = _graph(broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100, has_audio=False))
    assert "afftdn" not in graph and "highpass" not in graph and "lowpass" not in graph and ";[1:a]volume@vol" in graph


def test_filtro_chamado_direto_sem_o_argumento_novo_nao_muda():
    assert ";[0:a]volume@vol=1.000" in broadcast._filter_graph("q1", 100)
    assert ";[1:a]volume@vol=1.000" in broadcast._filter_graph("q1", 100, "1:a")


# --- ffmpeg sem o afftdn: a live não pode quebrar ----------------------------------------------

def test_ffmpeg_sem_afftdn_transmite_com_o_corte_de_agudos_e_avisa_uma_vez(monkeypatch, capsys):
    monkeypatch.setattr(broadcast, "_denoise_filter_ok", None)
    chamadas = []

    def fake_run(cmd, **kw):
        chamadas.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=" ... afwtdn ... anlmdn ...\n", stderr="")

    monkeypatch.setattr(broadcast.subprocess, "run", fake_run)
    for _ in range(3):
        graph = _graph(broadcast._ffmpeg_cmd("q1", "rtsp://x", "KEY", 100))
        assert "afftdn" not in graph and "highpass@hp" not in graph and "lowpass@lp1=f=4000:mix=1" in graph
    assert len(chamadas) == 1, "a checagem do ffmpeg roda uma vez só"
    assert capsys.readouterr().out.count("não tem o filtro afftdn") == 1
    assert not any("afftdn" in c or "highpass" in c for c in broadcast._audio_commands(PADRAO))


def test_ffmpeg_que_nao_responde_a_pergunta_nao_usa_o_afftdn(monkeypatch):
    monkeypatch.setattr(broadcast, "_denoise_filter_ok", None)

    def quebra(*a, **k):
        raise OSError("sem ffmpeg")

    monkeypatch.setattr(broadcast.subprocess, "run", quebra)
    assert "afftdn" not in broadcast._audio_chain()


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
def test_o_rumor_grave_e_cortado_e_a_voz_passa_intacta_com_o_padrao():
    rumor = "sine=frequency=40:sample_rate=16000:duration=12,volume=0.5"
    voz = "sine=frequency=1000:sample_rate=16000:duration=12,volume=0.5"
    agudo = "sine=frequency=6000:sample_rate=16000:duration=12,volume=0.5"
    chain = broadcast._audio_chain()
    assert _mean_volume(rumor, "anull") - _mean_volume(rumor, chain) > 14, "rumor de 40 Hz: mais de 14 dB"
    assert abs(_mean_volume(voz, "anull") - _mean_volume(voz, chain)) < 0.5, "um tom de 1 kHz não pode mudar"
    assert _mean_volume(agudo, "anull") - _mean_volume(agudo, chain) > 12, "o agudo de 6 kHz cai mais de 12 dB (corte 4 kHz)"


@needs_ffmpeg
def test_tudo_desligado_e_transparente():
    desligado = broadcast._audio_chain({"reducao_ruido": "off", "corte_agudos": "off"})
    for fonte in ("sine=frequency=1000:sample_rate=16000:duration=12,volume=0.5",
                  "sine=frequency=6000:sample_rate=16000:duration=12,volume=0.5",
                  "anoisesrc=amplitude=0.02:sample_rate=16000:duration=12"):
        assert abs(_mean_volume(fonte, "anull") - _mean_volume(fonte, desligado)) < 0.3, fonte


@needs_ffmpeg
@pytest.mark.parametrize("ajustes", TODOS, ids=lambda s: f"{s['reducao_ruido']}-{s['corte_agudos']}")
def test_todas_as_combinacoes_rodam_no_ffmpeg_sem_erro(ajustes):
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=amplitude=0.02:sample_rate=16000:duration=1",
         "-af", broadcast._audio_chain(ajustes), "-f", "null", "-"], capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


@needs_ffmpeg
def test_o_grafo_completo_da_transmissao_e_aceito_pelo_ffmpeg(tmp_path, monkeypatch):
    """Roda o filter_complex real (vídeo com placar + áudio com a cadeia nova) em fontes sintéticas."""
    if not shutil.os.path.exists(config.BROADCAST_FONT):
        pytest.skip("fonte do placar não instalada")
    monkeypatch.setattr(config, "BROADCAST_DIR", tmp_path / "bc")
    monkeypatch.setattr(config, "BROADCAST_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(broadcast, "_state", {})
    monkeypatch.setattr(broadcast, "_loaded", False)
    broadcast._write_overlay("q1")
    graph = broadcast._filter_graph("q1", 100, "1:a", broadcast._audio_chain())
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=640x360:d=2:r=25",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2", "-filter_complex", graph,
         "-map", "[v]", "-map", "[a]", "-t", "2", "-f", "null", "-"], capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


def _nivel_db(amostras: list[int], sr: int, hz: float) -> float:
    """Nível (dB) de uma frequência numa janela de PCM s16 (Goertzel, sem numpy)."""
    n = len(amostras)
    k = round(n * hz / sr)
    w = 2 * math.pi * k / n
    coef = 2 * math.cos(w)
    s1 = s2 = 0.0
    for x in amostras:
        s0 = x / 32768 + coef * s1 - s2
        s2, s1 = s1, s0
    pot = s1 * s1 + s2 * s2 - coef * s1 * s2
    return 10 * math.log10(max(pot, 1e-20)) - 20 * math.log10(n / 2)


@needs_ffmpeg
def test_trocar_o_corte_de_agudos_com_o_ffmpeg_rodando_muda_o_som_e_volta():
    """O ponto central: os MESMOS comandos que a API manda ao ffmpeg no ar mudam o som sem relançar, e o ffmpeg
    confirma cada um com ret:0. Fonte: tom de 1 kHz (voz) + tom de 6 kHz (agudo). O ffmpeg processa um comando a
    cada ~0,5 s e o fluxo corre 3x mais rápido que o relógio, por isso as janelas de checagem ficam bem afastadas."""
    sr = 16000
    desligado = {"reducao_ruido": "off", "corte_agudos": "off"}
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-readrate", "3",
           "-f", "lavfi", "-i", f"sine=frequency=1000:sample_rate={sr}:duration=30",
           "-f", "lavfi", "-i", f"sine=frequency=6000:sample_rate={sr}:duration=30",
           "-filter_complex", f"[0][1]amix=inputs=2:normalize=0,{broadcast._audio_chain(desligado)}[a]",
           "-map", "[a]", "-f", "s16le", "-ar", str(sr), "pipe:1"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    erros: list[bytes] = []
    saida = bytearray()
    # Lê as duas saídas em paralelo: com o canal cheio o ffmpeg para e deixa de processar comandos (na transmissão
    # real a saída vai para um arquivo e isso não acontece).
    threading.Thread(target=lambda: erros.extend(iter(p.stderr.readline, b"")), daemon=True).start()
    leitor = threading.Thread(target=lambda: [saida.extend(c) for c in iter(lambda: p.stdout.read(4096), b"")], daemon=True)
    leitor.start()

    def enviar(novo, antigo):
        p.stdin.write("".join(f"c{c}\n" for c in broadcast._audio_commands(novo, antigo)).encode())
        p.stdin.flush()

    time.sleep(1.0)
    enviar(PADRAO, desligado)           # corte médio (4 kHz) + redução média: 6 comandos, o corte primeiro
    time.sleep(4.0)
    enviar(desligado, PADRAO)           # volta a transparente: 3 comandos
    p.wait(timeout=40)
    leitor.join(timeout=10)
    raw = bytes(saida[: len(saida) // 2 * 2])
    pcm = list(struct.unpack(f"<{len(raw) // 2}h", raw))
    agudo = [_nivel_db(pcm[s * sr:(s + 1) * sr], sr, 6000) for s in range(len(pcm) // sr)]
    voz = [_nivel_db(pcm[s * sr:(s + 1) * sr], sr, 1000) for s in range(len(pcm) // sr)]

    base = agudo[1]
    assert min(agudo[9:14]) < base - 12, f"depois do comando o agudo tem de cair mais de 12 dB: {[round(a) for a in agudo]}"
    assert all(abs(v - voz[1]) < 0.5 for v in voz[1:-1]), f"o tom de 1 kHz não pode mudar nunca: {[round(v, 1) for v in voz]}"
    assert abs(agudo[-3] - base) < 1.5, f"ao desligar, o agudo tem de voltar ao nível original: {[round(a) for a in agudo]}"
    respostas = [int(m) for l in erros for m in re.findall(rb"ret:(-?\d+)", l)]
    assert len(respostas) == 9 and all(r == 0 for r in respostas), f"o ffmpeg tem de confirmar os 9 comandos com ret:0: {respostas}"
