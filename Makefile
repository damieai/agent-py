.PHONY: install check test evaluation-gate init api demo-repair demo-incident migrate worker dispatcher frontend
install:
	uv sync --extra dev --frozen
check:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
test:
	.venv/bin/pytest -q
evaluation-gate:
	.venv/bin/python scripts/check_evaluation_gate.py
init:
	.venv/bin/agent-py init
api:
	.venv/bin/agent-py api
demo-repair:
	.venv/bin/agent-py demo --kind repair --auto-approve-simulation
demo-incident:
	.venv/bin/agent-py demo --kind incident --auto-approve-simulation
migrate:
	.venv/bin/alembic upgrade head
worker:
	.venv/bin/agent-py worker
dispatcher:
	.venv/bin/agent-py dispatcher --tenant demo
frontend:
	npm --prefix web run dev

.PHONY: formal-check
formal-check:
	.venv/bin/python scripts/check_formal.py

.PHONY: browser-test
browser-test:
	npm --prefix web run test:e2e
