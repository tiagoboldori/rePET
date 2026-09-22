#!/usr/bin/env python3
"""
lara_worker.py — processo único com as três rotinas de fundo da
integração com o Lara (PT-14/PT-15, PLANO_DE_ACAO.md v3): sincronização de
configuração, fila de envio de clipes e heartbeat. Nenhuma delas roda no
caminho do acionamento do botão (RNF3/RNF9) — cada uma em sua própria
thread, com seu próprio intervalo e sua própria sessão de banco a cada
passada (SQLite/SQLModel não compartilha sessão entre threads).

Se `LARA_BASE_URL`/`REPLAY_API_TOKEN` não estiverem configurados, o
processo avisa e sai — não tem sentido subir sem eles (mesmo raciocínio de
`ffmpeg` obrigatório no start.sh).

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
from db.engine import engine
from integrations import config_sync, heartbeat, upload_queue
from integrations.lara_client import LaraClient


def _loop(name: str, interval: float, fn: Callable[[Session], None]) -> None:
    while True:
        try:
            with Session(engine) as session:
                fn(session)
        except Exception as exc:  # noqa: BLE001 — worker de fundo não pode morrer por uma falha isolada
            print(f"[lara-worker:{name}] erro inesperado: {exc}")
        time.sleep(interval)


def main() -> None:
    if not config.LARA_BASE_URL or not config.REPLAY_API_TOKEN:
        print(
            "[lara-worker] LARA_BASE_URL/REPLAY_API_TOKEN não configurados — "
            "não subindo (nada a sincronizar/enviar sem eles)."
        )
        sys.exit(1)

    client = LaraClient(config.LARA_BASE_URL, config.REPLAY_API_TOKEN)
    config.OVERLAY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    routines = [
        ("sync", config.LARA_POLL_INTERVAL_SECONDS,
         lambda s: config_sync.sync_once(s, client, config.OVERLAY_CACHE_DIR)),
        ("upload", config.LARA_UPLOAD_POLL_INTERVAL_SECONDS,
         lambda s: upload_queue.process_pending(
             s, client, config.OUTPUT_DIR,
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
