"""Retenção local dos clipes: apaga arquivo e registro após LOCAL_RAW_RETENTION_DAYS.

Usa `Replay.criado_em` (não o mtime) e não olha `lara_status`: replays que
nunca foram enviados também expiram.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from db.models import Replay


def purge_expired_replays(session: Session, output_dir: Path, retention_days: float) -> int:
    """Remove arquivo e registro de todo `Replay` mais velho que `retention_days`.
    Retorna quantos foram removidos."""
    output_dir = Path(output_dir)
    cutoff = datetime.now() - timedelta(days=retention_days)

    expirados = session.exec(select(Replay).where(Replay.criado_em < cutoff)).all()
    for replay in expirados:
        for filename in {replay.arquivo_bruto, replay.arquivo_processado}:
            if filename:
                (output_dir / filename).unlink(missing_ok=True)
        session.delete(replay)

    if expirados:
        session.commit()
    return len(expirados)
