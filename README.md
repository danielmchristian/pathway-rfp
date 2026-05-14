# Pathway RFP

End-to-end pipeline that ingests a restaurant menu and produces distributor RFP quotes with a recommendation. See [`docs/spec.md`](docs/spec.md) for the full system spec.

## Phase 1 — Foundation

Backend scaffold only: FastAPI + SQLAlchemy 2 (async) + Alembic, Postgres 16 via Docker Compose. No business logic yet.

### Prerequisites

- Python 3.11 or 3.12
- [`uv`](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Docker Desktop with WSL integration enabled (or a local Docker daemon)

### Quickstart

```bash
cp .env.example .env
make setup        # uv sync
make db-up        # starts Postgres in Docker
make db-migrate   # alembic upgrade head
make dev          # uvicorn on :8000
```

Then in another shell:

```bash
curl localhost:8000/health
# {"status":"ok","db":"ok","version":"0.1.0"}
```

### Make targets

| Target | What it does |
|---|---|
| `setup` | `uv sync` — install backend deps, create `.venv`, write `uv.lock` |
| `db-up` / `db-down` | Start / stop Postgres via docker compose |
| `db-migrate` | `alembic upgrade head` |
| `db-revision m='msg'` | Autogenerate a new migration |
| `db-downgrade` | Downgrade one revision (or `rev=base` for full rollback) |
| `dev` | Run FastAPI with `--reload` on :8000 |
| `test` | `pytest` |
| `lint` | `ruff check` + `ruff format --check` |
| `fmt` | `ruff format` (writes) |

### Layout

```
backend/         FastAPI app + SQLAlchemy models + Alembic
data/menus/      Pinned restaurant menus (added in Phase 2)
docs/spec.md     Full project spec — updated as decisions evolve
docker-compose.yml
.env.example
Makefile
```
