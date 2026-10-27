"""Фоновый помощник для мгновенных скриншотов экрана с TCC-правами родительского приложения."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

SOCK_PATH = "/tmp/station-screen-helper.sock"
SHOT_PATH = "/tmp/assistant_screen.jpg"


def capture_once() -> bool:
    out = Path(SHOT_PATH)
    out.unlink(missing_ok=True)
    res = subprocess.run(
        ["/usr/sbin/screencapture", "-x", "-t", "jpg", SHOT_PATH],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if res.returncode != 0 or not out.is_file() or out.stat().st_size < 1000:
        return False
    subprocess.run(
        ["/usr/bin/sips", "-Z", "1440", SHOT_PATH],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return True


def main() -> None:
    try:
        os.unlink(SOCK_PATH)
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o666)
    srv.listen(5)
    while True:
        conn, _ = srv.accept()
        with conn:
            ok = capture_once()
            conn.sendall(b"OK\n" if ok else b"ERR\n")


if __name__ == "__main__":
    main()
