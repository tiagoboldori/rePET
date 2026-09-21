"""
local_retention.py — retenção local dos clipes finais (S3, PLANO_DE_ACAO.md
v3 seção 7.2/RNF5). **Não é a entrega oficial ao sócio** — essa é do Lara,
que apaga sozinho em 7 dias. Isso aqui é só pra não deixar `OUTPUT_DIR`
crescer sem limite: a página de teste interna (`GET /quadra/{id}`) e os
endpoints `GET /api/replays/...` só existem pra depuração/validação local,
não pro consumo do sócio (esse é o link que o Lara devolve no envio).

Baseado em `Replay.criado_em` (banco), não em mtime de arquivo — mesma
fonte de verdade usada em todo o resto do sistema (idade real da
gravação). Roda por idade, sem checar `lara_status`: um replay que nunca
foi enviado com sucesso ao Lara (ex.: `external_id` nunca cadastrado lá,
ficando preso na fila) ainda assim expira depois de
`LOCAL_RAW_RETENTION_DAYS` — a fila de envio já trata "arquivo sumiu"
como falha não-crítica (RNF9, ver integrations/upload_queue.py), não como
bug.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from db.models import Replay


def purge_expired_replays(session: Session, output_dir: Path, retention_days: float) -> int:
    """Remove (arquivo + registro) todo `Replay` mais velho que
    `retention_days`. Retorna quantos foram removidos. Idempotente: nada
    a fazer numa segunda passada até o próximo replay expirar."""
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
