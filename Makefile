.PHONY: setup check test lint plan cook status

setup:
	git submodule update --init
	cd upstream && uv sync
	bash scripts/install-git-hooks.sh

test:
	python3 -m pytest -q

lint:
	ruff check tacobot tests scripts

plan:
	python3 tacobot/sequencer.py --dry-run

status:
	python3 scripts/claims.py status

check: lint test
	python3 scripts/claims.py check --changed

# Humans only: robot + dream-1-tasks must already be running (see README).
cook:
	cd upstream && uv run python ../tacobot/sequencer.py
