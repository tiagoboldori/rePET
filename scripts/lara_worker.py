#!/usr/bin/env python3
"""Processo com as rotinas de fundo da integração: sincronização de config, fila
de envio e heartbeat. Cada uma roda em sua thread, com sessão de banco própria.
Sai se LARA_BASE_URL/REPLAY_API_TOKEN ou as variáveis de FTP não estiverem configurados.

Uso:
    python -m scripts.lara_worker
"""
from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

from sqlmodel import Session

from api import config
from db.engine import create_db_and_tables, engine
from integrations import config_sync, heartbeat, upload_queue
from integrations.ftp_client import FtpClient
from integrations.lara_client import LaraClient


def _loop(name: str, interval: float, fn: Callable[[Session], None]) -> None:
    while True:
        try:
            with Session(engine) as session:
                fn(session)
        except Exception as exc:  # noqa: BLE001 — não deixa o worker morrer
            print(f"[lara-worker:{name}] erro inesperado: {exc}")
        time.sleep(interval)


def main() -> None:
    if not config.LARA_BASE_URL or not config.REPLAY_API_TOKEN:
        print(
            "[lara-worker] LARA_BASE_URL/REPLAY_API_TOKEN não configurados — "
            "não subindo (nada a sincronizar/enviar sem eles)."
        )
        sys.exit(1)

    try:
        ftp = FtpClient(
            config.FTP_HOST, config.FTP_PORT, config.FTP_USER, config.FTP_PASSWORD,
            config.FTP_BASE_DIR, config.FTP_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        print(f"[lara-worker] {exc} — não subindo (os clipes vão para o FTP).")
        sys.exit(1)

    create_db_and_tables()  # idempotente; garante colunas novas no banco existente
    client = LaraClient(config.LARA_BASE_URL, config.REPLAY_API_TOKEN)
    config.OVERLAY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    routines = [
        ("sync", config.LARA_POLL_INTERVAL_SECONDS,
         lambda s: config_sync.sync_once(s, client, config.OVERLAY_CACHE_DIR)),
        ("upload", config.LARA_UPLOAD_POLL_INTERVAL_SECONDS,
         lambda s: upload_queue.process_pending(
             s, client, ftp, config.OUTPUT_DIR,
             config.MUSIC_DIR, config.MUSIC_VOLUME, config.MUSIC_FADE_SECONDS,
         )),
        ("heartbeat", config.LARA_HEARTBEAT_INTERVAL_SECONDS,
         lambda s: heartbeat.send_all(s, client)),
    ]
    threads = [
        threading.Thread(target=_loop, args=(name, interval, fn), daemon=True)
        for name, interval, fn in routines
    ]
    for t in threads:
        t.start()

    print("[lara-worker] sync/upload/heartbeat no ar.")
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
