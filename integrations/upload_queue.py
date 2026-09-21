"""
upload_queue.py — fila de envio de clipes ao Lara (PT-15,
PLANO_DE_ACAO.md v3 seção 6, itens 3-5). Processa `Replay` com
`lara_status=PENDENTE`: aplica o overlay em cache (mecânico, sem decisão,
ver integrations/overlay.py) e envia via `POST /cameras/{id}/videos`,
idempotente por `external_id` do clipe (`Replay.id`, RNF11).

Nunca roda no caminho do acionamento do botão — o clipe já foi gravado em
disco e servido localmente antes desta fila sequer olhar pra ele (RNF3,
RNF9). Erro de rede/401/403/404/429/5xx (transitório ou dependente de
ação externa — token, cadastro, rate limit, instabilidade do Lara) deixa
o registro `PENDENTE`, mas só é retentado depois de um backoff exponencial
por item (`lara_tentativas`/`lara_proxima_tentativa_em`, ver abaixo) — o
prompt do Lara pede isso explicitamente ("retente com backoff", "429 ->
backoff"), e sem isso uma indisponibilidade prolongada bateria na API do
Lara a cada passada da fila (default 10s) pra cada replay pendente.
422/413 (formato inválido ou limite de servidor) marca `FALHA` e não
retenta — retentar não resolve nenhum dos dois.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, or_, select

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

# Backoff exponencial por item: 10s, 20s, 40s, ... até o teto de 10min.
# Zerado em sucesso ou falha definitiva (ver _process_one).
BACKOFF_BASE_SECONDS = 10
BACKOFF_MAX_SECONDS = 600


def process_pending(session: Session, client: LaraClient, output_dir: Path) -> None:
    now = datetime.now()
    pendentes = session.exec(
        select(Replay).where(
            Replay.lara_status == ReplayLaraStatus.PENDENTE,
            or_(
                Replay.lara_proxima_tentativa_em.is_(None),
                Replay.lara_proxima_tentativa_em <= now,
            ),
        )
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
        replay.lara_tentativas = 0
        replay.lara_proxima_tentativa_em = None
        print(f"[lara] envio de '{replay.id}' falhou (não retentável): {exc}")
    except LaraNotFoundError as exc:
        # provável external_id de câmera ainda não cadastrado no Lara —
        # fica PENDENTE, com o mesmo backoff dos erros transitórios abaixo
        # (não é bug daqui, mas também não adianta bater no Lara a cada
        # poucos segundos enquanto o cadastro não sai)
        _apply_backoff(replay, exc)
        print(
            f"[lara] envio de '{replay.id}' com 404 (tentativa {replay.lara_tentativas}) "
            f"— conferir cadastro no Lara: {exc}"
        )
    except (LaraAuthError, LaraRateLimitError, LaraServerError, LaraClientError) as exc:
        # transitório (token, rede, 429, 5xx) — fica PENDENTE, retentado só
        # depois do backoff (ver _apply_backoff)
        _apply_backoff(replay, exc)
        print(f"[lara] envio de '{replay.id}' falhou (tentativa {replay.lara_tentativas}, retentável): {exc}")
    else:
        replay.lara_status = ReplayLaraStatus.ENVIADO
        replay.lara_uuid = result.uuid
        replay.lara_enviado_em = datetime.now()
        replay.lara_ultimo_erro = None
        replay.lara_tentativas = 0
        replay.lara_proxima_tentativa_em = None
        print(f"[lara] envio de '{replay.id}' confirmado (uuid={result.uuid}, duplicated={result.duplicated}).")

    session.add(replay)
    session.commit()


def _apply_backoff(replay: Replay, exc: Exception) -> None:
    replay.lara_ultimo_erro = str(exc)
    replay.lara_tentativas += 1
    delay = min(BACKOFF_BASE_SECONDS * (2 ** (replay.lara_tentativas - 1)), BACKOFF_MAX_SECONDS)
    replay.lara_proxima_tentativa_em = datetime.now() + timedelta(seconds=delay)
