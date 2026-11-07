#!/usr/bin/env bash
# Run any kit script with the kit's own venv (macOS / Linux). Sets it up first if needed.
#   ./run.sh                          -> web app with the arms from .env
#   ./run.sh web_app.py --sim         -> web app with simulated arms
#   ./run.sh first_move.py --list-ports
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || bash setup.sh
if [ $# -eq 0 ]; then
  set -- web_app.py
fi
exec .venv/bin/python "$@"
