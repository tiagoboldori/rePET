"""Autorização das rotas de controle: Bearer (LIVE_API_KEY ou REPLAY_API_TOKEN), X-Live-Key e cookie.

Sem rede e sem servidor: monta o `Request` do Starlette direto e chama api.auth.check_access.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api import auth, config, panel

LIVE = "live-key-de-teste"
LARA = "7|token-sanctum-do-lara"
PAINEL = "repet_live_chave-da-integracao-do-painel"


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.setattr(config, "LIVE_API_KEY", LIVE)
    monkeypatch.setattr(config, "REPLAY_API_TOKEN", LARA)
    monkeypatch.setattr(config, "API_TOKENS", {"painel-web": PAINEL})
    monkeypatch.setattr(config, "ADMIN_USERNAME", "operador")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3nha-longa")
    monkeypatch.setattr(config, "PANEL_SECRET", "")
    monkeypatch.setattr(config, "PANEL_SESSION_HOURS", 12.0)


def req(method: str = "GET", **headers: str) -> Request:
    """Request mínimo. Nomes de header com `_` viram `-` (x_live_key -> x-live-key)."""
    raw = [(k.replace("_", "-").lower().encode(), v.encode("utf-8")) for k, v in headers.items()]
    return Request({"type": "http", "method": method, "path": "/", "headers": raw, "query_string": b""})


def status_of(request: Request) -> int:
    try:
        auth.check_access(request)
    except HTTPException as exc:
        return exc.status_code
    return 200


def test_bearer_com_a_chave_da_live():
    assert status_of(req(authorization=f"Bearer {LIVE}")) == 200


def test_bearer_com_o_token_do_lara():
    assert status_of(req(authorization=f"Bearer {LARA}")) == 200


def test_bearer_com_a_chave_nomeada_de_api_tokens():
    assert status_of(req(authorization=f"Bearer {PAINEL}")) == 200
    assert status_of(req("POST", authorization=f"Bearer {PAINEL}")) == 200


def test_chave_nomeada_nao_vale_no_x_live_key():
    assert status_of(req(x_live_key=PAINEL)) == 401


def test_chave_revogada_deixa_de_valer_e_as_outras_continuam(monkeypatch):
    monkeypatch.setattr(config, "API_TOKENS", {})  # tirou a linha do .env
    assert status_of(req(authorization=f"Bearer {PAINEL}")) == 401
    assert status_of(req(authorization=f"Bearer {LIVE}")) == 200
    assert status_of(req(authorization=f"Bearer {LARA}")) == 200


def test_varias_chaves_nomeadas_sao_independentes(monkeypatch):
    monkeypatch.setattr(config, "API_TOKENS", {"painel-web": PAINEL, "relatorios": "repet_live_outra"})
    assert status_of(req(authorization="Bearer repet_live_outra")) == 200
    monkeypatch.setattr(config, "API_TOKENS", {"relatorios": "repet_live_outra"})
    assert status_of(req(authorization="Bearer repet_live_outra")) == 200
    assert status_of(req(authorization=f"Bearer {PAINEL}")) == 401


def test_rotulo_de_quem_chamou_fica_no_request_sem_o_token():
    r = req(authorization=f"Bearer {PAINEL}")
    auth.check_access(r)
    assert r.state.token_owner == "api_tokens:painel-web"
    r = req(authorization=f"Bearer {LARA}")
    auth.check_access(r)
    assert r.state.token_owner == "replay_api_token"
    assert auth.token_owner("nada") is None and auth.token_owner("") is None


def test_so_api_tokens_configurado(monkeypatch):
    monkeypatch.setattr(config, "LIVE_API_KEY", "")
    monkeypatch.setattr(config, "REPLAY_API_TOKEN", "")
    assert status_of(req(authorization=f"Bearer {PAINEL}")) == 200
    assert status_of(req(authorization=f"Bearer {LIVE}")) == 401


@pytest.mark.parametrize("raw,esperado", [
    ("", {}),
    ("painel:abc123", {"painel": "abc123"}),
    (" painel : abc123 , outro:def456 ,", {"painel": "abc123", "outro": "def456"}),
    ("semdoispontos", {}),
    (":semnome", {}),
    ("semtoken:", {}),
    ("a:b:c", {"a": "b:c"}),  # só o primeiro ':' separa; o token pode ter ':'
    ("ok:1,quebrado,ok2:2", {"ok": "1", "ok2": "2"}),
])
def test_parse_api_tokens(raw, esperado):
    assert config.parse_api_tokens(raw) == esperado


def test_bearer_vale_em_metodos_que_mudam_algo_sem_x_painel():
    assert status_of(req("POST", authorization=f"Bearer {LARA}")) == 200
    assert status_of(req("DELETE", authorization=f"Bearer {LIVE}")) == 200


def test_esquema_bearer_nao_diferencia_maiuscula():
    assert status_of(req(authorization=f"bearer {LIVE}")) == 200
    assert status_of(req(authorization=f"BEARER {LARA}")) == 200


def test_bearer_errado_e_401_com_desafio_www_authenticate():
    with pytest.raises(HTTPException) as exc:
        auth.check_access(req(authorization="Bearer qualquer-coisa"))
    assert exc.value.status_code == 401
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Bearer    "])
def test_bearer_vazio_e_401(header):
    assert status_of(req(authorization=header)) == 401


def test_bearer_errado_nao_cai_para_x_live_key():
    # Se o cliente mandou Bearer, só ele vale: um X-Live-Key correto junto não "salva" um token ruim.
    assert status_of(req(authorization="Bearer errado", x_live_key=LIVE)) == 401


def test_prefixo_ou_sufixo_do_token_nao_vale():
    assert status_of(req(authorization=f"Bearer {LIVE[:-1]}")) == 401
    assert status_of(req(authorization=f"Bearer {LIVE}x")) == 401
    assert status_of(req(authorization=f"Bearer {LARA.split('|')[1]}")) == 401  # sem o "id|"


def test_esquema_que_nao_e_bearer_e_ignorado():
    assert status_of(req(authorization="Basic b3BlcmFkb3I6czNuaGE=")) == 401
    # e não atrapalha quem também manda X-Live-Key
    assert status_of(req(authorization="Basic b3BlcmFkb3I6czNuaGE=", x_live_key=LIVE)) == 200


def test_x_live_key_continua_funcionando():
    assert status_of(req(x_live_key=LIVE)) == 200
    assert status_of(req("POST", x_live_key=LIVE)) == 200
    assert status_of(req(x_live_key="errada")) == 401


def test_x_live_key_nao_aceita_o_token_do_lara():
    # O método legado segue sendo só a LIVE_API_KEY; o token do Lara entra apenas via Bearer.
    assert status_of(req(x_live_key=LARA)) == 401


def test_sem_credencial_nenhuma_e_401():
    with pytest.raises(HTTPException) as exc:
        auth.check_access(req())
    assert exc.value.status_code == 401
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


def test_cabecalho_nao_ascii_e_401_e_nao_erro_500():
    assert status_of(req(authorization="Bearer tókén-ínválido")) == 401
    assert status_of(req(x_live_key="chávé")) == 401


def test_so_o_token_do_lara_configurado(monkeypatch):
    monkeypatch.setattr(config, "API_TOKENS", {})
    monkeypatch.setattr(config, "LIVE_API_KEY", "")
    assert status_of(req(authorization=f"Bearer {LARA}")) == 200
    assert status_of(req(authorization=f"Bearer {LIVE}")) == 401
    assert status_of(req(x_live_key=LIVE)) == 503  # o legado exige LIVE_API_KEY configurada


def test_so_a_chave_da_live_configurada(monkeypatch):
    monkeypatch.setattr(config, "API_TOKENS", {})
    monkeypatch.setattr(config, "REPLAY_API_TOKEN", "")
    assert status_of(req(authorization=f"Bearer {LIVE}")) == 200
    assert status_of(req(authorization=f"Bearer {LARA}")) == 401


def test_nada_configurado_recusa_tudo_com_503(monkeypatch):
    monkeypatch.setattr(config, "LIVE_API_KEY", "")
    monkeypatch.setattr(config, "REPLAY_API_TOKEN", "")
    monkeypatch.setattr(config, "API_TOKENS", {})
    monkeypatch.setattr(config, "ADMIN_USERNAME", "")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    assert status_of(req(authorization="Bearer qualquer")) == 503
    assert status_of(req(authorization="Bearer ")) == 503  # token vazio não casa com config vazia
    assert status_of(req(x_live_key="qualquer")) == 503
    assert status_of(req()) == 503


def test_token_vazio_nunca_casa_com_config_vazia(monkeypatch):
    monkeypatch.setattr(config, "LIVE_API_KEY", "")
    monkeypatch.setattr(config, "REPLAY_API_TOKEN", "")
    monkeypatch.setattr(config, "API_TOKENS", {})
    assert auth.accepted_tokens() == []
    assert not auth.token_is_valid("")
    assert not auth.token_is_valid(None)


def test_mensagens_de_erro_nao_vazam_token():
    for headers in ({"authorization": "Bearer errado"}, {"x_live_key": "errada"}, {}):
        with pytest.raises(HTTPException) as exc:
            auth.check_access(req(**headers))
        for secret in (LIVE, LARA, PAINEL):
            assert secret not in str(exc.value.detail)


# --- cookie do painel (comportamento anterior, preservado) ---------------------

def test_cookie_do_painel_em_get():
    cookie = f"{panel.COOKIE}={panel.make_token('operador')}"
    assert status_of(req(cookie=cookie)) == 200


def test_cookie_em_rota_que_muda_algo_exige_x_painel():
    cookie = f"{panel.COOKIE}={panel.make_token('operador')}"
    assert status_of(req("POST", cookie=cookie)) == 403
    assert status_of(req("POST", cookie=cookie, x_painel="1")) == 200
    assert status_of(req("POST", cookie=cookie, x_painel="0")) == 403


def test_cookie_adulterado_e_401():
    assert status_of(req(cookie=f"{panel.COOKIE}=lixo.lixo")) == 401


def test_bearer_ruim_vence_cookie_valido():
    cookie = f"{panel.COOKIE}={panel.make_token('operador')}"
    assert status_of(req(authorization="Bearer errado", cookie=cookie)) == 401


def test_rotulo_do_operador_do_painel_e_do_x_live_key_ficam_no_request():
    cookie = f"{panel.COOKIE}={panel.make_token('operador')}"
    r = req("POST", cookie=cookie, x_painel="1")
    auth.check_access(r)
    assert r.state.token_owner == "painel:operador"
    r = req(x_live_key=LIVE)
    auth.check_access(r)
    assert r.state.token_owner == "x_live_key"
