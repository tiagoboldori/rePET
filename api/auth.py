"""Autorização das rotas de controle (/live, /broadcast, /status).

Três formas de entrar, avaliadas nesta ordem (a primeira presente decide; não há "tenta a próxima"
se ela estiver errada):

1. `Authorization: Bearer <token>`: o token pode ser a LIVE_API_KEY (token próprio da API de live),
   o REPLAY_API_TOKEN (o mesmo token Sanctum que já usamos para falar com o Lara) **ou** uma chave
   nomeada de API_TOKENS (uma por integração, revogável sozinha). Qualquer uma abre as mesmas rotas.
   É o método recomendado para integrações.
2. `X-Live-Key: <LIVE_API_KEY>`: o método original, mantido por compatibilidade (scripts, docs/LIVE.md).
3. Cookie de sessão do painel (`repet_painel`): rotas que mudam algo exigem também `X-Painel: 1`.

Os tokens são comparados em tempo constante (`hmac.compare_digest` sobre bytes, então um cabeçalho com
caracteres não ASCII dá 401 e não um erro 500) e nunca aparecem em mensagem de erro nem em log.
"""
import hmac

from fastapi import HTTPException, Request

from api import config, panel

_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def _named_tokens() -> dict[str, str]:
    """{rótulo: token} de tudo que abre as rotas de controle (o que não está configurado fica de fora)."""
    named = {"live_api_key": config.LIVE_API_KEY, "replay_api_token": config.REPLAY_API_TOKEN}
    named.update({f"api_tokens:{name}": token for name, token in config.API_TOKENS.items()})
    return {name: token for name, token in named.items() if token}


def accepted_tokens() -> list[str]:
    """Tokens que abrem as rotas de controle."""
    return list(_named_tokens().values())


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def token_owner(candidate: str | None) -> str | None:
    """Rótulo do token (`live_api_key`, `replay_api_token` ou `api_tokens:<nome>`) ou None se não vale.
    Percorre todos os tokens, sem curto-circuito."""
    if not candidate:
        return None
    owner = None
    for name, token in _named_tokens().items():
        if _equal(candidate, token):
            owner = name
    return owner


def token_is_valid(candidate: str | None) -> bool:
    return token_owner(candidate) is not None


def parse_bearer(authorization: str | None) -> str | None:
    """Extrai o token de `Authorization: Bearer <token>`; None se o esquema não for Bearer."""
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip()


def check_access(request: Request) -> None:
    """Levanta HTTPException (401/403/503) se a requisição não puder usar as rotas de controle."""
    bearer = parse_bearer(request.headers.get("authorization"))
    if bearer is not None:
        if not accepted_tokens():
            raise HTTPException(status_code=503, detail="Nenhum token configurado (LIVE_API_KEY / REPLAY_API_TOKEN / API_TOKENS).")
        owner = token_owner(bearer)
        if owner is None:
            raise HTTPException(status_code=401, detail="Token inválido.", headers=_BEARER_CHALLENGE)
        request.state.token_owner = owner  # rótulo, nunca o token (útil em log/auditoria)
        return

    x_live_key = request.headers.get("x-live-key")
    if x_live_key:
        if not config.LIVE_API_KEY:
            raise HTTPException(status_code=503, detail="LIVE_API_KEY não configurada.")
        if not _equal(x_live_key, config.LIVE_API_KEY):
            raise HTTPException(status_code=401, detail="Chave inválida.", headers=_BEARER_CHALLENGE)
        request.state.token_owner = "x_live_key"
        return

    user = panel.verify_token(request.cookies.get(panel.COOKIE))
    if user:
        if request.method not in ("GET", "HEAD") and request.headers.get("x-painel") != "1":
            raise HTTPException(status_code=403, detail="Falta o header X-Painel.")
        request.state.token_owner = f"painel:{user}"  # quem é o operador, para a auditoria
        return

    if not accepted_tokens() and not panel.configured():
        raise HTTPException(status_code=503, detail="Nenhum token configurado (LIVE_API_KEY / REPLAY_API_TOKEN / API_TOKENS).")
    raise HTTPException(status_code=401, detail="Chave inválida ou sessão ausente.", headers=_BEARER_CHALLENGE)
