"""Recria no banco os registros de `Replay` dos clipes que existem em disco.

Só insere o que falta; nunca altera nem apaga registros existentes. Serve
para recuperar o índice se o banco for perdido.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from sqlmodel import Session

from clipper.clip_generator import ClipGenerationError, probe_duration_seconds
from db.models import Quadra, Replay

# Nome gerado por clip_generator.generate_clip: <quadra_id>_<%Y%m%d%H%M%S>.mp4
CLIP_RE = re.compile(r"^(?P<quadra_id>.+)_(?P<ts>\d{14})\.mp4$")


def reconcile_replays(session: Session, output_dir: Path) -> int:
    """Insere um `Replay` para cada `*.mp4` de `output_dir` sem registro.
    Retorna quantos foram inseridos.

    Clipes corrompidos ou de quadra desconhecida são pulados com aviso,
    sem interromper a varredura.
    """
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return 0

    inserted = 0
    for path in sorted(output_dir.glob("*.mp4")):
        m = CLIP_RE.match(path.name)
        if not m:
            continue

        replay_id = path.stem
        if session.get(Replay, replay_id) is not None:
            continue

        quadra_id = m.group("quadra_id")
        if session.get(Quadra, quadra_id) is None:
            print(
                f"[reconcile] aviso: '{path.name}' pertence a quadra "
                f"'{quadra_id}' desconhecida (não está em cameras.json) — pulando.",
                file=sys.stderr,
            )
            continue

        try:
            duracao = probe_duration_seconds(path)
        except ClipGenerationError as exc:
            print(
                f"[reconcile] aviso: falha ao sondar '{path.name}' via ffprobe "
                f"(arquivo corrompido/incompleto?) — pulando: {exc}",
                file=sys.stderr,
            )
            continue

        criado_em = datetime.strptime(m.group("ts"), "%Y%m%d%H%M%S")
        session.add(
            Replay(
                id=replay_id,
                quadra_id=quadra_id,
                arquivo_bruto=path.name,
                criado_em=criado_em,
                duracao_segundos=duracao,
                tamanho_bytes=path.stat().st_size,
            )
        )
        inserted += 1

    if inserted:
        session.commit()
    return inserted
