#!/usr/bin/env sh
# One-command start:  ./run.sh   (first run asks for everything it needs)
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "Python 3.10+ is not installed. Install it (e.g. sudo apt install python3 python3-venv python3-pip)."; exit 1; }
exec "$PY" run.py "$@"
