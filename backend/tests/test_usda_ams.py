from unittest.mock import patch

import httpx
import pytest
import respx
from sqlalchemy import select

from app.models.ingredient import Ingredient
from app.models.ingredient_price import IngredientPrice
from app.services.usda_ams import AmsResult, fetch_prices_for_ingredient


@pytest.mark.asyncio
async def test_unmapped_ingredient_records_pricing_unavailable(db_session) -> None:
    ingredient = Ingredient(name="Chicken Breast", normalized_name="chicken breast")
    db_session.add(ingredient)
    await db_session.commit()
    await db_session.refresh(ingredient)

    result = AmsResult()
    async with httpx.AsyncClient() as http:
        await fetch_prices_for_ingredient(
            session=db_session, ingredient=ingredient, client=http, result=result
        )
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(IngredientPrice).where(IngredientPrice.ingredient_id == ingredient.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].pricing_unavailable is True
    assert rows[0].source == "ams_no_match"
    assert result.pricing_unavailable_count == 1


@pytest.mark.asyncio
async def test_seed_fallback_when_no_ams_key(db_session) -> None:
    ingredient = Ingredient(name="Kale", normalized_name="kale")
    db_session.add(ingredient)
    await db_session.commit()
    await db_session.refresh(ingredient)

    result = AmsResult()
    with patch("app.services.usda_ams.settings.usda_ams_api_key", ""):
        async with httpx.AsyncClient() as http:
            await fetch_prices_for_ingredient(
                session=db_session, ingredient=ingredient, client=http, result=result
            )
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(IngredientPrice).where(IngredientPrice.ingredient_id == ingredient.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) > 0
    assert all(r.source == "ams_seed_fallback" for r in rows)
    assert all(not r.pricing_unavailable for r in rows)
    assert result.source_breakdown["ams_seed_fallback"] > 0


@pytest.mark.asyncio
async def test_seed_fallback_is_idempotent_on_rerun(db_session) -> None:
    ingredient = Ingredient(name="Kale", normalized_name="kale")
    db_session.add(ingredient)
    await db_session.commit()
    await db_session.refresh(ingredient)

    with patch("app.services.usda_ams.settings.usda_ams_api_key", ""):
        async with httpx.AsyncClient() as http:
            result1 = AmsResult()
            await fetch_prices_for_ingredient(
                session=db_session, ingredient=ingredient, client=http, result=result1
            )
            await db_session.commit()
            result2 = AmsResult()
            await fetch_prices_for_ingredient(
                session=db_session, ingredient=ingredient, client=http, result=result2
            )
            await db_session.commit()

    assert result1.prices_inserted > 0
    assert result2.prices_inserted == 0


@pytest.mark.asyncio
async def test_live_api_failure_falls_back_to_seed(db_session) -> None:
    ingredient = Ingredient(name="Kale", normalized_name="kale")
    db_session.add(ingredient)
    await db_session.commit()
    await db_session.refresh(ingredient)

    result = AmsResult()
    with (
        patch("app.services.usda_ams.settings.usda_ams_api_key", "fake_key"),
        respx.mock(assert_all_called=False) as router,
    ):
        router.get(url__regex=r"https://marsapi\.ams\.usda\.gov/.*").mock(
            return_value=httpx.Response(503, text="service unavailable")
        )
        async with httpx.AsyncClient() as http:
            await fetch_prices_for_ingredient(
                session=db_session,
                ingredient=ingredient,
                client=http,
                result=result,
            )
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(IngredientPrice).where(IngredientPrice.ingredient_id == ingredient.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) > 0
    assert all(r.source == "ams_seed_fallback" for r in rows)


@pytest.mark.asyncio
async def test_live_api_success_persists_real_observations(db_session) -> None:
    ingredient = Ingredient(name="Kale", normalized_name="kale")
    db_session.add(ingredient)
    await db_session.commit()
    await db_session.refresh(ingredient)

    # Wire format mirrors live MARS API: MM/DD/YYYY dates, package + item_size,
    # no pre-computed price_per_unit (we compute it from package).
    payload = {
        "results": [
            {
                "commodity": "Greens, Kale",
                "report_date": "05/13/2026",
                "package": "cartons bunched",
                "item_size": "24s",
                "low_price": "18.00",
                "high_price": "20.00",
            },
            {
                "commodity": "Greens, Kale",
                "report_date": "05/06/2026",
                "package": "cartons bunched",
                "item_size": "24s",
                "low_price": "16.00",
                "high_price": "18.00",
            },
        ]
    }

    result = AmsResult()
    with (
        patch("app.services.usda_ams.settings.usda_ams_api_key", "fake_key"),
        respx.mock(assert_all_called=False) as router,
    ):
        router.get(url__regex=r"https://marsapi\.ams\.usda\.gov/.*").mock(
            return_value=httpx.Response(200, json=payload)
        )
        async with httpx.AsyncClient() as http:
            await fetch_prices_for_ingredient(
                session=db_session,
                ingredient=ingredient,
                client=http,
                result=result,
            )
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(IngredientPrice).where(IngredientPrice.ingredient_id == ingredient.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert all(r.source == "ams_market_news" for r in rows)
    assert all(r.observed_at is not None for r in rows)
    assert result.source_breakdown["ams_market_news"] == 2
