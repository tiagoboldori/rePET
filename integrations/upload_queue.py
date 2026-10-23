"""Fila de envio de clipes à plataforma externa.

Processa os `Replay` com `lara_status=PENDENTE` em duas etapas: (1) renderiza
(orientação e overlay), mixa a música e grava o clipe no FTP do clube; (2) avisa o
Lara com o caminho via `POST /cameras/{id}/videos`, idempotente pelo `Replay.id`.
Concluída a etapa 1, `Replay.lara_ftp_path` fica gravado e só o aviso é retentado.
Erros de FTP, de rede, de autenticação, 404, 429, 5xx e falhas locais de ffmpeg
deixam o registro PENDENTE com backoff exponencial por item; 422 marca FALHA, sem retry.
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
    LaraRateLimitError,
    LaraServerError,
    LaraValidationError,
)
from integrations.ftp_client import FtpClient, FtpClientError, build_remote_path
from integrations.render import RenderApplicationError, render_clip

# Backoff exponencial por item (10s, 20s, 40s...) até o teto; zerado em sucesso ou FALHA.
BACKOFF_BASE_SECONDS = 10
BACKOFF_MAX_SECONDS = 600


def process_pending(
    session: Session,
    client: LaraClient,
    ftp: FtpClient,
    output_dir: Path,
    music_dir: Path | None = None,
    music_volume: float = 0.4,
    music_fade_seconds: float = 1.5,
    ambient_volume: float = 0.4,
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
        _process_one(
            session, client, ftp, output_dir, replay,
            music_dir, music_volume, music_fade_seconds, ambient_volume,
        )


def _process_one(
    session: Session,
    client: LaraClient,
    ftp: FtpClient,
    output_dir: Path,
    replay: Replay,
    music_dir: Path | None,
    music_volume: float,
    music_fade_seconds: float,
    ambient_volume: float,
) -> None:
    quadra = session.get(Quadra, replay.quadra_id)
    if quadra is None:
        return  # não deveria acontecer; não trava a fila

    # capturado antes de o contador ser zerado ou incrementado, para o log
    tentativa_atual = replay.lara_tentativas + 1

    try:
        if not replay.lara_ftp_path:
            clip_path = output_dir / replay.arquivo_bruto
            if not clip_path.is_file():
                replay.lara_status = ReplayLaraStatus.FALHA
                replay.lara_ultimo_erro = "arquivo bruto não existe mais em disco (retenção local já removeu?)"
                session.add(replay)
                session.commit()
                return

            send_path = render_clip(clip_path, quadra)
            if send_path != clip_path:
                replay.arquivo_processado = send_path.name

            audio_path = apply_audio(
                send_path, music_dir, music_volume, music_fade_seconds, ambient_volume,
            )
            if audio_path != send_path:
                # apaga o intermediário sem música; só bruto/processado são rastreados
                if send_path != clip_path:
                    send_path.unlink(missing_ok=True)
                send_path = audio_path
                replay.arquivo_processado = send_path.name

            replay.lara_ftp_path = ftp.upload(
                send_path, build_remote_path(replay.quadra_id, replay.criado_em, replay.id)
            )
            # persiste já: se o aviso falhar, o retry não refaz render nem upload
            session.add(replay)
            session.commit()
            print(f"[lara] clipe '{replay.id}' gravado no FTP em '{replay.lara_ftp_path}'.")

        result = client.notify_video(
            external_id=replay.quadra_id,
            path=replay.lara_ftp_path,
            recorded_at=replay.criado_em,
            duration_seconds=round(replay.duracao_segundos),
            clip_external_id=replay.id,
        )
    except (
        RenderApplicationError,
        AudioApplicationError,
        ClipGenerationError,
        FtpClientError,
    ) as exc:
        # falha local (ffmpeg/ffprobe) ou do FTP: mesmo tratamento, loga e retenta
        _apply_backoff(replay, exc)
        print(f"[lara] processamento local/FTP de '{replay.id}' falhou (tentativa {tentativa_atual}, retentável): {exc}")
    except LaraValidationError as exc:
        # não é transitório; o replay continua disponível localmente
        replay.lara_status = ReplayLaraStatus.FALHA
        replay.lara_ultimo_erro = str(exc)
        replay.lara_tentativas = 0
        replay.lara_proxima_tentativa_em = None
        print(f"[lara] aviso de '{replay.id}' (path={replay.lara_ftp_path}) falhou (tentativa {tentativa_atual}, não retentável): {exc}")
    except LaraNotFoundError as exc:
        # provável câmera ainda não cadastrada; fica PENDENTE com backoff
        _apply_backoff(replay, exc)
        print(
            f"[lara] aviso de '{replay.id}' com 404 (tentativa {tentativa_atual}) "
            f"— conferir cadastro no Lara: {exc}"
        )
    except (LaraAuthError, LaraRateLimitError, LaraServerError, LaraClientError) as exc:
        # transitório: fica PENDENTE até o backoff vencer
        _apply_backoff(replay, exc)
        print(f"[lara] aviso de '{replay.id}' (path={replay.lara_ftp_path}) falhou (tentativa {tentativa_atual}, retentável): {exc}")
    else:
        replay.lara_status = ReplayLaraStatus.ENVIADO
        replay.lara_uuid = result.uuid
        replay.lara_enviado_em = datetime.now()
        replay.lara_ultimo_erro = None
        replay.lara_tentativas = 0
        replay.lara_proxima_tentativa_em = None
        print(
            f"[lara] aviso de '{replay.id}' confirmado na tentativa {tentativa_atual} "
            f"(path={replay.lara_ftp_path}, uuid={result.uuid}, "
            f"duplicated={result.duplicated}, lara_path={result.path})."
        )

    session.add(replay)
    session.commit()


def _apply_backoff(replay: Replay, exc: Exception) -> None:
    replay.lara_ultimo_erro = str(exc)
    replay.lara_tentativas += 1
    delay = min(BACKOFF_BASE_SECONDS * (2 ** (replay.lara_tentativas - 1)), BACKOFF_MAX_SECONDS)
    replay.lara_proxima_tentativa_em = datetime.now() + timedelta(seconds=delay)
