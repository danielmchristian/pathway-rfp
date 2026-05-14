"""End-to-end pipeline test for Phase 5 — mocks Claude composer + Resend.

Builds a fixture restaurant with:
  - dishes: 2 dishes each using produce + chicken
  - distributors: 1 produce (3 matches), 1 meat (1 match), 1 seafood (0 matches)
With min_matches=2, only the produce distributor gets emailed.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import select

from app.models.dish import Dish
from app.models.dish_ingredient import DishIngredient
from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.restaurant import Restaurant
from app.models.rfp import EmailDirection, EmailStatus, RfpEmail, RfpRequest, RfpRequestItem
from app.services.email_sender import RESEND_URL
from app.services.rfp_pipeline import send_rfps


def _fake_compose(subject_tail="Ingredient quote request", body="Hello distributor."):
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
            input_tokens=400,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


async def _build_fixture(db_session) -> int:
    restaurant = Restaurant(
        name="Sweetgreen Park Rd",
        city="Charlotte",
        state="NC",
        latitude=Decimal("35.18"),
        longitude=Decimal("-80.83"),
    )
    db_session.add(restaurant)
    await db_session.commit()
    await db_session.refresh(restaurant)

    # Two wording variants of kale (test dedupe) + tomato + chicken
    kale = Ingredient(
        name="Shredded Kale",
        normalized_name="shredded kale",
        category="Vegetables and Vegetable Products",
    )
    kale_alt = Ingredient(
        name="Organic Kale",
        normalized_name="organic kale",
        category="Vegetables and Vegetable Products",
    )
    tomato = Ingredient(
        name="Vine Ripe Tomatoes",
        normalized_name="vine ripe tomatoes",
        category="Vegetables and Vegetable Products",
    )
    chicken = Ingredient(
        name="Roasted Chicken",
        normalized_name="roasted chicken",
        category="Poultry Products",
    )
    for i in (kale, kale_alt, tomato, chicken):
        db_session.add(i)
    await db_session.commit()
    for i in (kale, kale_alt, tomato, chicken):
        await db_session.refresh(i)

    dish1 = Dish(restaurant_id=restaurant.id, name="Kale Caesar", parse_confidence=0.95)
    dish2 = Dish(restaurant_id=restaurant.id, name="Harvest Bowl", parse_confidence=0.9)
    db_session.add_all([dish1, dish2])
    await db_session.commit()
    await db_session.refresh(dish1)
    await db_session.refresh(dish2)

    db_session.add_all(
        [
            DishIngredient(
                dish_id=dish1.id, ingredient_id=kale.id, quantity=Decimal("4"), unit="oz"
            ),
            DishIngredient(
                dish_id=dish1.id, ingredient_id=tomato.id, quantity=Decimal("2"), unit="oz"
            ),
            DishIngredient(
                dish_id=dish1.id, ingredient_id=chicken.id, quantity=Decimal("4"), unit="oz"
            ),
            DishIngredient(
                dish_id=dish2.id, ingredient_id=kale_alt.id, quantity=Decimal("3"), unit="oz"
            ),
            DishIngredient(
                dish_id=dish2.id, ingredient_id=tomato.id, quantity=Decimal("2"), unit="oz"
            ),
        ]
    )
    await db_session.commit()

    produce = Distributor(
        name="Carolina Fresh Produce",
        specialties=["produce", "leafy_greens", "tomatoes"],
        source="seed",
        email="orders@carolinafresh.example",
        latitude=Decimal("35.26"),
        longitude=Decimal("-80.84"),
    )
    meat = Distributor(
        name="Piedmont Meats Co",
        specialties=["protein_meat", "protein_poultry"],
        source="seed",
        email="orders@piedmontmeats.example",
        latitude=Decimal("35.17"),
        longitude=Decimal("-80.87"),
    )
    seafood = Distributor(
        name="Tidewater Seafood",
        specialties=["protein_seafood"],
        source="seed",
        email="orders@tidewater.example",
        latitude=Decimal("35.21"),
        longitude=Decimal("-80.94"),
    )
    db_session.add_all([produce, meat, seafood])
    await db_session.commit()
    return restaurant.id


@pytest.mark.asyncio
async def test_pipeline_emails_only_distributors_above_min_matches(
    db_session, monkeypatch
) -> None:
    restaurant_id = await _build_fixture(db_session)

    monkeypatch.setattr(
        "app.services.email_sender.settings.resend_api_key", "test-key"
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    sent_payloads: list[dict] = []

    def _resend_handler(request: httpx.Request) -> httpx.Response:
        import json
        sent_payloads.append(json.loads(request.content))
        return httpx.Response(
            200, json={"id": f"resend-{len(sent_payloads)}"}
        )

    fake_claude = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                side_effect=lambda **kw: _fake_compose(
                    body="Body content with Shredded Kale, Vine Ripe Tomatoes, Organic Kale"
                )
            )
        )
    )

    with (
        patch("app.services.rfp_composer.get_client", return_value=fake_claude),
        respx.mock(assert_all_called=True) as router,
    ):
        router.post(RESEND_URL).mock(side_effect=_resend_handler)
        result = await send_rfps(
            restaurant_id=restaurant_id,
            distributor_limit=5,
            min_matches=2,
            deadline_days=5,
        )

    # Only the produce distributor matches >= 2 ingredients (kale + kale_alt + tomato).
    # Meat (chicken=1) and seafood (0) are excluded.
    assert result.distributors_targeted == 1
    assert result.emails_sent == 1
    assert result.emails_failed == 0
    assert len(sent_payloads) == 1
    assert sent_payloads[0]["to"] == [
        "daniel+carolina-fresh-produce@getserviceledger.com"
    ]
    # Subject prefix is deterministic
    assert sent_payloads[0]["subject"].startswith(f"[RFP-{result.rfp_request_id}]")

    # rfp_request_items dedupe-aware union — only kale + tomato (the produce
    # scope), and the two kale variants collapsed to ONE item row.
    items = (
        (
            await db_session.execute(
                select(RfpRequestItem).where(
                    RfpRequestItem.rfp_request_id == result.rfp_request_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(items) == 2
    assert result.items_count == 2

    # Email row was persisted with our minted Message-ID and demo recipient.
    emails = (
        (await db_session.execute(select(RfpEmail))).scalars().all()
    )
    assert len(emails) == 1
    assert emails[0].direction == EmailDirection.out
    assert emails[0].status == EmailStatus.sent
    assert emails[0].recipient_actual == (
        "daniel+carolina-fresh-produce@getserviceledger.com"
    )
    assert emails[0].recipient_nominal == "orders@carolinafresh.example"
    assert emails[0].message_id.startswith(f"<rfp-{result.rfp_request_id}-")

    # rfp_request marked as fully sent
    req = await db_session.get(RfpRequest, result.rfp_request_id)
    assert req.status.value == "sent"


@pytest.mark.asyncio
async def test_pipeline_continues_when_one_resend_call_fails(
    db_session, monkeypatch
) -> None:
    restaurant_id = await _build_fixture(db_session)

    monkeypatch.setattr(
        "app.services.email_sender.settings.resend_api_key", "test-key"
    )
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

    # Lower min_matches to 1 so meat distributor (chicken match) is included.
    with (
        patch("app.services.rfp_composer.get_client", return_value=fake_claude),
        respx.mock(assert_all_called=False) as router,
    ):
        # First call (produce) → 200 OK. Second call (meat) → 422.
        router.post(RESEND_URL).mock(
            side_effect=[
                httpx.Response(200, json={"id": "resend-ok"}),
                httpx.Response(422, json={"error": "bad address"}),
            ]
        )
        result = await send_rfps(
            restaurant_id=restaurant_id,
            distributor_limit=5,
            min_matches=1,
            deadline_days=5,
        )

    assert result.emails_sent == 1
    assert result.emails_failed == 1
    assert len(result.breakdown) == 2
    statuses = {b.status for b in result.breakdown}
    assert "sent" in statuses
    assert "failed" in statuses

    # rfp_request status is 'partial' on mixed outcome.
    req = await db_session.get(RfpRequest, result.rfp_request_id)
    assert req.status.value == "partial"
