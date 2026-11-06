"""Regras de segurança do painel web (api/static), travadas em teste.

O servidor responde o painel com `Content-Security-Policy: default-src 'self'`, que bloqueia script e estilo
inline e imagens `data:`; e todo texto vindo da API tem de entrar pelo DOM como texto (nunca como HTML).
Estes testes leem os arquivos; o comportamento na tela é verificado com um navegador de verdade fora do pytest.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "api" / "static"
HTML = (STATIC / "painel.html").read_text(encoding="utf-8")
JS = (STATIC / "painel.js").read_text(encoding="utf-8")
# o código sem comentários (os comentários podem citar o que é proibido, justamente para avisar)
JS_CODE = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)
JS_CODE = re.sub(r"(?m)^\s*//.*$|(?<=[;{}),])\s*//.*$", "", JS_CODE)
CSS = (STATIC / "painel.css").read_text(encoding="utf-8")


def test_html_nao_tem_script_nem_estilo_inline():
    assert "<style" not in HTML.lower()
    assert not re.search(r"<script(?![^>]*\bsrc=)", HTML, re.I), "script inline: a CSP bloqueia"
    assert not re.search(r'\sstyle\s*=', HTML, re.I), "atributo style inline: a CSP bloqueia"
    assert not re.search(r'\son[a-z]+\s*=', HTML, re.I), "manipulador inline (onclick etc.): a CSP bloqueia"


def test_arquivos_referenciados_existem_e_tem_versao():
    refs = re.findall(r'(?:src|href)="(/painel/static/[^"?]+)(\?v=[^"]+)?"', HTML)
    assert refs, "o HTML não referencia o CSS/JS do painel"
    for path, version in refs:
        assert (STATIC / path.rsplit("/", 1)[1]).is_file(), f"{path} não existe"
        assert version, f"{path} sem ?v=: o navegador ficaria com a versão antiga em cache"


@pytest.mark.parametrize("proibido", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setAttribute(\"style\"", "setAttribute('style'"])
def test_js_nao_injeta_html_nem_executa_texto(proibido):
    assert proibido not in JS_CODE, f"{proibido!r} no painel.js: todo texto da API deve entrar como texto"


def test_js_so_fala_com_a_propria_origem():
    urls = set(re.findall(r"https?://[^\s\"'`)]+", JS_CODE))
    # a única exceção é o namespace do SVG, um identificador XML que não gera requisição nenhuma
    assert urls <= {"http://www.w3.org/2000/svg"}, f"URL externa no painel.js: {urls}"


def test_css_nao_usa_recursos_externos():
    assert "@import" not in CSS
    assert not re.search(r"https?://|url\(\s*['\"]?data:", CSS), "a CSP bloqueia recursos externos e imagens data:"


def _segmentos(rota: str) -> tuple[str, ...]:
    """'/quadras/${card.id}/youtube?x=1' e '/quadras/{quadra_id}/youtube' viram ('quadras', '*', 'youtube')."""
    rota = rota.split("?")[0]
    return tuple("*" if (s.startswith("${") or s.startswith("{")) else s for s in rota.strip("/").split("/"))


def test_js_so_chama_rotas_que_existem():
    """Toda rota citada no painel tem de existir na API (pega erro de digitação e rota renomeada)."""
    main = (STATIC.parent / "main.py").read_text(encoding="utf-8")
    existentes = {_segmentos(d) for d in re.findall(r'@app\.\w+\("([^"]+)"', main)}
    usadas = set(re.findall(r"[`\"'](/(?:status|broadcast|live|quadras|painel/(?:login|logout|me))[^`\"']*)[`\"']", JS_CODE))
    assert usadas, "não achei nenhuma chamada de API no painel.js (o teste está cego?)"
    for rota in usadas:
        seg = _segmentos(rota)
        assert any(len(seg) == len(e) and all(a == b or "*" in (a, b) for a, b in zip(seg, e)) for e in existentes), \
            f"o painel chama {rota!r}, que não existe na API"
    # as rotas novas do destino da transmissão têm de estar entre as usadas
    assert ("quadras", "*", "youtube", "key") in {_segmentos(r) for r in usadas}
    assert ("quadras", "*", "youtube", "url") in {_segmentos(r) for r in usadas}
    assert ("broadcast", "*", "audio") in {_segmentos(r) for r in usadas}  # aba Áudio


def test_volume_do_painel_tem_teto_de_100():
    """O painel nunca envia volume acima do volume original da câmera (100%): controle, + e − respeitam o teto."""
    assert re.search(r"const VOLUME_MAX = 100;", JS_CODE)
    assert re.search(r"max:\s*VOLUME_MAX", JS_CODE), "o controle deslizante tem de usar VOLUME_MAX como máximo"
    assert "volume/up" not in JS_CODE, "volume/up soma um passo fixo no servidor e passaria do teto: o painel calcula e usa PUT"
    assert re.search(r"Math\.min\(VOLUME_MAX,\s*nivel\)", JS_CODE), "o envio tem de limitar o nível ao teto"
