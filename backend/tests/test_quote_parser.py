"""Quote parser tests + F7 prep (parse-failure handling)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.dish import Dish
from app.models.dish_ingredient import DishIngredient
from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.quote import Quote
from app.models.restaurant import Restaurant
from app.models.rfp import (
    EmailDirection,
    EmailStatus,
    RfpEmail,
    RfpRequest,
    RfpRequestItem,
    RfpRequestStatus,
)
from app.services.quote_parser import parse_quote_email


def _claude_response(payload: dict):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="parse_quote", input=payload)],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=300,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


async def _build_inbound_fixture(db_session, *, body: str = "Kale: $4/lb") -> tuple[int, int, int]:
    r = Restaurant(name="Sweetgreen Test", city="Charlotte", state="NC")
    d = Distributor(
        name="Carolina Fresh", specialties=["produce"], source="seed", email="o@x.example"
    )
    kale = Ingredient(name="Shredded Kale", normalized_name="shredded kale", category=None)
    tom = Ingredient(name="Tomatoes", normalized_name="tomatoes", category=None)
    db_session.add_all([r, d, kale, tom])
    await db_session.commit()
    for x in (r, d, kale, tom):
        await db_session.refresh(x)
    # Need dish + dish_ingredient so ingredients are linked (not strictly
    # required for parser, but mirrors the real setup).
    dish = Dish(restaurant_id=r.id, name="Bowl", parse_confidence=0.95)
    db_session.add(dish)
    await db_session.commit()
    await db_session.refresh(dish)
    db_session.add(
        DishIngredient(dish_id=dish.id, ingredient_id=kale.id, quantity=Decimal("4"), unit="cup")
    )
    await db_session.commit()
    req = RfpRequest(restaurant_id=r.id, status=RfpRequestStatus.sent)
    db_session.add(req)
    await db_session.commit()
    await db_session.refresh(req)
    db_session.add_all(
        [
            RfpRequestItem(
                rfp_request_id=req.id,
                ingredient_id=kale.id,
                quantity=Decimal("600"),
                unit="cup",
            ),
            RfpRequestItem(
                rfp_request_id=req.id,
                ingredient_id=tom.id,
                quantity=None,
                unit=None,
            ),
        ]
    )
    await db_session.commit()
    email_row = RfpEmail(
        rfp_request_id=req.id,
        distributor_id=d.id,
        direction=EmailDirection.in_,
        subject=f"Re: [RFP-{req.id}] Quote",
        body=body,
        message_id="<reply@x.example>",
        status=EmailStatus.received,
        parse_status="unparsed",
    )
    db_session.add(email_row)
    await db_session.commit()
    await db_session.refresh(email_row)
    return email_row.id, d.id, req.id


@pytest.mark.asyncio
async def test_complete_quote_parses_with_empty_missing_fields(
    db_session,
) -> None:
    email_id, dist_id, _ = await _build_inbound_fixture(db_session)
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=_claude_response(
                    {
                        "quotes": [
                            {
                                "ingredient_name": "Shredded Kale",
                                "unit_price": 4.00,
                                "unit": "lb",
                                "min_order_qty": 20.0,
                                "delivery_days": 2,
                                "terms": "net 30",
                                "missing_fields": [],
                                "parse_confidence": 0.95,
                            }
                        ],
                        "overall_parse_confidence": 0.95,
                        "off_topic": False,
                    }
                )
            )
        )
    )
    with patch("app.services.quote_parser.get_client", return_value=fake):
        result = await parse_quote_email(rfp_email_id=email_id)

    assert result.quotes_inserted == 1
    assert not result.off_topic
    q = (
        await db_session.execute(select(Quote).where(Quote.distributor_id == dist_id))
    ).scalar_one()
    assert q.unit_price == Decimal("4")
    assert q.missing_fields == []


@pytest.mark.asyncio
async def test_partial_quote_populates_missing_fields(db_session) -> None:
    email_id, dist_id, _ = await _build_inbound_fixture(db_session)
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=_claude_response(
                    {
                        "quotes": [
                            {
                                "ingredient_name": "Shredded Kale",
                                "unit_price": 4.50,
                                "unit": "lb",
                                "min_order_qty": None,
                                "delivery_days": None,
                                "terms": None,
                                "missing_fields": ["min_order_qty", "delivery_days", "terms"],
                                "parse_confidence": 0.8,
                            }
                        ],
                        "overall_parse_confidence": 0.8,
                        "off_topic": False,
                    }
                )
            )
        )
    )
    with patch("app.services.quote_parser.get_client", return_value=fake):
        await parse_quote_email(rfp_email_id=email_id)
    q = (
        await db_session.execute(select(Quote).where(Quote.distributor_id == dist_id))
    ).scalar_one()
    assert set(q.missing_fields) >= {"min_order_qty", "delivery_days", "terms"}
    assert q.unit_price == Decimal("4.5")


@pytest.mark.asyncio
async def test_quote_on_tbd_quantity_ingredient_persists(db_session) -> None:
    """F6 prep — quote on an ingredient whose rfp_request_items.quantity
    was NULL still persists with the quoted unit_price."""
    email_id, dist_id, _ = await _build_inbound_fixture(
        db_session, body="Tomatoes: $1.50/lb, MOQ 50 lb."
    )
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=_claude_response(
                    {
                        "quotes": [
                            {
                                "ingredient_name": "Tomatoes",
                                "unit_price": 1.50,
                                "unit": "lb",
                                "min_order_qty": 50.0,
                                "delivery_days": 3,
                                "terms": "net 15",
                                "missing_fields": [],
                                "parse_confidence": 0.9,
                            }
                        ],
                        "overall_parse_confidence": 0.9,
                        "off_topic": False,
                    }
                )
            )
        )
    )
    with patch("app.services.quote_parser.get_client", return_value=fake):
        await parse_quote_email(rfp_email_id=email_id)
    q = (
        await db_session.execute(select(Quote).where(Quote.distributor_id == dist_id))
    ).scalar_one()
    assert q.unit_price == Decimal("1.5")
    # The recommender handles TBD-quantity exclusion separately; parser
    # just persists the row.


@pytest.mark.asyncio
async def test_off_topic_reply_persists_empty_quotes(db_session) -> None:
    email_id, _, _ = await _build_inbound_fixture(
        db_session, body="I'm out of the office until next week."
    )
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=_claude_response(
                    {
                        "quotes": [],
                        "overall_parse_confidence": 0.95,
                        "off_topic": True,
                        "note": "vacation auto-responder",
                    }
                )
            )
        )
    )
    with patch("app.services.quote_parser.get_client", return_value=fake):
        result = await parse_quote_email(rfp_email_id=email_id)

    assert result.off_topic
    assert result.quotes_inserted == 0
    assert result.note == "vacation auto-responder"
    n = (
        (await db_session.execute(select(Quote).where(Quote.source_email_id == email_id)))
        .scalars()
        .all()
    )
    assert n == []


@pytest.mark.asyncio
async def test_unmatched_ingredient_name_logged_not_crashed(db_session) -> None:
    email_id, _, _ = await _build_inbound_fixture(db_session)
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(
                return_value=_claude_response(
                    {
                        "quotes": [
                            {
                                "ingredient_name": "Filet Mignon",  # not asked
                                "unit_price": 30.0,
                                "unit": "lb",
                                "missing_fields": [],
                                "parse_confidence": 0.9,
                            }
                        ],
                        "overall_parse_confidence": 0.9,
                        "off_topic": False,
                    }
                )
            )
        )
    )
    with patch("app.services.quote_parser.get_client", return_value=fake):
        result = await parse_quote_email(rfp_email_id=email_id)

    assert "Filet Mignon" in result.unmatched_ingredient_names
    assert result.quotes_inserted == 0  # didn't persist a row for unmatched name
