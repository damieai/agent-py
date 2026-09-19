.PHONY: install check test init api demo-repair demo-incident migrate worker dispatcher frontend
install:
	uv sync --extra dev --frozen
check:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
test:
	.venv/bin/pytest -q
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
