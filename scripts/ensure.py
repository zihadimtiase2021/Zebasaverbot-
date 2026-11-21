"""Ask for whatever is still missing (first run / revoked login) and exit. Used by `run.py --background`.

    python3 -m scripts.ensure
"""
from unified_core.setup_wizard import ensure_ready

if __name__ == "__main__":
    ensure_ready(force=False)
