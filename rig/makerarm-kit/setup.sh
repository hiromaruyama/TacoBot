#!/usr/bin/env bash
# One-time setup on macOS / Linux: Python 3.10+ virtual env + dependencies, and a .env to fill in.
#   bash setup.sh                 (or: PYTHON=python3.12 bash setup.sh)
set -euo pipefail
cd "$(dirname "$0")"

# Pick a Python >= 3.10. macOS's built-in python3 is often 3.9, so prefer a Homebrew one.
PY=${PYTHON:-}
if [ -z "$PY" ]; then
  for cand in python3.12 python3.13 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1 && \
       "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY=$cand; break
    fi
  done
fi
if [ -z "$PY" ]; then
  echo "Python 3.10+ not found."
  if [ "$(uname)" = "Darwin" ]; then
    echo "Install it with Homebrew:  brew install python@3.12   (https://brew.sh)"
  else
    echo "Install python3.10+ and python3-venv with your package manager."
  fi
  exit 1
fi
echo "using $($PY --version) ($PY)"

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - set ARM_PORTS (and the LiveKit key/secret if you use LiveKit)."
fi

.venv/bin/python -c "import maker_arm, livekit.rtc, livekit.api, aiohttp; print('setup OK')"
echo
echo "Next:  ./run.sh first_move.py --list-ports     (find your arms' ports)"
echo "       ./run.sh web_app.py --sim               (try the web app without hardware)"
