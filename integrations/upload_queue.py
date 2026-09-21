"""
upload_queue.py — fila de envio de clipes ao Lara (PT-15,
PLANO_DE_ACAO.md v3 seção 6, itens 3-5). Processa `Replay` com
`lara_status=PENDENTE`: aplica o overlay em cache (mecânico, sem decisão,
ver integrations/overlay.py) e envia via `POST /cameras/{id}/videos`,
idempotente por `external_id` do clipe (`Replay.id`, RNF11).

Nunca roda no caminho do acionamento do botão — o clipe já foi gravado em
disco e servido localmente antes desta fila sequer olhar pra ele (RNF3,
RNF9). Erro de rede/429/5xx deixa o registro `PENDENTE` pra próxima
passada; 422/413 (formato inválido ou limite de servidor) marca `FALHA` e
não retenta — retentar não resolve nenhum dos dois.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from db.models import Quadra, Replay, ReplayLaraStatus
from integrations.lara_client import (
    LaraAuthError,
    LaraClient,
    LaraClientError,
    LaraNotFoundError,
    LaraPayloadTooLargeError,
    LaraRateLimitError,
    LaraServerError,
    LaraValidationError,
)
from integrations.overlay import apply_overlay


def process_pending(session: Session, client: LaraClient, output_dir: Path) -> None:
    pendentes = session.exec(
        select(Replay).where(Replay.lara_status == ReplayLaraStatus.PENDENTE)
    ).all()

    for replay in pendentes:
        _process_one(session, client, output_dir, replay)


def _process_one(
    session: Session, client: LaraClient, output_dir: Path, replay: Replay
) -> None:
    quadra = session.get(Quadra, replay.quadra_id)
    if quadra is None:
        return  # não deveria acontecer (FK formal), mas não trava a fila

    clip_path = output_dir / replay.arquivo_bruto
    if not clip_path.is_file():
        replay.lara_status = ReplayLaraStatus.FALHA
        replay.lara_ultimo_erro = "arquivo bruto não existe mais em disco (retenção local já removeu?)"
        session.add(replay)
        session.commit()
        return

    try:
        send_path = apply_overlay(clip_path, quadra)
        if send_path != clip_path:
            replay.arquivo_com_overlay = send_path.name

        result = client.upload_video(
            external_id=replay.quadra_id,
            file_path=send_path,
            recorded_at=replay.criado_em,
            duration_seconds=round(replay.duracao_segundos),
            clip_external_id=replay.id,
        )
    except (LaraValidationError, LaraPayloadTooLargeError) as exc:
        # não é transitório — retentar não resolve (RNF9: o replay já está
        # disponível localmente, isso só afeta a entrega via Lara)
        replay.lara_status = ReplayLaraStatus.FALHA
        replay.lara_ultimo_erro = str(exc)
        print(f"[lara] envio de '{replay.id}' falhou (não retentável): {exc}")
    except LaraNotFoundError as exc:
        # provável external_id de câmera ainda não cadastrado no Lara —
        # fica PENDENTE, tenta de novo no próximo ciclo (não é bug daqui)
        replay.lara_ultimo_erro = str(exc)
        print(f"[lara] envio de '{replay.id}' com 404 — conferir cadastro no Lara: {exc}")
    except (LaraAuthError, LaraRateLimitError, LaraServerError, LaraClientError) as exc:
        # transitório (token, rede, 429, 5xx) — fica PENDENTE pro próximo ciclo
        replay.lara_ultimo_erro = str(exc)
        print(f"[lara] envio de '{replay.id}' falhou (retentável): {exc}")
    else:
        replay.lara_status = ReplayLaraStatus.ENVIADO
        replay.lara_uuid = result.uuid
        replay.lara_enviado_em = datetime.now()
        replay.lara_ultimo_erro = None

    session.add(replay)
    session.commit()
