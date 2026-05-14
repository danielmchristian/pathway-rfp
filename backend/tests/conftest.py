import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.main import app
from app.pipeline.events import get_bus


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
async def _reset_state():
    """Reset event bus + truncate llm_usage + dispose the global engine pool around every test.

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
    """AsyncSession against the dev DB; truncates touched tables after each test."""
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
