.DEFAULT_GOAL := help
SHELL := /bin/bash

BACKEND := backend
UV := uv
COMPOSE := docker compose

FRONTEND := frontend

.PHONY: help setup db-up db-down db-wait db-migrate db-revision db-downgrade dev test lint fmt clean \
	demo demo-reset poll finalize frontend-dev frontend-build

help:
	@echo "Targets:"
	@echo "  setup         Install backend deps with uv (creates .venv + uv.lock)"
	@echo "  db-up         Start Postgres (docker compose) and wait for healthy"
	@echo "  db-down       Stop Postgres"
	@echo "  db-migrate    Apply Alembic migrations to head"
	@echo "  db-revision   Autogenerate a new migration: make db-revision m='message'"
	@echo "  db-downgrade  Downgrade one revision (or set rev=...)"
	@echo "  dev           Run FastAPI with reload (depends on db-up)"
	@echo "  test          Run pytest"
	@echo "  lint          Run ruff check + format check"
	@echo "  fmt           Run ruff format (writes)"
	@echo "  clean         Remove __pycache__, .pytest_cache, .ruff_cache"
	@echo "  demo          End-to-end demo seed (idempotent; no-op if RFP exists)"
	@echo "  demo-reset    Wipe demo content rows + re-seed (preserves distributors + schema)"
	@echo "  poll          Poll inbox for the most recent RFP"
	@echo "  finalize      Force-compute the recommendation for the most recent RFP"
	@echo "  frontend-dev  Run the Next.js dev server"
	@echo "  frontend-build  Build the Next.js production bundle"

setup:
	cd $(BACKEND) && $(UV) sync

db-up:
	$(COMPOSE) up -d db
	@$(MAKE) db-wait

db-down:
	$(COMPOSE) down

db-wait:
	@echo "Waiting for Postgres to be healthy..."
	@for i in $$(seq 1 30); do \
		status=$$($(COMPOSE) ps --format json db 2>/dev/null | grep -o '"Health":"[^"]*"' | head -1); \
		if echo "$$status" | grep -q healthy; then echo "Postgres healthy."; exit 0; fi; \
		sleep 1; \
	done; \
	echo "Postgres did not become healthy in time"; exit 1

db-migrate:
	cd $(BACKEND) && $(UV) run alembic upgrade head

db-revision:
	@if [ -z "$(m)" ]; then echo "Usage: make db-revision m='your message'"; exit 1; fi
	cd $(BACKEND) && $(UV) run alembic revision --autogenerate -m "$(m)"

db-downgrade:
	cd $(BACKEND) && $(UV) run alembic downgrade $${rev:--1}

dev:
	cd $(BACKEND) && $(UV) run uvicorn app.main:app --reload --port 8000

test:
	cd $(BACKEND) && $(UV) run pytest -q

lint:
	cd $(BACKEND) && $(UV) run ruff check . && $(UV) run ruff format --check .

fmt:
	cd $(BACKEND) && $(UV) run ruff format .

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache

# ---------------------------------------------------------------------------
# Phase 7 — demo orchestration
# ---------------------------------------------------------------------------
demo:
	cd $(BACKEND) && $(UV) run python -m app.cli run-demo

demo-reset:
	cd $(BACKEND) && $(UV) run python -m app.cli run-demo --reset-data --yes

poll:
	cd $(BACKEND) && $(UV) run python -m app.cli poll-latest

finalize:
	cd $(BACKEND) && $(UV) run python -m app.cli finalize-latest

# ---------------------------------------------------------------------------
# Phase 7 — frontend
# ---------------------------------------------------------------------------
frontend-dev:
	cd $(FRONTEND) && npm run dev

frontend-build:
	cd $(FRONTEND) && npm run build
