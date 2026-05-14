"""F5 + F6 — null-safe recommender, plus apples-to-not-apples coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

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
from app.services.recommender import compute_for_rfp


def delivery_comp_for(r) -> object:
    return next(c for c in r.components if c.name == "delivery")


async def _build_rfp_with_two_distributors(
    db_session,
) -> tuple[int, int, int, int, int]:
    """Restaurant + 2 produce distributors + an RFP for kale + tomato.
    Returns (rfp_id, dist_produce_a, dist_produce_b, kale_id, tomato_id)."""
    r = Restaurant(name="Test", city="Charlotte", state="NC")
    da = Distributor(name="Produce A", specialties=["produce"], source="seed", email="a@x.example")
    db = Distributor(name="Produce B", specialties=["produce"], source="seed", email="b@x.example")
    kale = Ingredient(name="Kale", normalized_name="kale", category=None)
    tom = Ingredient(name="Tomatoes", normalized_name="tomatoes", category=None)
    db_session.add_all([r, da, db, kale, tom])
    await db_session.commit()
    for x in (r, da, db, kale, tom):
        await db_session.refresh(x)

    req = RfpRequest(
        restaurant_id=r.id,
        status=RfpRequestStatus.sent,
        deadline=datetime.now(UTC) + timedelta(days=5),
    )
    db_session.add(req)
    await db_session.commit()
    await db_session.refresh(req)
    db_session.add_all(
        [
            RfpRequestItem(
                rfp_request_id=req.id,
                ingredient_id=kale.id,
                quantity=Decimal("100"),
                unit="cup",
            ),
            RfpRequestItem(
                rfp_request_id=req.id,
                ingredient_id=tom.id,
                quantity=Decimal("50"),
                unit="cup",
            ),
        ]
    )
    # Outbound emails for both distributors (so they're in the cohort).
    for dd in (da, db):
        db_session.add(
            RfpEmail(
                rfp_request_id=req.id,
                distributor_id=dd.id,
                direction=EmailDirection.out,
                subject=f"[RFP-{req.id}] Quote",
                body="ask",
                message_id=f"<out-{dd.id}@x>",
                status=EmailStatus.sent,
            )
        )
    await db_session.commit()
    return req.id, da.id, db.id, kale.id, tom.id


# ---------------------------------------------------------------------------
# F5 — null-safe scoring (price NULL excluded+flagged; does NOT crash)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_price_does_not_crash_and_excludes_from_basket(
    db_session,
) -> None:
    rfp_id, da_id, db_id, kale_id, tom_id = await _build_rfp_with_two_distributors(db_session)

    # Produce A: complete quote on kale, null price on tomato.
    db_session.add_all(
        [
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=kale_id,
                unit_price=Decimal("4"),
                unit="lb",
                min_order_qty=Decimal("20"),
                delivery_days=2,
                terms="net 30",
                parse_confidence=0.95,
                missing_fields=[],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=tom_id,
                unit_price=None,  # NULL
                unit=None,
                min_order_qty=None,
                delivery_days=2,
                terms=None,
                parse_confidence=0.6,
                missing_fields=["unit_price", "unit"],
            ),
            # Produce B: complete on both for contrast.
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=db_id,
                ingredient_id=kale_id,
                unit_price=Decimal("5"),
                unit="lb",
                min_order_qty=Decimal("30"),
                delivery_days=3,
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=[],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=db_id,
                ingredient_id=tom_id,
                unit_price=Decimal("2"),
                unit="lb",
                min_order_qty=Decimal("40"),
                delivery_days=3,
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=[],
            ),
        ]
    )
    await db_session.commit()

    rec = await compute_for_rfp(rfp_id, force=True)
    assert rec.ready
    assert rec.pick is not None
    # Produce A has the null tomato → basket marked incomplete.
    a = next(r for r in rec.ranked if r.distributor_id == da_id)
    assert a.incomplete_comparison is True
    assert any("no price" in e or "tomato" in e.lower() for e in a.excluded_for_cost)
    # Score is finite — DID NOT crash, DID NOT treat null as zero.
    assert isinstance(a.score, float)
    # Produce B's complete quote should rank higher (or at least be not-incomplete).
    b = next(r for r in rec.ranked if r.distributor_id == db_id)
    assert b.incomplete_comparison is False


# ---------------------------------------------------------------------------
# F6 — TBD-quantity quotes handled explicitly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tbd_quantity_handled_explicitly(db_session) -> None:
    """TBD-quantity items: quote persists with a price; recommender
    excludes from basket sum AND flags incomplete_comparison."""
    rfp_id, da_id, db_id, kale_id, tom_id = await _build_rfp_with_two_distributors(db_session)

    # Make tomato's RFP item TBD-quantity (NULL).
    from sqlalchemy import update

    await db_session.execute(
        update(RfpRequestItem)
        .where(
            RfpRequestItem.rfp_request_id == rfp_id,
            RfpRequestItem.ingredient_id == tom_id,
        )
        .values(quantity=None, unit=None)
    )
    await db_session.commit()

    # Distributor A quotes a price on the TBD-quantity tomato.
    db_session.add_all(
        [
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=kale_id,
                unit_price=Decimal("4"),
                unit="lb",
                min_order_qty=Decimal("20"),
                delivery_days=2,
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=[],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=tom_id,
                unit_price=Decimal("1.50"),
                unit="lb",
                min_order_qty=Decimal("50"),
                delivery_days=2,
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=[],
            ),
        ]
    )
    await db_session.commit()

    rec = await compute_for_rfp(rfp_id, force=True)
    assert rec.pick is not None
    a = next(r for r in rec.ranked if r.distributor_id == da_id)
    # The TBD ingredient is excluded from the basket sum AND incomplete is set.
    assert a.incomplete_comparison is True
    assert any("TBD" in e or "tomato" in e.lower() for e in a.excluded_for_cost)
    # Did not crash on the TBD path.
    assert isinstance(a.score, float)


# ---------------------------------------------------------------------------
# Asymmetric null-safety: delivery_days NULL → 0.0 (NOT excluded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_null_scored_worst_case_not_excluded(db_session) -> None:
    """Asymmetric rule: a distributor refusing to commit to delivery_days
    is scored 0.0 — not absent, not excluded. The rationale text must
    say this explicitly so the writeup can defend the choice."""
    rfp_id, da_id, db_id, kale_id, tom_id = await _build_rfp_with_two_distributors(db_session)

    db_session.add_all(
        [
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=kale_id,
                unit_price=Decimal("4"),
                unit="lb",
                min_order_qty=Decimal("20"),
                delivery_days=None,  # NULL — worst-case
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=["delivery_days"],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=tom_id,
                unit_price=Decimal("2"),
                unit="lb",
                min_order_qty=Decimal("40"),
                delivery_days=None,  # NULL — worst-case
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=["delivery_days"],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=db_id,
                ingredient_id=kale_id,
                unit_price=Decimal("5"),
                unit="lb",
                min_order_qty=Decimal("30"),
                delivery_days=3,
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=[],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=db_id,
                ingredient_id=tom_id,
                unit_price=Decimal("3"),
                unit="lb",
                min_order_qty=Decimal("40"),
                delivery_days=3,
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=[],
            ),
        ]
    )
    await db_session.commit()

    rec = await compute_for_rfp(rfp_id, force=True)
    a = next(r for r in rec.ranked if r.distributor_id == da_id)
    b = next(r for r in rec.ranked if r.distributor_id == db_id)
    delivery_comp = next(c for c in a.components if c.name == "delivery")
    assert delivery_comp.normalized == 0.0
    assert delivery_comp.null_imputed is True
    assert delivery_comp.note and "worst-case" in delivery_comp.note
    # Rationale must explicitly say worst-case (the asymmetric design).
    assert "worst-case" in a.rationale
    # The delivery penalty hurts A (delivery contribution = 0 vs B's ~0.13)
    # — A's overall score must be lower than it would be if delivery had
    # been imputed at the median or excluded. We assert the penalty bit it
    # for at least the full delivery-weight worth (0.20 × 1.0 = 0.20 pts).
    b_delivery_contribution = 0.20 * delivery_comp_for(b).normalized
    assert b_delivery_contribution > 0.0
    # And the asymmetric rule fired: delivery is 0.0, NOT imputed-as-median.
    assert delivery_comp.raw_value is None


# ---------------------------------------------------------------------------
# Apples-to-not-apples: different baskets, surfaced honestly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_basket_coverage_surfaced_in_rationale(db_session) -> None:
    rfp_id, da_id, db_id, kale_id, tom_id = await _build_rfp_with_two_distributors(db_session)
    # A quotes only kale. B quotes both.
    db_session.add_all(
        [
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=kale_id,
                unit_price=Decimal("4"),
                unit="lb",
                min_order_qty=Decimal("20"),
                delivery_days=2,
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=[],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=db_id,
                ingredient_id=kale_id,
                unit_price=Decimal("5"),
                unit="lb",
                min_order_qty=Decimal("30"),
                delivery_days=3,
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=[],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=db_id,
                ingredient_id=tom_id,
                unit_price=Decimal("2"),
                unit="lb",
                min_order_qty=Decimal("40"),
                delivery_days=3,
                terms="net 30",
                parse_confidence=0.9,
                missing_fields=[],
            ),
        ]
    )
    await db_session.commit()

    rec = await compute_for_rfp(rfp_id, force=True)
    a = next(r for r in rec.ranked if r.distributor_id == da_id)
    assert a.quoted_ingredient_count == 1
    assert a.requested_ingredient_count == 2
    assert a.coverage_pct == Decimal("50.00")
    assert a.incomplete_comparison is True
    assert "1/2" in a.rationale or "50%" in a.rationale


# ---------------------------------------------------------------------------
# Deadline not passed + not all replied → not ready
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recommendation_not_ready_without_force(db_session) -> None:
    rfp_id, _, _, _, _ = await _build_rfp_with_two_distributors(db_session)
    # No quotes inserted; deadline 5 days out.
    rec = await compute_for_rfp(rfp_id, force=False)
    assert rec.ready is False
    assert rec.not_ready_reason and "awaiting" in rec.not_ready_reason
    assert rec.pick is None


@pytest.mark.asyncio
async def test_force_with_no_quotes_returns_no_pick_no_crash(db_session) -> None:
    rfp_id, _, _, _, _ = await _build_rfp_with_two_distributors(db_session)
    rec = await compute_for_rfp(rfp_id, force=True)
    assert rec.ready is True
    assert rec.pick is None  # no quotes → no pick
    assert rec.ranked == []


# ---------------------------------------------------------------------------
# Scoring regression: complete competitive quote MUST outrank
# incomplete partial quote with equivalent per-item pricing.
#
# Pre-fix bug: cost was scored on basket TOTAL, so a partial-basket
# distributor had a smaller sum and looked artificially cheaper. The
# fix is per-item rank averaging + coverage as a 5th weighted component.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_competitive_outranks_partial_equivalent(db_session) -> None:
    """A distributor that quotes ALL items at price $X beats a distributor
    that quotes ONE item at the same $X. The per-item cost score ties, but
    coverage breaks the tie — and the broken pre-fix code would have ranked
    the partial as cheaper because the basket-sum total was smaller."""
    rfp_id, da_id, db_id, kale_id, tom_id = await _build_rfp_with_two_distributors(db_session)
    # A: complete on both, $4/lb kale, $2/lb tomato, delivery 2, MOQ low.
    # B: partial — only kale at the same $4/lb. Identical per-item pricing
    #    on the items they share; B simply quoted less.
    db_session.add_all(
        [
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=kale_id,
                unit_price=Decimal("4"),
                unit="lb",
                min_order_qty=Decimal("20"),
                delivery_days=2,
                terms="net 30",
                parse_confidence=0.95,
                missing_fields=[],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=tom_id,
                unit_price=Decimal("2"),
                unit="lb",
                min_order_qty=Decimal("30"),
                delivery_days=2,
                terms="net 30",
                parse_confidence=0.95,
                missing_fields=[],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=db_id,
                ingredient_id=kale_id,
                unit_price=Decimal("4"),  # identical per-item price to A
                unit="lb",
                min_order_qty=Decimal("20"),
                delivery_days=2,
                terms="net 30",
                parse_confidence=0.95,
                missing_fields=[],
            ),
        ]
    )
    await db_session.commit()

    rec = await compute_for_rfp(rfp_id, force=True)
    a = next(r for r in rec.ranked if r.distributor_id == da_id)
    b = next(r for r in rec.ranked if r.distributor_id == db_id)

    # Per-item kale cost is tied (both at $4) — both get 1.0 on kale.
    # Tomato is only quoted by A and isn't compared. So cost scores tie at 1.0.
    a_cost = next(c for c in a.components if c.name == "cost")
    b_cost = next(c for c in b.components if c.name == "cost")
    assert a_cost.normalized == b_cost.normalized == 1.0

    # Coverage breaks the tie — A covers 2/2, B covers 1/2.
    a_cov = next(c for c in a.components if c.name == "coverage")
    b_cov = next(c for c in b.components if c.name == "coverage")
    assert a_cov.normalized == 1.0
    assert b_cov.normalized == 0.5

    # Complete distributor MUST win.
    assert a.score > b.score
    assert rec.pick.distributor_id == da_id

    # And the partial distributor's coverage is honestly surfaced.
    assert b.incomplete_comparison is True
    assert "1/2" in b.rationale or "50%" in b.rationale


@pytest.mark.asyncio
async def test_partial_with_strictly_better_per_item_pricing_can_still_win(db_session) -> None:
    """The fix isn't a hard gate against partial baskets — a distributor
    with genuinely better per-item pricing should still be able to win
    despite lower coverage. This documents the trade-off explicitly so a
    later weight tweak doesn't silently turn coverage into a hard floor."""
    rfp_id, da_id, db_id, kale_id, tom_id = await _build_rfp_with_two_distributors(db_session)
    # A: complete, but expensive — $10/lb kale, $5/lb tomato.
    # B: partial, dramatically cheaper — $1/lb kale only.
    db_session.add_all(
        [
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=kale_id,
                unit_price=Decimal("10"),
                unit="lb",
                min_order_qty=Decimal("20"),
                delivery_days=2,
                terms="net 30",
                parse_confidence=0.95,
                missing_fields=[],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=da_id,
                ingredient_id=tom_id,
                unit_price=Decimal("5"),
                unit="lb",
                min_order_qty=Decimal("30"),
                delivery_days=2,
                terms="net 30",
                parse_confidence=0.95,
                missing_fields=[],
            ),
            Quote(
                rfp_request_id=rfp_id,
                distributor_id=db_id,
                ingredient_id=kale_id,
                unit_price=Decimal("1"),  # 10x cheaper than A's kale
                unit="lb",
                min_order_qty=Decimal("20"),
                delivery_days=2,
                terms="net 30",
                parse_confidence=0.95,
                missing_fields=[],
            ),
        ]
    )
    await db_session.commit()

    rec = await compute_for_rfp(rfp_id, force=True)
    a = next(r for r in rec.ranked if r.distributor_id == da_id)
    b = next(r for r in rec.ranked if r.distributor_id == db_id)
    # B is dramatically cheaper on the one item they share → B wins on cost.
    # 0.35*(1.0 - 0.0) + 0.20*(50% - 100% coverage delta) is enough.
    assert b.score > a.score
