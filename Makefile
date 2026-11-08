.PHONY: setup check test lint plan cook status plan-astra cook-astra prompt

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
# Dream baseline: the fixed tasks.toml -> Dream-1.
cook:
	cd upstream && uv run python ../tacobot/sequencer.py

# Free-text console: type any instruction and Dream-1 runs it (manual, no Astra, no tasks.toml).
prompt:
	cd upstream && uv run python ../tacobot/dream_prompt.py

# Astra+Dream: Astra plans from the overhead camera (steps + contact markers), Dream-1 executes each
# step, Astra verifies before the next. Run this AFTER checking the Dream baseline above.
plan-astra:
	cd upstream && uv run python ../tacobot/astra_cook.py --dry-run

cook-astra:
	cd upstream && uv run python ../tacobot/astra_cook.py --goal "make a quesadilla"
