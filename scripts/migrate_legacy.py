"""Copy files from the old project folder into ./data (session, databases, cookies, .env).

    python -m scripts.migrate_legacy /path/to/old/zebasaverbot
"""
import shutil
import sys
from pathlib import Path

from unified_core.config import settings
from unified_core.paths import ENV_FILE


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    old = Path(sys.argv[1]).expanduser()
    settings.ensure_dirs()
    copied = []
    for pattern in ("*.session", "unified_database.db", "accounts.db", "cookies.txt"):
        for f in old.glob(pattern):
            shutil.copy2(f, settings.DATA_DIR / f.name)
            copied.append(f.name)
    env_old, env_new = old / ".env", ENV_FILE
    if env_old.is_file() and not env_new.exists():
        shutil.copy2(env_old, env_new)
        copied.append(".env (missing values are asked for on the next  python3 run.py)")
    print("Copied:", ", ".join(copied) or "nothing found")


if __name__ == "__main__":
    main()
