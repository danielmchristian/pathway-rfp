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
    """Reset event bus + dispose the global engine pool around every test.

    pytest-asyncio runs each test in a fresh event loop. The module-level
    `engine` keeps asyncpg connections in its pool that are bound to the
    *previous* loop; reusing them after that loop is closed raises
    `RuntimeError: ... attached to a different loop`. Disposing on teardown
    forces a clean pool for the next test.
    """
    get_bus().reset()
    yield
    get_bus().reset()
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session():
    """AsyncSession against the dev DB; truncates touched tables after each test."""
    async with SessionLocal() as session:
        yield session
        await session.execute(
            text(
                "TRUNCATE TABLE llm_usage, dish_ingredients, dishes, ingredient_prices, "
                "restaurants, ingredients RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
