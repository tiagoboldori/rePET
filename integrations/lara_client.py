"""Cliente HTTP da API da plataforma externa.

Só fala com a rede; as decisões ficam em config_sync.py e upload_queue.py.
Cada tipo de resposta vira uma exceção própria, sem expor códigos HTTP a quem chama.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


class LaraClientError(RuntimeError):
    """Base de todos os erros deste cliente."""


class LaraAuthError(LaraClientError):
    """401/403 — token ausente, inválido ou sem a ability `replay:operate`."""


class LaraNotFoundError(LaraClientError):
    """404 — external_id desconhecido ou câmera inativa. Não é transitório."""


class LaraValidationError(LaraClientError):
    """422 com corpo — dados inválidos; retentar sem corrigir não adianta."""

    def __init__(self, message: str, body: dict | None = None):
        super().__init__(message)
        self.body = body


class LaraPayloadTooLargeError(LaraClientError):
    """413, ou 422 com corpo vazio — limite do servidor web; retentar não resolve."""


class LaraRateLimitError(LaraClientError):
    """429 — limite de requisições. Retentável com backoff."""


class LaraServerError(LaraClientError):
    """5xx ou erro de conexão/timeout. Retentável com backoff."""


@dataclass
class OverlayConfig:
    png_url: str | None
    animated_url: str | None
    width: int | None
    height: int | None


@dataclass
class CameraConfig:
    external_id: str
    config_hash: str
    orientation: str  # "vertical" ou "horizontal"
    clip_seconds: int
    overlay: OverlayConfig | None


@dataclass
class UploadResult:
    uuid: str
    url: str
    expires_at: str
    duplicated: bool


def _parse_overlay(raw: dict | None) -> OverlayConfig | None:
    if raw is None:
        return None
    return OverlayConfig(
        png_url=raw.get("png_url"),
        animated_url=raw.get("animated_url"),
        width=raw.get("width"),
        height=raw.get("height"),
    )


def _parse_camera(raw: dict, default_config_hash: str | None = None) -> CameraConfig:
    return CameraConfig(
        external_id=raw["external_id"],
        config_hash=raw.get("config_hash", default_config_hash),
        orientation=raw["orientation"],
        clip_seconds=int(raw["clip_seconds"]),
        overlay=_parse_overlay(raw.get("overlay")),
    )


class LaraClient:
    """Um cliente por processo basta; a sessão HTTP é reaproveitada."""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0):
        if not base_url:
            raise ValueError("LARA_BASE_URL não configurado.")
        if not token:
            raise ValueError("REPLAY_API_TOKEN não configurado.")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        self._session.headers["Accept"] = "application/json"

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self._base_url}{path}"
        try:
            resp = self._session.request(
                method, url, timeout=kwargs.pop("timeout", self._timeout), **kwargs
            )
        except requests.RequestException as exc:
            raise LaraServerError(f"Falha de conexão com o Lara ({url}): {exc}") from exc

        if resp.status_code in (401, 403):
            raise LaraAuthError(
                f"{resp.status_code} do Lara em {path} — conferir REPLAY_API_TOKEN."
            )
        if resp.status_code == 404:
            raise LaraNotFoundError(f"404 do Lara em {path} — external_id desconhecido.")
        if resp.status_code == 413:
            raise LaraPayloadTooLargeError(
                f"413 do Lara em {path} — arquivo acima do limite do servidor web."
            )
        if resp.status_code == 422:
            body = None
            try:
                body = resp.json()
            except ValueError:
                pass
            if not body:
                raise LaraPayloadTooLargeError(
                    f"422 com corpo vazio em {path} — provável limite de servidor web."
                )
            raise LaraValidationError(
                body.get("message", f"422 do Lara em {path}"), body=body
            )
        if resp.status_code == 429:
            raise LaraRateLimitError(f"429 do Lara em {path} — respeitar backoff.")
        if resp.status_code >= 500:
            raise LaraServerError(f"{resp.status_code} do Lara em {path}.")
        if not resp.ok:
            raise LaraServerError(f"{resp.status_code} inesperado do Lara em {path}.")
        return resp

    def ping(self) -> dict[str, Any]:
        return self._request("GET", "/ping").json()

    def get_cameras(self) -> list[CameraConfig]:
        """A API devolve `{"config_hash": ..., "cameras": [...]}` com um hash só
        pro lote. Ele vira o `config_hash` de cada câmera, a menos que o item
        traga o seu."""
        data = self._request("GET", "/cameras").json()
        if isinstance(data, dict):
            batch_hash = data.get("config_hash")
            cameras = data.get("cameras", data.get("data", data))
        else:
            batch_hash = None
            cameras = data
        return [_parse_camera(c, batch_hash) for c in cameras]

    def get_camera(self, external_id: str) -> CameraConfig:
        data = self._request("GET", f"/cameras/{external_id}").json()
        raw = data.get("data", data) if isinstance(data, dict) else data
        return _parse_camera(raw, raw.get("config_hash") if isinstance(raw, dict) else None)

    def send_heartbeat(self, external_id: str) -> None:
        self._request("POST", f"/cameras/{external_id}/heartbeat")

    def upload_video(
        self,
        external_id: str,
        file_path: Path,
        recorded_at: datetime,
        duration_seconds: int,
        clip_external_id: str,
    ) -> UploadResult:
        """`clip_external_id` é o Replay.id e torna o envio idempotente.
        `recorded_at` é o instante do botão, não do envio."""
        # naive vem de datetime.now(); astimezone() anexa o offset local
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.astimezone()
        with file_path.open("rb") as fh:
            resp = self._request(
                "POST",
                f"/cameras/{external_id}/videos",
                files={"file": (file_path.name, fh)},
                data={
                    "recorded_at": recorded_at.isoformat(),
                    "duration_seconds": str(int(duration_seconds)),
                    "external_id": clip_external_id,
                },
                timeout=max(self._timeout, 60.0),  # upload de até 256MB
            )
        body = resp.json()
        return UploadResult(
            uuid=body["uuid"],
            url=body["url"],
            expires_at=body["expires_at"],
            duplicated=bool(body.get("duplicated", False)),
        )

    def download_to(self, url: str, dest: Path) -> None:
        """Baixa um overlay (png/webm) para disco, via arquivo .part."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = self._session.get(url, timeout=max(self._timeout, 30.0), stream=True)
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
        tmp.replace(dest)
