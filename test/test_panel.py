"""Sessão e login do painel web (sem rede, sem servidor)."""
from __future__ import annotations

import pytest

from api import config, panel


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "operador")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3nha-longa")
    monkeypatch.setattr(config, "PANEL_SECRET", "")
    monkeypatch.setattr(config, "PANEL_SESSION_HOURS", 12.0)
    panel._failures.clear()


def test_credenciais():
    assert panel.check_credentials("operador", "s3nha-longa")
    assert not panel.check_credentials("operador", "errada")
    assert not panel.check_credentials("outro", "s3nha-longa")
    assert not panel.check_credentials("", "")


def test_sem_credencial_configurada_nada_entra(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    assert not panel.configured()
    assert not panel.check_credentials("", "")  # nem usuário e senha vazios
    assert panel.verify_token(panel.make_token("x")) is None


def test_token_ida_e_volta_e_expiracao():
    token = panel.make_token("operador", now=1000.0)
    assert panel.verify_token(token, now=1000.0 + 3600) == "operador"
    assert panel.verify_token(token, now=1000.0 + 12 * 3600 + 1) is None  # expirou


def test_token_adulterado_ou_de_outra_senha_e_recusado(monkeypatch):
    token = panel.make_token("operador")
    payload, _, sig = token.partition(".")
    assert panel.verify_token(f"{payload}.{'0' * len(sig)}") is None
    assert panel.verify_token(f"x{payload}.{sig}") is None
    assert panel.verify_token(payload) is None and panel.verify_token("") is None and panel.verify_token(None) is None
    forged = panel._b64(b"administrador|99999999999") + "." + sig  # troca o corpo, mantém a assinatura
    assert panel.verify_token(forged) is None
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "outra-senha")  # trocar a senha derruba as sessões
    assert panel.verify_token(token) is None


def test_segredo_extra_muda_a_chave(monkeypatch):
    token = panel.make_token("operador")
    monkeypatch.setattr(config, "PANEL_SECRET", "x")
    assert panel.verify_token(token) is None


def test_usuario_com_barra_vertical_nao_confunde_o_formato():
    assert panel.verify_token(panel.make_token("a|b")) == "a|b"


def test_limite_de_tentativas_por_ip():
    for _ in range(panel.MAX_FAILURES):
        assert not panel.login_blocked("1.1.1.1", now=100.0)
        panel.register_failure("1.1.1.1", now=100.0)
    assert panel.login_blocked("1.1.1.1", now=101.0)
    assert not panel.login_blocked("2.2.2.2", now=101.0)  # outro IP não é afetado
    assert not panel.login_blocked("1.1.1.1", now=100.0 + panel.WINDOW_SECONDS + 1)  # a janela passa
    panel.register_failure("3.3.3.3", now=1.0)
    panel.clear_failures("3.3.3.3")
    assert not panel.login_blocked("3.3.3.3", now=2.0)
