"""Login do painel web (/painel): sessão assinada em cookie, sem estado no servidor.

- Credencial: ADMIN_USERNAME/ADMIN_PASSWORD (as mesmas do DELETE). Sem elas, 503.
- Sessão: cookie `repet_painel` = base64(usuário|expiração) + "." + HMAC-SHA256. A chave
  deriva da senha (+ PANEL_SECRET opcional), então trocar a senha derruba as sessões e um
  reinício da API NÃO desloga ninguém.
- Cookie HttpOnly + SameSite=Strict. Rotas que mudam algo, quando autenticadas por cookie,
  exigem o header `X-Painel: 1` (um site de fora não consegue enviá-lo sem CORS): é a defesa
  contra CSRF. Quem usa o header X-Live-Key (scripts) não passa por nada disso.
- Tentativas de login erradas: MAX_FAILURES por IP a cada WINDOW_SECONDS, depois 429.
- O tráfego é HTTP puro na rede interna: a senha trafega em claro. Para uso fora da LAN,
  ponha um proxy HTTPS na frente (e então o cookie pode ganhar o atributo Secure).
"""
import base64
import hashlib
import hmac
import secrets
import threading
import time

from api import config

COOKIE = "repet_painel"
MAX_FAILURES = 5
WINDOW_SECONDS = 60.0

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


def configured() -> bool:
    return bool(config.ADMIN_USERNAME and config.ADMIN_PASSWORD)


def _key() -> bytes:
    return hashlib.sha256(
        f"repet-painel|{config.ADMIN_PASSWORD}|{config.PANEL_SECRET}".encode()
    ).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_credentials(username: str, password: str) -> bool:
    ok_user = secrets.compare_digest(username.encode(), config.ADMIN_USERNAME.encode())
    ok_pass = secrets.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())
    return configured() and ok_user and ok_pass


def make_token(username: str, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + config.PANEL_SESSION_HOURS * 3600)
    payload = _b64(f"{username}|{expires}".encode())
    return f"{payload}.{hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()}"


def verify_token(token: str | None, now: float | None = None) -> str | None:
    """Usuário da sessão, ou None se ausente, adulterada ou expirada."""
    if not token or not configured() or "." not in token:
        return None
    payload, _, signature = token.partition(".")
    expected = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(signature, expected):
        return None
    try:
        username, _, expires = _unb64(payload).decode().rpartition("|")
        if float(expires) < (now if now is not None else time.time()):
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return username


def login_blocked(ip: str, now: float | None = None) -> bool:
    """True se este IP já errou a senha vezes demais na janela."""
    now = now if now is not None else time.time()
    with _lock:
        recent = [t for t in _failures.get(ip, []) if now - t < WINDOW_SECONDS]
        _failures[ip] = recent
        return len(recent) >= MAX_FAILURES


def register_failure(ip: str, now: float | None = None) -> None:
    with _lock:
        _failures.setdefault(ip, []).append(now if now is not None else time.time())


def clear_failures(ip: str) -> None:
    with _lock:
        _failures.pop(ip, None)
