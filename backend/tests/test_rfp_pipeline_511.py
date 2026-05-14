"""Phase 5.1 pipeline invariants: tightened matching + unassigned surface."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.models.dish import Dish
from app.models.dish_ingredient import DishIngredient
from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.restaurant import Restaurant
from app.services.distributor_matching import specialty_tags_for
from app.services.email_sender import RESEND_URL
from app.services.rfp_pipeline import send_rfps


def _fake_compose(subject_tail="Quote request", body="Body content."):
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
            input_tokens=400, output_tokens=200,
            cache_creation_input_tokens=0, cache_read_input_tokens=0,
        ),
    )


async def _build_fixture_with_composite_and_leak_traps(db_session) -> int:
    """Restaurant with a sauce + a steak + a beverage — exercises every guard."""
    r = Restaurant(
        name="Test Restaurant",
        city="Charlotte",
        state="NC",
        latitude=Decimal("35.18"),
        longitude=Decimal("-80.83"),
    )
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)

    # The composite: would match Carolina Fresh via "cilantro" in v1.
    sauce = Ingredient(
        name="Lime Cilantro Jalapeño Sauce",
        normalized_name="lime cilantro jalapeño sauce",
        category=None,
    )
    # The substring-leak trap: would match Three Rivers via "tea" in "steak".
    steak = Ingredient(
        name="Caramelized Garlic Steak",
        normalized_name="caramelized garlic steak",
        category=None,
    )
    # Legit produce.
    kale = Ingredient(name="Shredded Kale", normalized_name="shredded kale", category=None)
    tomato = Ingredient(
        name="Vine Ripe Tomatoes", normalized_name="vine ripe tomatoes", category=None
    )
    # Legit beverage.
    kombucha = Ingredient(
        name="Kombucha (brewed tea)", normalized_name="kombucha (brewed tea)", category=None
    )
    for i in (sauce, steak, kale, tomato, kombucha):
        db_session.add(i)
    await db_session.commit()
    for i in (sauce, steak, kale, tomato, kombucha):
        await db_session.refresh(i)

    d1 = Dish(restaurant_id=r.id, name="Kale Caesar", parse_confidence=0.95)
    d2 = Dish(restaurant_id=r.id, name="Steak Bowl", parse_confidence=0.9)
    d3 = Dish(restaurant_id=r.id, name="Refresher", parse_confidence=0.9)
    db_session.add_all([d1, d2, d3])
    await db_session.commit()
    for d in (d1, d2, d3):
        await db_session.refresh(d)

    db_session.add_all(
        [
            DishIngredient(dish_id=d1.id, ingredient_id=kale.id, quantity=Decimal("4"), unit="cup"),
            DishIngredient(dish_id=d1.id, ingredient_id=tomato.id, quantity=Decimal("2"), unit="cup"),
            DishIngredient(dish_id=d1.id, ingredient_id=sauce.id, quantity=Decimal("3"), unit="tbsp"),
            DishIngredient(dish_id=d2.id, ingredient_id=steak.id, quantity=Decimal("6"), unit="oz"),
            DishIngredient(dish_id=d2.id, ingredient_id=kale.id, quantity=Decimal("3"), unit="cup"),
            DishIngredient(dish_id=d3.id, ingredient_id=kombucha.id, quantity=Decimal("16"), unit="fl oz"),
        ]
    )
    await db_session.commit()

    db_session.add_all(
        [
            Distributor(
                name="Carolina Fresh Produce",
                specialties=["produce", "leafy_greens", "tomatoes"],
                source="seed",
                email="orders@carolinafresh.example",
                latitude=Decimal("35.26"),
                longitude=Decimal("-80.84"),
            ),
            Distributor(
                name="Three Rivers Beverage",
                specialties=["beverages"],
                source="seed",
                email="orders@threerivers.example",
                latitude=Decimal("35.17"),
                longitude=Decimal("-80.87"),
            ),
            Distributor(
                name="Queen City Meats",
                specialties=["protein_meat", "protein_poultry"],
                source="seed",
                email="orders@queencitymeats.example",
                latitude=Decimal("35.17"),
                longitude=Decimal("-80.87"),
            ),
        ]
    )
    await db_session.commit()
    return r.id


@pytest.mark.asyncio
async def test_unassigned_includes_composite_and_no_leaks(
    db_session, monkeypatch
) -> None:
    restaurant_id = await _build_fixture_with_composite_and_leak_traps(db_session)
    monkeypatch.setattr("app.services.email_sender.settings.resend_api_key", "test")
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    captured: list[dict] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        import json
        captured.append(json.loads(req.content))
        return httpx.Response(200, json={"id": f"resend-{len(captured)}"})

    fake_claude = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(side_effect=lambda **kw: _fake_compose())
        )
    )

    # With min_matches=1 — even a single legit match per distributor counts.
    with (
        patch("app.services.rfp_composer.get_client", return_value=fake_claude),
        respx.mock(assert_all_called=False) as router,
    ):
        router.post(RESEND_URL).mock(side_effect=_handler)
        result = await send_rfps(
            restaurant_id=restaurant_id,
            distributor_limit=5,
            min_matches=1,
            deadline_days=5,
        )

    # Composite sauce MUST appear in unassigned (it doesn't fit any specialty).
    assert "Lime Cilantro Jalapeño Sauce" in result.unassigned_ingredients

    # Invariant: every scoped ingredient under each distributor's email
    # must have a tag overlap with that distributor's specialties.
    for outcome in result.breakdown:
        # Pull the distributor + its scoped ingredients from the DB to verify.
        # Easier proxy: by name, validate from rfp_emails body NOT containing
        # leak items.
        body_text = next(
            payload["text"] for payload in captured
            if payload["to"][0].endswith(
                f"+{outcome.distributor_name.lower().replace(' ', '-')}@getserviceledger.com"
            )
        )
        if outcome.distributor_name == "Three Rivers Beverage":
            assert "Caramelized Garlic Steak" not in body_text  # v1 leak fixed
            assert "Steak" not in body_text
        if outcome.distributor_name == "Carolina Fresh Produce":
            assert "Lime Cilantro Jalapeño Sauce" not in body_text  # v1 leak fixed
            assert "Sauce" not in body_text


@pytest.mark.asyncio
async def test_distributor_drops_when_only_leak_matches(
    db_session, monkeypatch
) -> None:
    """If a distributor's only matches were leak-driven, it falls below
    min_matches after tightening and gets dropped."""
    r = Restaurant(name="Tiny", city="Charlotte", state="NC")
    db_session.add(r)
    await db_session.commit()
    await db_session.refresh(r)

    steak = Ingredient(
        name="Caramelized Garlic Steak", normalized_name="steak", category=None
    )
    db_session.add(steak)
    await db_session.commit()
    await db_session.refresh(steak)

    d = Dish(restaurant_id=r.id, name="Steak", parse_confidence=0.9)
    db_session.add(d)
    await db_session.commit()
    await db_session.refresh(d)
    db_session.add(
        DishIngredient(dish_id=d.id, ingredient_id=steak.id, quantity=Decimal("4"), unit="oz")
    )
    await db_session.commit()

    db_session.add_all(
        [
            Distributor(
                name="Beverages Only",
                specialties=["beverages"],
                source="seed",
                email="b@example.com",
            ),
            Distributor(
                name="Meats Co",
                specialties=["protein_meat"],
                source="seed",
                email="m@example.com",
            ),
        ]
    )
    await db_session.commit()

    monkeypatch.setattr("app.services.email_sender.settings.resend_api_key", "test")
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    fake_claude = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(side_effect=lambda **kw: _fake_compose())
        )
    )
    with (
        patch("app.services.rfp_composer.get_client", return_value=fake_claude),
        respx.mock(assert_all_called=False) as router,
    ):
        router.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
        result = await send_rfps(
            restaurant_id=r.id, distributor_limit=5, min_matches=1, deadline_days=5
        )

    # Only Meats Co gets the email; "Beverages Only" had a leak match in v1
    # but now has zero, so falls below min_matches=1 and is excluded.
    names = {b.distributor_name for b in result.breakdown}
    assert names == {"Meats Co"}


# Ensure the matcher itself is exercised — invariant: any tag we report
# for a steak DOES NOT include "beverages".
def test_specialty_invariant_steak_not_beverage() -> None:
    from app.models.ingredient import Ingredient as Ing
    tags = specialty_tags_for(
        Ing(id=1, name="Caramelized Garlic Steak", normalized_name="steak", category=None)
    )
    assert "beverages" not in tags
