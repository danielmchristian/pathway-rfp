from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.distributor import Distributor
from app.models.restaurant import Restaurant
from app.services.distributor_discovery import (
    SOURCE_SEED,
    discover_distributors,
)


@pytest.mark.asyncio
async def test_seed_load_inserts_all_records(db_session) -> None:
    restaurant = Restaurant(name="Test")
    db_session.add(restaurant)
    await db_session.commit()
    await db_session.refresh(restaurant)

    with patch("app.services.distributor_discovery.settings.google_places_api_key", ""):
        result = await discover_distributors(restaurant_id=restaurant.id)

    assert result.seed_loaded >= 8
    assert result.places_found == 0
    assert result.duplicates_merged == 0

    rows = (await db_session.execute(select(Distributor))).scalars().all()
    assert len(rows) >= 8
    sources = {r.source for r in rows}
    assert sources == {SOURCE_SEED}
    names = {r.name for r in rows}
    assert "Carolina Fresh Produce Co." in names
    assert "Tidewater Seafood Distributors" in names
    # Sanity: lat/long present
    assert all(r.latitude is not None and r.longitude is not None for r in rows)


@pytest.mark.asyncio
async def test_seed_load_is_idempotent(db_session) -> None:
    restaurant = Restaurant(name="Test")
    db_session.add(restaurant)
    await db_session.commit()
    await db_session.refresh(restaurant)

    with patch("app.services.distributor_discovery.settings.google_places_api_key", ""):
        first = await discover_distributors(restaurant_id=restaurant.id)
        second = await discover_distributors(restaurant_id=restaurant.id)

    assert first.total_distributors == second.total_distributors
    rows = (await db_session.execute(select(Distributor))).scalars().all()
    # No duplicates by name
    assert len({r.name for r in rows}) == len(rows)
