"""Run the full interactive setup wizard (every question, current values as defaults).

    python3 run.py --setup        # same thing
    python3 -m scripts.setup
"""
from unified_core.setup_wizard import ensure_ready

if __name__ == "__main__":
    ensure_ready(force=True)
