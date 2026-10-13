"""Cliente FTP do servidor de arquivos do clube (destino dos clipes).

Só fala com a rede; as decisões ficam em upload_queue.py. O upload vai para um
arquivo `.part` e é renomeado no fim, para o Lara nunca ler um clipe pela metade.
"""
from __future__ import annotations

import ftplib
from datetime import datetime
from pathlib import Path, PurePosixPath


class FtpClientError(RuntimeError):
    """Qualquer falha de conexão, login, permissão ou transferência no FTP."""


def build_remote_path(quadra_id: str, recorded_at: datetime, clip_id: str) -> str:
    """Caminho do clipe relativo à pasta base: <quadra>/<AAAA-MM-DD>/<HHh00>/<id>.mp4.
    `recorded_at` é o horário local do aperto do botão. Sem ':' no nome da pasta da
    hora (Windows e FTP recusam)."""
    return str(
        PurePosixPath(quadra_id)
        / recorded_at.strftime("%Y-%m-%d")
        / recorded_at.strftime("%Hh00")
        / f"{clip_id}.mp4"
    )


class FtpClient:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        base_dir: str = "Lara/Repet",
        timeout: float = 60.0,
    ):
        if not host:
            raise ValueError("FTP_HOST não configurado.")
        if not user or not password:
            raise ValueError("FTP_USER/FTP_PASSWORD não configurados.")
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._base_dir = base_dir.strip("/")
        self._timeout = timeout

    def _connect(self) -> ftplib.FTP:
        ftp = ftplib.FTP()
        try:
            ftp.connect(self._host, self._port, timeout=self._timeout)
            ftp.login(self._user, self._password)
        except (ftplib.all_errors, OSError) as exc:
            raise FtpClientError(
                f"Falha ao conectar/autenticar no FTP {self._host}:{self._port}: {exc}"
            ) from exc
        return ftp

    @staticmethod
    def _enter_dir(ftp: ftplib.FTP, parts: list[str]) -> None:
        """Entra na pasta, criando cada nível que faltar."""
        for part in parts:
            try:
                ftp.cwd(part)
            except ftplib.error_perm:
                ftp.mkd(part)
                ftp.cwd(part)

    def upload(self, local_path: Path, remote_rel_path: str) -> str:
        """Grava `local_path` em <base_dir>/<remote_rel_path>, via `.part` + rename.
        Devolve `remote_rel_path` (o que vai no aviso ao Lara)."""
        rel = PurePosixPath(remote_rel_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise FtpClientError(f"Caminho remoto inválido: {remote_rel_path}")
        folders = [p for p in self._base_dir.split("/") if p] + list(rel.parent.parts)
        tmp_name = rel.name + ".part"
        try:
            ftp = self._connect()
            try:
                self._enter_dir(ftp, folders)
                with local_path.open("rb") as fh:
                    ftp.storbinary(f"STOR {tmp_name}", fh)
                try:
                    ftp.delete(rel.name)  # retry de um envio anterior interrompido
                except ftplib.error_perm:
                    pass
                ftp.rename(tmp_name, rel.name)
            finally:
                try:
                    ftp.quit()
                except (ftplib.all_errors, OSError):
                    ftp.close()
        except FtpClientError:
            raise
        except (ftplib.all_errors, OSError) as exc:
            raise FtpClientError(f"Falha no upload FTP de '{rel}': {exc}") from exc
        return str(rel)

    def check_write_access(self) -> str:
        """Diagnóstico: conecta, garante <base_dir> e grava/apaga um arquivo
        temporário nela. Devolve uma descrição do que foi verificado."""
        probe = f".diag_{datetime.now():%Y%m%d%H%M%S}.part"
        try:
            ftp = self._connect()
            try:
                self._enter_dir(ftp, [p for p in self._base_dir.split("/") if p])
                from io import BytesIO

                ftp.storbinary(f"STOR {probe}", BytesIO(b"diag"))
                ftp.delete(probe)
            finally:
                try:
                    ftp.quit()
                except (ftplib.all_errors, OSError):
                    ftp.close()
        except FtpClientError:
            raise
        except (ftplib.all_errors, OSError) as exc:
            raise FtpClientError(f"Sem escrita em '{self._base_dir}': {exc}") from exc
        return f"conexão ok e escrita permitida em '{self._base_dir}/'"
