.PHONY: setup check test lint plan cook status

# Pin tooling to Python 3.12 via uv, whatever `python3` happens to be on this machine (macOS ships 3.9).
PY := uv run --quiet --no-project --python 3.12

setup:
	git submodule update --init
	cd upstream && uv sync
	bash scripts/install-git-hooks.sh

test:
	$(PY) --with pytest python -m pytest -q

lint:
	uvx ruff check tacobot tests scripts

plan:
	$(PY) python tacobot/sequencer.py --dry-run

status:
	$(PY) python scripts/claims.py status

check: lint test
	$(PY) python scripts/claims.py check --changed

# Humans only: robot + dream-1-tasks must already be running (see README). Uses upstream's own venv.
cook:
	cd upstream && uv run python ../tacobot/sequencer.py
