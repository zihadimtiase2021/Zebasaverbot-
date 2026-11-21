"""Temp-file hygiene: nothing downloaded may stay on disk after it has been handled."""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger("zeba.cleanup")


def dir_size(path: Path) -> int:
    total = 0
    try:
        for p in Path(path).rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def safe_rmtree(path: Path | str, attempts: int = 3) -> bool:
    """Delete a directory tree. Retries (Windows keeps files locked briefly). Never raises.

    Returns True when the path no longer exists.
    """
    p = Path(path)
    for i in range(attempts):
        if not p.exists():
            return True
        shutil.rmtree(p, ignore_errors=True)
        if not p.exists():
            return True
        time.sleep(0.3 * (i + 1))
    log.warning("could not fully delete %s", p)
    return not p.exists()


def purge_tmp(tmp_dir: Path | str, older_than: float = 0.0) -> int:
    """Remove entries in `tmp_dir` whose last change is older than `older_than` seconds
    (0 = remove everything). Returns the number of entries removed."""
    root = Path(tmp_dir)
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    for entry in root.iterdir():
        try:
            if older_than and now - entry.stat().st_mtime < older_than:
                continue
            if entry.is_dir() and not entry.is_symlink():
                ok = safe_rmtree(entry)
            else:
                entry.unlink(missing_ok=True)
                ok = True
            removed += 1 if ok else 0
        except OSError as exc:
            log.debug("purge skipped %s: %s", entry, exc)
    return removed
