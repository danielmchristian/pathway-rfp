from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.distributor import Distributor
from app.models.restaurant import Restaurant
from app.services.quantity_aggregator import IngredientVolume, canonical_root
from app.services.rfp_composer import compose_rfp_email


def _fake_compose_response(subject_tail: str, body: str):
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="compose_rfp_email",
                input={"subject_tail": subject_tail, "body": body},
            )
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=300,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _vol(name: str, qty: Decimal | None, unit: str) -> IngredientVolume:
    return IngredientVolume(
        ingredient_id=hash(name) & 0xFFFFFF,
        ingredient_name=name,
        normalized_name=name.lower(),
        category=None,
        root=canonical_root(name),
        weekly_quantity=qty,
        unit=unit,
        dishes_used=1,
    )


@pytest.mark.asyncio
async def test_composer_returns_subject_and_body() -> None:
    restaurant = Restaurant(id=1, name="Sweetgreen Park Rd", city="Charlotte", state="NC")
    distributor = Distributor(id=10, name="Carolina Fresh Produce", specialties=["produce"])
    fake_claude = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=_fake_compose_response(
                    "Ingredient quote request — Sweetgreen Charlotte",
                    "Hello Carolina Fresh,\n\nWe'd like quotes on: Kale, Tomatoes...",
                )
            )
        )
    )

    with patch("app.services.rfp_composer.get_client", return_value=fake_claude):
        content = await compose_rfp_email(
            restaurant=restaurant,
            distributor=distributor,
            ingredients=[
                _vol("Kale", Decimal("200"), "oz"),
                _vol("Tomatoes", Decimal("80"), "oz"),
            ],
            deadline=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
            covers_per_dish_per_week=150,
        )

    assert content.subject_tail == "Ingredient quote request — Sweetgreen Charlotte"
    assert "Carolina Fresh" in content.body
    # No [RFP-id] prefix yet — orchestrator adds that.
    assert "[RFP-" not in content.subject_tail


@pytest.mark.asyncio
async def test_composer_user_message_includes_scoped_ingredients_only() -> None:
    """Claude receives the ingredients for THIS distributor — nothing else."""
    restaurant = Restaurant(id=1, name="Test")
    distributor = Distributor(id=10, name="Produce Co", specialties=["produce"])
    captured: dict = {}

    async def capturing_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _fake_compose_response("Quote request", "Body text.")

    fake_claude = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=capturing_create))
    )
    with patch("app.services.rfp_composer.get_client", return_value=fake_claude):
        await compose_rfp_email(
            restaurant=restaurant,
            distributor=distributor,
            ingredients=[_vol("Kale", Decimal("200"), "oz")],
            deadline=datetime(2026, 5, 21, tzinfo=UTC),
            covers_per_dish_per_week=150,
        )

    user_msg = captured["messages"][0]["content"]
    assert "Kale" in user_msg
    # Salmon was NEVER in scope; must not leak in.
    assert "Salmon" not in user_msg
    # Planning-assumption label MUST be present (user-required).
    assert "covers per dish per week" in user_msg
    assert "150" in user_msg


@pytest.mark.asyncio
async def test_composer_rejects_empty_ingredient_list() -> None:
    restaurant = Restaurant(id=1, name="Test")
    distributor = Distributor(id=10, name="Produce Co", specialties=["produce"])
    with pytest.raises(ValueError, match="empty"):
        await compose_rfp_email(
            restaurant=restaurant,
            distributor=distributor,
            ingredients=[],
            deadline=datetime(2026, 5, 21, tzinfo=UTC),
            covers_per_dish_per_week=150,
        )


@pytest.mark.asyncio
async def test_composer_raises_on_missing_tool_use() -> None:
    restaurant = Restaurant(id=1, name="Test")
    distributor = Distributor(id=10, name="Produce Co", specialties=["produce"])
    bad_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="sorry no tool call")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=10,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )
    fake_claude = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=bad_response))
    )
    with (
        patch("app.services.rfp_composer.get_client", return_value=fake_claude),
        pytest.raises(RuntimeError, match="tool_use"),
    ):
        await compose_rfp_email(
            restaurant=restaurant,
            distributor=distributor,
            ingredients=[_vol("Kale", Decimal("200"), "oz")],
            deadline=datetime(2026, 5, 21, tzinfo=UTC),
            covers_per_dish_per_week=150,
        )
