#!/usr/bin/env bash
# One-time setup on macOS / Linux: virtual env + dependencies, and a .env to fill in.
#   bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - set ARM_PORTS (and the LiveKit key/secret if you use LiveKit)."
fi

.venv/bin/python -c "import maker_arm, livekit.rtc, livekit.api, aiohttp; print('setup OK')"
