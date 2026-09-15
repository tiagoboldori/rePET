#!/usr/bin/env python3
"""
generate_camera_envs.py — lê config/cameras.json (fonte única de verdade
sobre quais câmeras existem) e gera um arquivo .env por câmera, no formato
que o unit systemd `replay-capture@.service` espera
(/etc/replay-system/cameras/<quadra_id>.env).

Uso:
    python3 generate_camera_envs.py [--out-dir /etc/replay-system/cameras]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_CAMERAS_FILE = Path(__file__).resolve().parent.parent / "config" / "cameras.json"
DEFAULT_OUT_DIR = Path("/etc/replay-system/cameras")
DEFAULT_BUFFER_ROOT = "/var/replay"
DEFAULT_SEGMENT_TIME = 5


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cameras-file", type=Path, default=DEFAULT_CAMERAS_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--buffer-root", default=DEFAULT_BUFFER_ROOT)
    parser.add_argument("--segment-time", type=int, default=DEFAULT_SEGMENT_TIME)
    args = parser.parse_args()

    cameras = json.loads(args.cameras_file.read_text())
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for cam in cameras:
        quadra_id = cam["quadra_id"]
        env_path = args.out_dir / f"{quadra_id}.env"
        env_path.write_text(
            f"INPUT_URL={cam['input_url']}\n"
            f"BUFFER_ROOT={args.buffer_root}\n"
            f"SEGMENT_TIME={args.segment_time}\n"
        )
        print(f"gerado: {env_path}")

    print(f"\n{len(cameras)} câmeras processadas.")
    print("Pra ativar cada uma:")
    for cam in cameras:
        print(f"  systemctl enable --now replay-capture@{cam['quadra_id']}")


if __name__ == "__main__":
    main()
