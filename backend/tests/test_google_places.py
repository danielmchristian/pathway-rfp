from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import select

from app.models.distributor import Distributor
from app.models.restaurant import Restaurant
from app.services.distributor_discovery import (
    SOURCE_MERGED,
    SOURCE_PLACES,
    SOURCE_SEED,
    discover_distributors,
)


def _fake_classify_response(decisions):
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="classify_distributor_candidates",
                input={"decisions": decisions},
            )
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=400,
            output_tokens=80,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


@pytest.mark.asyncio
async def test_no_key_skips_places_branch(db_session) -> None:
    restaurant = Restaurant(name="R", latitude=35.18, longitude=-80.83)
    db_session.add(restaurant)
    await db_session.commit()
    await db_session.refresh(restaurant)

    with patch("app.services.distributor_discovery.settings.google_places_api_key", ""):
        result = await discover_distributors(restaurant_id=restaurant.id)

    assert result.places_found == 0
    assert result.duplicates_merged == 0
    rows = (await db_session.execute(select(Distributor))).scalars().all()
    assert all(r.source == SOURCE_SEED for r in rows)


@pytest.mark.asyncio
async def test_places_403_falls_through_to_seed_only(db_session) -> None:
    restaurant = Restaurant(name="R", latitude=35.18, longitude=-80.83)
    db_session.add(restaurant)
    await db_session.commit()
    await db_session.refresh(restaurant)

    with (
        patch("app.services.distributor_discovery.settings.google_places_api_key", "fake"),
        patch("app.services.google_places.settings.google_places_api_key", "fake"),
        respx.mock(assert_all_called=False) as router,
    ):
        router.post(url__regex=r"https://places\.googleapis\.com/.*").mock(
            return_value=httpx.Response(403, json={"error": "billing not enabled"})
        )
        result = await discover_distributors(restaurant_id=restaurant.id)

    assert result.places_found == 0
    rows = (await db_session.execute(select(Distributor))).scalars().all()
    assert all(r.source == SOURCE_SEED for r in rows)


@pytest.mark.asyncio
async def test_places_success_merges_into_seed_and_inserts_new(db_session) -> None:
    restaurant = Restaurant(name="R", latitude=35.18, longitude=-80.83)
    db_session.add(restaurant)
    await db_session.commit()
    await db_session.refresh(restaurant)

    # Two places: one matches a seed (Carolina Fresh Produce Co.) — should merge.
    # One is brand new (Mecklenburg Cold Storage) — should insert.
    places_payload = {
        "places": [
            {
                "id": "places/123",
                "displayName": {"text": "Carolina Fresh Produce Co"},
                "formattedAddress": "2410 Distribution St, Charlotte NC",
                "location": {"latitude": 35.26, "longitude": -80.84},
                "types": ["food_store", "wholesaler"],
                "nationalPhoneNumber": "+1-704-555-9999",  # different from seed
                "websiteUri": "https://updated.carolinafresh.example",
            },
            {
                "id": "places/456",
                "displayName": {"text": "Mecklenburg Cold Storage"},
                "formattedAddress": "1 Cold Storage Way, Charlotte NC",
                "location": {"latitude": 35.25, "longitude": -80.85},
                "types": ["wholesaler"],
                "nationalPhoneNumber": "+1-704-555-7777",
                "websiteUri": "https://meckcold.example",
            },
        ]
    }

    # Classifier keeps both candidates.
    fake_claude = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=_fake_classify_response(
                    [
                        {"index": 0, "is_wholesale_distributor": True, "reason": "produce"},
                        {
                            "index": 1,
                            "is_wholesale_distributor": True,
                            "reason": "cold storage wholesaler",
                        },
                    ]
                )
            )
        )
    )

    with (
        patch("app.services.distributor_discovery.settings.google_places_api_key", "fake"),
        patch("app.services.google_places.settings.google_places_api_key", "fake"),
        patch("app.services.google_places.get_client", return_value=fake_claude),
        respx.mock(assert_all_called=False) as router,
    ):
        router.post(url__regex=r"https://places\.googleapis\.com/.*").mock(
            return_value=httpx.Response(200, json=places_payload)
        )
        result = await discover_distributors(restaurant_id=restaurant.id)

    assert result.places_found == 2  # both kept after classifier
    assert result.places_filtered_out == 0
    assert result.duplicates_merged == 1  # Carolina Fresh existed in seed

    rows = (await db_session.execute(select(Distributor).order_by(Distributor.id))).scalars().all()
    merged = next(r for r in rows if r.name.startswith("Carolina Fresh"))
    assert merged.source == SOURCE_MERGED
    # Places wins on phone/website (amendment 4)
    assert merged.phone == "+1-704-555-9999"
    assert merged.website == "https://updated.carolinafresh.example"
    # Seed wins on address — should retain the seed value
    assert "2410 Distribution St" in (merged.address or "")

    new_row = next(r for r in rows if r.name == "Mecklenburg Cold Storage")
    assert new_row.source == SOURCE_PLACES


@pytest.mark.asyncio
async def test_places_noise_filter_rejects_retail_chains(db_session) -> None:
    restaurant = Restaurant(name="R", latitude=35.18, longitude=-80.83)
    db_session.add(restaurant)
    await db_session.commit()
    await db_session.refresh(restaurant)

    places_payload = {
        "places": [
            {
                "id": "places/grocery",
                "displayName": {"text": "Harris Teeter"},
                "formattedAddress": "100 Park Rd, Charlotte NC",
                "location": {"latitude": 35.18, "longitude": -80.84},
                "types": ["grocery_store", "supermarket"],
            },
            {
                "id": "places/legit",
                "displayName": {"text": "Atlantic Restaurant Supply Wholesale"},
                "formattedAddress": "1 Wholesale Way, Charlotte NC",
                "location": {"latitude": 35.20, "longitude": -80.85},
                "types": ["wholesaler"],
            },
        ]
    }

    fake_claude = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=_fake_classify_response(
                    [
                        {"index": 0, "is_wholesale_distributor": False, "reason": "retail grocery"},
                        {"index": 1, "is_wholesale_distributor": True, "reason": "wholesaler"},
                    ]
                )
            )
        )
    )

    with (
        patch("app.services.distributor_discovery.settings.google_places_api_key", "fake"),
        patch("app.services.google_places.settings.google_places_api_key", "fake"),
        patch("app.services.google_places.get_client", return_value=fake_claude),
        respx.mock(assert_all_called=False) as router,
    ):
        router.post(url__regex=r"https://places\.googleapis\.com/.*").mock(
            return_value=httpx.Response(200, json=places_payload)
        )
        result = await discover_distributors(restaurant_id=restaurant.id)

    assert result.places_filtered_out == 1
    assert result.places_found == 1
    rows = (await db_session.execute(select(Distributor))).scalars().all()
    names = {r.name for r in rows}
    assert "Harris Teeter" not in names
    assert "Atlantic Restaurant Supply Wholesale" in names
