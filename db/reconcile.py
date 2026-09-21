"""
reconcile.py — reconciliação idempotente entre disco e banco (PT-03,
M4 do PLANO_DE_ACAO.md).

Motivo de existir: clipes já existiam em disco antes da introdução do
banco (PT-01/PT-02), e o disco continua sendo a fonte de verdade final
(RNF6) — se o banco for perdido/recriado, o conteúdo já publicado em
/clips não pode sumir da listagem/consulta. Esta rotina varre
`output_dir` e insere só os registros de `Replay` ainda ausentes; nunca
apaga nem sobrescreve um registro já existente (não é sincronização
bidirecional, só recuperação de ausência).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from sqlmodel import Session

from clipper.clip_generator import ClipGenerationError, probe_duration_seconds
from db.models import Quadra, Replay

# Mesmo formato de nome gerado por clip_generator.generate_clip:
# f"{quadra_id}_{ts}.mp4", ts = "%Y%m%d%H%M%S". quadra_id nunca leva "_"
# na convenção atual (usa "-", ex. "loc1-quadra1"), então casar o último
# "_" antes de 14 dígitos é seguro.
CLIP_RE = re.compile(r"^(?P<quadra_id>.+)_(?P<ts>\d{14})\.mp4$")


def reconcile_replays(session: Session, output_dir: Path) -> int:
    """Varre `output_dir` por clipes (`*.mp4`) sem registro em `Replay` e
    insere o que faltar. Idempotente: rodar de novo sobre o mesmo
    diretório não duplica nem altera registros já existentes. Retorna o
    número de registros inseridos.

    Best-effort por arquivo: um clipe corrompido (ffprobe falha) ou de
    uma quadra desconhecida (nunca cadastrada ou removida de
    cameras.json) é pulado com aviso, em vez de interromper a varredura
    inteira — mesmo princípio de RNF9 (uma falha isolada não pode travar
    o resto do sistema).
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
