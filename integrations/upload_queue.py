"""Fila de envio de clipes à plataforma externa.

Processa os `Replay` com `lara_status=PENDENTE`: renderiza (orientação e overlay),
mixa a música e envia via `POST /cameras/{id}/videos`, idempotente pelo `Replay.id`.
Erros de rede, de autenticação, 404, 429, 5xx e falhas locais de ffmpeg deixam o
registro PENDENTE com backoff exponencial por item; 422/413 marcam FALHA, sem retry.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, or_, select

from clipper.clip_generator import ClipGenerationError
from db.models import Quadra, Replay, ReplayLaraStatus
from integrations.audio import AudioApplicationError, apply_audio
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
from integrations.render import RenderApplicationError, render_clip

# Backoff exponencial por item (10s, 20s, 40s...) até o teto; zerado em sucesso ou FALHA.
BACKOFF_BASE_SECONDS = 10
BACKOFF_MAX_SECONDS = 600


def process_pending(
    session: Session,
    client: LaraClient,
    output_dir: Path,
    music_dir: Path | None = None,
    music_volume: float = 0.5,
    music_fade_seconds: float = 1.5,
) -> None:
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
        _process_one(session, client, output_dir, replay, music_dir, music_volume, music_fade_seconds)


def _process_one(
    session: Session,
    client: LaraClient,
    output_dir: Path,
    replay: Replay,
    music_dir: Path | None,
    music_volume: float,
    music_fade_seconds: float,
) -> None:
    quadra = session.get(Quadra, replay.quadra_id)
    if quadra is None:
        return  # não deveria acontecer; não trava a fila

    clip_path = output_dir / replay.arquivo_bruto
    if not clip_path.is_file():
        replay.lara_status = ReplayLaraStatus.FALHA
        replay.lara_ultimo_erro = "arquivo bruto não existe mais em disco (retenção local já removeu?)"
        session.add(replay)
        session.commit()
        return

    # capturado antes de o contador ser zerado ou incrementado, para o log
    tentativa_atual = replay.lara_tentativas + 1

    try:
        send_path = render_clip(clip_path, quadra)
        if send_path != clip_path:
            replay.arquivo_processado = send_path.name

        audio_path = apply_audio(send_path, music_dir, music_volume, music_fade_seconds)
        if audio_path != send_path:
            # apaga o intermediário sem música; só bruto/processado são rastreados
            if send_path != clip_path:
                send_path.unlink(missing_ok=True)
            send_path = audio_path
            replay.arquivo_processado = send_path.name

        result = client.upload_video(
            external_id=replay.quadra_id,
            file_path=send_path,
            recorded_at=replay.criado_em,
            duration_seconds=round(replay.duracao_segundos),
            clip_external_id=replay.id,
        )
    except (
        RenderApplicationError,
        AudioApplicationError,
        ClipGenerationError,
    ) as exc:
        # falha local (ffmpeg/ffprobe): mesmo tratamento, loga e retenta
        _apply_backoff(replay, exc)
        print(f"[lara] processamento local de '{replay.id}' falhou (tentativa {tentativa_atual}, retentável): {exc}")
    except (LaraValidationError, LaraPayloadTooLargeError) as exc:
        # não é transitório; o replay continua disponível localmente
        replay.lara_status = ReplayLaraStatus.FALHA
        replay.lara_ultimo_erro = str(exc)
        replay.lara_tentativas = 0
        replay.lara_proxima_tentativa_em = None
        print(f"[lara] envio de '{replay.id}' falhou (tentativa {tentativa_atual}, não retentável): {exc}")
    except LaraNotFoundError as exc:
        # provável câmera ainda não cadastrada; fica PENDENTE com backoff
        _apply_backoff(replay, exc)
        print(
            f"[lara] envio de '{replay.id}' com 404 (tentativa {tentativa_atual}) "
            f"— conferir cadastro no Lara: {exc}"
        )
    except (LaraAuthError, LaraRateLimitError, LaraServerError, LaraClientError) as exc:
        # transitório: fica PENDENTE até o backoff vencer
        _apply_backoff(replay, exc)
        print(f"[lara] envio de '{replay.id}' falhou (tentativa {tentativa_atual}, retentável): {exc}")
    else:
        replay.lara_status = ReplayLaraStatus.ENVIADO
        replay.lara_uuid = result.uuid
        replay.lara_enviado_em = datetime.now()
        replay.lara_ultimo_erro = None
        replay.lara_tentativas = 0
        replay.lara_proxima_tentativa_em = None
        print(
            f"[lara] envio de '{replay.id}' confirmado na tentativa {tentativa_atual} "
            f"(uuid={result.uuid}, duplicated={result.duplicated})."
        )

    session.add(replay)
    session.commit()


def _apply_backoff(replay: Replay, exc: Exception) -> None:
    replay.lara_ultimo_erro = str(exc)
    replay.lara_tentativas += 1
    delay = min(BACKOFF_BASE_SECONDS * (2 ** (replay.lara_tentativas - 1)), BACKOFF_MAX_SECONDS)
    replay.lara_proxima_tentativa_em = datetime.now() + timedelta(seconds=delay)
