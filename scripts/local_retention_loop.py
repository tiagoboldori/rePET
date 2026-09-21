#!/usr/bin/env python3
"""
local_retention_loop.py — roda db/local_retention.py::purge_expired_replays
em loop, uma vez por hora (retenção é medida em dias, não precisa de
cadência mais fina que isso). Subido pelo scripts/ensure_services.sh
(chamado por start.sh e pelo watchdog), sempre — ao contrário do worker
do Lara, não depende de nenhuma credencial externa pra funcionar.

Uso:
    python -m scripts.local_retention_loop
"""
from __future__ import annotations

import time

from sqlmodel import Session

from api import config
from db.engine import create_db_and_tables, engine
from db.local_retention import purge_expired_replays

CHECK_INTERVAL_SECONDS = 3600


def main() -> None:
    create_db_and_tables()  # idempotente — cobre rodar isolado, sem a API já ter subido
    print(
        f"[local-retention] no ar — retenção={config.LOCAL_RAW_RETENTION_DAYS} dia(s), "
        f"checagem a cada {CHECK_INTERVAL_SECONDS}s"
    )
    while True:
        try:
            with Session(engine) as session:
                removidos = purge_expired_replays(
                    session, config.OUTPUT_DIR, config.LOCAL_RAW_RETENTION_DAYS
                )
                if removidos:
                    print(f"[local-retention] {removidos} replay(s) expirado(s) removido(s).")
        except Exception as exc:  # noqa: BLE001 — loop de fundo não pode morrer por uma falha isolada
            print(f"[local-retention] erro inesperado: {exc}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
