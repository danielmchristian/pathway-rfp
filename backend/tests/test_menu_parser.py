from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from app.models.dish import Dish
from app.models.ingredient import Ingredient
from app.models.llm_usage import LlmUsage
from app.models.restaurant import Restaurant
from app.services.menu_parser import parse_menu


def _fake_response(dishes):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", name="extract_menu_items", input={"dishes": dishes})
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=2000,
            output_tokens=800,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


DISHES_FIXTURE = [
    {
        "name": "Harvest Bowl",
        "description": "wild rice, sweet potato, apples, goat cheese, chicken",
        "price": 13.95,
        "parse_confidence": 0.95,
        "ingredients": [
            {"name": "Wild Rice", "quantity": 1.0, "unit": "cup", "estimation_confidence": 0.92},
            {"name": "Sweet Potato", "quantity": 4, "unit": "oz", "estimation_confidence": 0.9},
            {"name": "Chicken Breast", "quantity": 4, "unit": "oz", "estimation_confidence": 0.95},
        ],
    },
    {
        "name": "Guacamole Greens",
        "description": "kale, romaine, lime cilantro jalapeno vinaigrette",
        "price": 11.45,
        "parse_confidence": 0.9,
        "ingredients": [
            {"name": "kale", "quantity": 2, "unit": "cup", "estimation_confidence": 0.9},
            {"name": "romaine", "quantity": 1, "unit": "cup", "estimation_confidence": 0.85},
        ],
    },
    {
        "name": "Chef's Special",
        "description": "rotating seasonal dish",
        "price": None,
        "parse_confidence": 0.3,
        "ingredients": [],
    },
]


@pytest.fixture
def menu_file(tmp_path: Path) -> Path:
    f = tmp_path / "fake.html"
    f.write_text(
        "<html><body><nav>nav junk</nav>"
        "<h1>Menu</h1><p>Harvest Bowl - wild rice, sweet potato, chicken - $13.95</p>"
        "<script>tracking()</script></body></html>",
        encoding="utf-8",
    )
    return f


@pytest.mark.asyncio
async def test_parse_menu_inserts_dishes_and_usage(db_session, menu_file: Path) -> None:
    restaurant = Restaurant(name="Test Restaurant")
    db_session.add(restaurant)
    await db_session.commit()
    await db_session.refresh(restaurant)

    fake_client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=_fake_response(DISHES_FIXTURE)))
    )

    with patch("app.services.menu_parser.get_client", return_value=fake_client):
        result = await parse_menu(restaurant_id=restaurant.id, menu_path=menu_file)

    assert result.dishes_inserted == 3
    assert (
        result.ingredients_inserted >= 4
    )  # wild rice, sweet potato, chicken breast, kale, romaine (5 unique)
    assert result.cost_usd > 0

    dishes = (
        (await db_session.execute(select(Dish).where(Dish.restaurant_id == restaurant.id)))
        .scalars()
        .all()
    )
    assert {d.name for d in dishes} == {"Harvest Bowl", "Guacamole Greens", "Chef's Special"}

    usage_rows = (await db_session.execute(select(LlmUsage))).scalars().all()
    assert any(r.stage == "menu_parse" and (r.cost_usd or 0) > 0 for r in usage_rows)


@pytest.mark.asyncio
async def test_parse_menu_is_idempotent(db_session, menu_file: Path) -> None:
    restaurant = Restaurant(name="Idempotent Restaurant")
    db_session.add(restaurant)
    await db_session.commit()
    await db_session.refresh(restaurant)

    fake_client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=_fake_response(DISHES_FIXTURE)))
    )

    with patch("app.services.menu_parser.get_client", return_value=fake_client):
        await parse_menu(restaurant_id=restaurant.id, menu_path=menu_file)
        await parse_menu(restaurant_id=restaurant.id, menu_path=menu_file)

    dish_count = len(
        (await db_session.execute(select(Dish).where(Dish.restaurant_id == restaurant.id)))
        .scalars()
        .all()
    )
    assert dish_count == 3  # no duplicates after re-parse

    # Ingredient names are unique on normalized_name — kale/romaine etc not duplicated.
    ing_count = len((await db_session.execute(select(Ingredient))).scalars().all())
    assert ing_count == 5


@pytest.mark.asyncio
async def test_parse_menu_rejects_bad_tool_response(db_session, menu_file: Path) -> None:
    restaurant = Restaurant(name="Bad Response Restaurant")
    db_session.add(restaurant)
    await db_session.commit()
    await db_session.refresh(restaurant)

    bad_response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input={"dishes": [{"name": "missing fields"}]})],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=10,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=bad_response))
    )

    # Snapshot dish count before — should be unchanged after the failed parse.
    before = len(
        (await db_session.execute(select(Dish).where(Dish.restaurant_id == restaurant.id)))
        .scalars()
        .all()
    )

    with (
        patch("app.services.menu_parser.get_client", return_value=fake_client),
        pytest.raises(ValueError),
    ):
        await parse_menu(restaurant_id=restaurant.id, menu_path=menu_file)

    after = len(
        (await db_session.execute(select(Dish).where(Dish.restaurant_id == restaurant.id)))
        .scalars()
        .all()
    )
    assert after == before  # nothing deleted, nothing inserted
    # Cleanup the partial llm_usage row that traced_call wrote (the call still happened)
    await db_session.execute(delete(LlmUsage))
    await db_session.commit()
