"""Filesystem locations shared by the app and the setup wizard (stdlib only)."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Where the generated configuration lives. Docker sets ZEBA_ENV_FILE=/data/.env so it persists.
ENV_FILE = Path(os.environ.get("ZEBA_ENV_FILE") or BASE_DIR / ".env")


def data_dir() -> Path:
    """DATA_DIR from the environment / env file, resolved against BASE_DIR."""
    raw = os.environ.get("DATA_DIR", "")
    if not raw and ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("DATA_DIR="):
                raw = line.split("=", 1)[1].strip().strip("\"'")
    p = Path(raw) if raw else BASE_DIR / "data"
    return p if p.is_absolute() else BASE_DIR / p
