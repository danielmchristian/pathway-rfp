import os
from pathlib import Path

# CRITICAL — DB ISOLATION GUARD.
# This file's autouse `_reset_state` fixture issues `TRUNCATE ... CASCADE`
# on 11 tables of whatever DB `SessionLocal` is bound to. If pytest inherits
# the dev `.env`, that's the dev/demo DB — every `make test` wipes restaurants,
# dishes, ingredients, distributors, the lot. (Three confirmed dev-DB wipes
# pre-Loom traced back to this exact path.)
#
# Fix: rebind DATABASE_URL to a dedicated `pathway_test` database BEFORE any
# `from app.*` import, so the cached pydantic Settings + the module-level
# `app.db.engine` both point at the test DB. `pytest_configure` below creates
# the test DB if missing and runs `alembic upgrade head` against it.
#
# Override the default by exporting TEST_DATABASE_URL.
_TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://pathway:pathway@localhost:5432/pathway_test",
)
os.environ["DATABASE_URL"] = _TEST_DB_URL
os.environ["ENV"] = "test"

import asyncio  # noqa: E402

import asyncpg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.pipeline.events import get_bus  # noqa: E402

# Belt-and-suspenders: if anything ever bypasses the override above, refuse
# to load the test suite rather than silently TRUNCATE the dev DB.
_bound_db = str(engine.url).rsplit("/", 1)[-1]
assert _bound_db == "pathway_test", (
    f"FATAL: tests are bound to '{_bound_db}', not 'pathway_test'. "
    f"Refusing to run — this would wipe the dev DB. "
    f"Check the DATABASE_URL override at the top of tests/conftest.py."
)

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def _sync_url(async_url: str) -> str:
    return async_url.replace("postgresql+asyncpg://", "postgresql://")


async def _ensure_test_db() -> None:
    sync = _sync_url(_TEST_DB_URL)
    base, _, dbname = sync.rpartition("/")
    conn = await asyncpg.connect(f"{base}/postgres")
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", dbname)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await conn.close()


def pytest_configure(config: pytest.Config) -> None:
    """Provision pathway_test (CREATE DATABASE if missing + alembic upgrade
    head) before any test runs. Idempotent — no-ops on subsequent runs."""
    asyncio.run(_ensure_test_db())
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _TEST_DB_URL)
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
async def _reset_state():
    """Reset event bus + truncate test-DB tables + dispose engine pool around every test.

    pytest-asyncio runs each test in a fresh event loop. The module-level
    `engine` keeps asyncpg connections in its pool that are bound to the
    *previous* loop; reusing them after that loop is closed raises
    `RuntimeError: ... attached to a different loop`. Disposing on teardown
    forces a clean pool for the next test.

    `llm_usage` is truncated because `traced_call` writes on its own session
    (independent of any test fixture session), so without an autouse truncate
    rows accumulate across tests and pollute downstream /usage assertions.
    """
    get_bus().reset()
    yield
    get_bus().reset()
    async with SessionLocal() as session:
        # CASCADE needed — quotes table has an FK on rfp_emails.
        # Phase 5.1: distributors added because rfp_emails has an FK on
        # distributors; without truncating them too, leftover seed rows
        # from one test bleed into the next test's matcher view.
        await session.execute(
            text(
                "TRUNCATE TABLE llm_usage, rfp_emails, rfp_request_items, "
                "rfp_requests, dish_ingredients, dishes, ingredient_prices, "
                "restaurants, ingredients, distributors "
                "RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session():
    """AsyncSession against the TEST DB; truncates touched tables after each test."""
    async with SessionLocal() as session:
        yield session
        await session.execute(
            text(
                "TRUNCATE TABLE llm_usage, rfp_emails, rfp_request_items, "
                "rfp_requests, dish_ingredients, dishes, ingredient_prices, "
                "restaurants, ingredients, distributors RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
