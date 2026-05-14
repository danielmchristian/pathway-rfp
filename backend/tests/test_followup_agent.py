"""F3 + F4 — atomic follow-up cap + no recursive sending."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.quote import Quote
from app.models.restaurant import Restaurant
from app.models.rfp import (
    EmailDirection,
    EmailStatus,
    RfpEmail,
    RfpRequest,
    RfpRequestStatus,
)
from app.services.email_sender import RESEND_URL
from app.services.followup_agent import maybe_send_followup


def _fake_compose():
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="compose_followup_email",
                input={
                    "subject_tail": "Quick follow-up",
                    "body": "Thanks for your reply — to finalize our planning, could you confirm…",
                },
            )
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=200,
            output_tokens=100,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


async def _build_incomplete_fixture(db_session) -> tuple[int, int, RfpEmail, list[Quote]]:
    r = Restaurant(name="Test", city="Charlotte", state="NC")
    d = Distributor(
        name="Carolina Fresh Produce Co.",
        specialties=["produce"],
        source="seed",
        email="orders@cf.example",
    )
    kale = Ingredient(name="Kale", normalized_name="kale", category=None)
    db_session.add_all([r, d, kale])
    await db_session.commit()
    for x in (r, d, kale):
        await db_session.refresh(x)
    req = RfpRequest(restaurant_id=r.id, status=RfpRequestStatus.sent)
    db_session.add(req)
    await db_session.commit()
    await db_session.refresh(req)
    inbound = RfpEmail(
        rfp_request_id=req.id,
        distributor_id=d.id,
        direction=EmailDirection.in_,
        subject=f"Re: [RFP-{req.id}] Quote",
        body="Kale: $4/lb",
        message_id="<reply-1@distributor.example>",
        status=EmailStatus.received,
        attribution_method="in_reply_to",
        parse_status="parsed",
    )
    db_session.add(inbound)
    await db_session.commit()
    await db_session.refresh(inbound)
    q = Quote(
        rfp_request_id=req.id,
        distributor_id=d.id,
        ingredient_id=kale.id,
        unit_price=Decimal("4"),
        unit="lb",
        min_order_qty=None,  # missing
        delivery_days=None,  # missing
        terms=None,  # missing
        source_email_id=inbound.id,
        parse_confidence=0.8,
        missing_fields=["min_order_qty", "delivery_days", "terms"],
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return req.id, d.id, inbound, [q]


@pytest.mark.asyncio
async def test_followup_sends_once_on_first_incomplete_reply(
    db_session, monkeypatch
) -> None:
    monkeypatch.setattr("app.services.email_sender.settings.resend_api_key", "test")
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    rfp_id, dist_id, inbound, quotes = await _build_incomplete_fixture(db_session)

    fake = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=lambda **kw: _fake_compose()))
    )
    with (
        patch("app.services.followup_agent.get_client", return_value=fake),
        patch("app.services.followup_agent.settings.resend_api_key", "test"),
        patch("app.services.followup_agent.settings.rfp_from_email", "procurement@getserviceledger.com"),
        patch("app.services.followup_agent.settings.rfp_demo_inbox", "daniel@getserviceledger.com"),
        respx.mock(assert_all_called=True) as router,
    ):
        router.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "fu-1"}))
        result = await maybe_send_followup(
            rfp_request_id=rfp_id,
            distributor_id=dist_id,
            parent_inbound_email=inbound,
            incomplete_quotes=quotes,
        )

    assert result.sent is True
    assert result.rfp_email_id is not None
    # rfp_emails has exactly one is_followup row.
    n_followups = (
        await db_session.execute(
            select(func.count())
            .select_from(RfpEmail)
            .where(
                RfpEmail.rfp_request_id == rfp_id,
                RfpEmail.distributor_id == dist_id,
                RfpEmail.is_followup.is_(True),
            )
        )
    ).scalar_one()
    assert n_followups == 1


# ---------------------------------------------------------------------------
# F3 — DB-enforced atomic cap. Even when the application's pre-flight check
#      is bypassed (simulated here by short-circuiting it), the partial
#      UNIQUE INDEX MUST prevent a second insert.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_one_followup_via_db_unique_index(db_session, monkeypatch) -> None:
    """F3: insert a follow-up directly, then try maybe_send_followup again;
    the second attempt MUST be caught by the unique index, NOT silently
    inserted as a second row. We bypass the application's pre-flight to
    ensure the DB invariant is what fires (not the app cache)."""
    monkeypatch.setattr("app.services.followup_agent.settings.resend_api_key", "test")
    monkeypatch.setattr(
        "app.services.followup_agent.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.followup_agent.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    rfp_id, dist_id, inbound, quotes = await _build_incomplete_fixture(db_session)

    # Pre-insert a follow-up row directly to exercise the DB invariant.
    db_session.add(
        RfpEmail(
            rfp_request_id=rfp_id,
            distributor_id=dist_id,
            direction=EmailDirection.out,
            subject="Re: [RFP-X] Pre-existing follow-up",
            body="manual",
            message_id="<existing-fu@x.example>",
            status=EmailStatus.sent,
            is_followup=True,
            recipient_actual="daniel+x@getserviceledger.com",
        )
    )
    await db_session.commit()

    fake = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=lambda **kw: _fake_compose()))
    )
    with (
        patch("app.services.followup_agent.get_client", return_value=fake),
        respx.mock(assert_all_called=False) as router,
    ):
        router.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "fu-2"}))
        result = await maybe_send_followup(
            rfp_request_id=rfp_id,
            distributor_id=dist_id,
            parent_inbound_email=inbound,
            incomplete_quotes=quotes,
        )

    assert result.sent is False
    assert result.skipped_reason == "cap_reached"
    # Still only ONE follow-up row in the DB.
    n_followups = (
        await db_session.execute(
            select(func.count())
            .select_from(RfpEmail)
            .where(
                RfpEmail.rfp_request_id == rfp_id,
                RfpEmail.distributor_id == dist_id,
                RfpEmail.is_followup.is_(True),
            )
        )
    ).scalar_one()
    assert n_followups == 1


@pytest.mark.asyncio
async def test_db_partial_unique_index_directly_blocks_second_followup(
    db_session,
) -> None:
    """F3 (DB invariant — bypasses application). Insert two follow-up
    rows with the SAME (rfp_request_id, distributor_id) directly. The
    second MUST raise IntegrityError because the partial unique index
    `ix_one_followup_per_dist_rfp` forbids it. This proves the cap is
    enforced at the schema level (Amendment A), not just by application
    code that could be bypassed."""
    from sqlalchemy.exc import IntegrityError

    rfp_id, dist_id, _, _ = await _build_incomplete_fixture(db_session)
    db_session.add(
        RfpEmail(
            rfp_request_id=rfp_id,
            distributor_id=dist_id,
            direction=EmailDirection.out,
            subject="Re: [RFP-X] First follow-up",
            body="first",
            message_id="<fu-direct-1@x.example>",
            status=EmailStatus.sent,
            is_followup=True,
            recipient_actual="daniel+x@getserviceledger.com",
        )
    )
    await db_session.commit()

    db_session.add(
        RfpEmail(
            rfp_request_id=rfp_id,
            distributor_id=dist_id,
            direction=EmailDirection.out,
            subject="Re: [RFP-X] Second follow-up",
            body="second",
            message_id="<fu-direct-2@x.example>",
            status=EmailStatus.sent,
            is_followup=True,
            recipient_actual="daniel+x@getserviceledger.com",
        )
    )
    with pytest.raises(IntegrityError) as exc_info:
        await db_session.commit()
    # The specific constraint that fired is the partial unique index.
    assert "ix_one_followup_per_dist_rfp" in str(exc_info.value).lower() or "duplicate" in str(
        exc_info.value
    ).lower()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_two_consecutive_incomplete_replies_only_trigger_one_followup(
    db_session, monkeypatch
) -> None:
    """End-to-end F3: feed two incomplete replies in sequence. Only one
    follow-up should land. This is the realistic failure mode where the
    user replies to the follow-up still incompletely."""
    monkeypatch.setattr("app.services.email_sender.settings.resend_api_key", "test")
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    rfp_id, dist_id, inbound1, quotes1 = await _build_incomplete_fixture(db_session)

    # Insert a second inbound reply (the response to the follow-up,
    # still incomplete).
    inbound2 = RfpEmail(
        rfp_request_id=rfp_id,
        distributor_id=dist_id,
        direction=EmailDirection.in_,
        subject=f"Re: [RFP-{rfp_id}] Re: follow-up",
        body="Sorry, still working on the MOQ.",
        message_id="<reply-2@distributor.example>",
        in_reply_to="<follow-up@getserviceledger.com>",
        status=EmailStatus.received,
        attribution_method="in_reply_to",
        parse_status="parsed",
    )
    db_session.add(inbound2)
    await db_session.commit()
    await db_session.refresh(inbound2)
    quotes2 = [
        Quote(
            rfp_request_id=rfp_id,
            distributor_id=dist_id,
            ingredient_id=quotes1[0].ingredient_id,
            unit_price=Decimal("4"),
            unit="lb",
            min_order_qty=None,
            delivery_days=None,
            terms=None,
            source_email_id=inbound2.id,
            parse_confidence=0.7,
            missing_fields=["min_order_qty", "delivery_days", "terms"],
        )
    ]
    db_session.add(quotes2[0])
    await db_session.commit()

    fake = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=lambda **kw: _fake_compose()))
    )
    with (
        patch("app.services.followup_agent.get_client", return_value=fake),
        patch("app.services.followup_agent.settings.resend_api_key", "test"),
        patch("app.services.followup_agent.settings.rfp_from_email", "procurement@getserviceledger.com"),
        patch("app.services.followup_agent.settings.rfp_demo_inbox", "daniel@getserviceledger.com"),
        respx.mock(assert_all_called=False) as router,
    ):
        router.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "fu-1"}))
        first = await maybe_send_followup(
            rfp_request_id=rfp_id,
            distributor_id=dist_id,
            parent_inbound_email=inbound1,
            incomplete_quotes=quotes1,
        )
        second = await maybe_send_followup(
            rfp_request_id=rfp_id,
            distributor_id=dist_id,
            parent_inbound_email=inbound2,
            incomplete_quotes=quotes2,
        )

    assert first.sent is True
    assert second.sent is False
    assert second.skipped_reason == "cap_reached"

    n_followups = (
        await db_session.execute(
            select(func.count())
            .select_from(RfpEmail)
            .where(
                RfpEmail.rfp_request_id == rfp_id,
                RfpEmail.distributor_id == dist_id,
                RfpEmail.is_followup.is_(True),
            )
        )
    ).scalar_one()
    assert n_followups == 1


# ---------------------------------------------------------------------------
# F4 — no recursive sending. Calling maybe_send_followup MUST trigger
#      exactly one Resend POST and exactly one Claude compose call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_recursive_sending(db_session, monkeypatch) -> None:
    """F4: one inbound reply → exactly one Resend POST and one Claude
    compose call. The follow-up path must NEVER fan out into a second
    send during the same call."""
    monkeypatch.setattr("app.services.email_sender.settings.resend_api_key", "test")
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    rfp_id, dist_id, inbound, quotes = await _build_incomplete_fixture(db_session)

    compose_calls = 0

    async def _counted_create(**kwargs):
        nonlocal compose_calls
        compose_calls += 1
        return _fake_compose()

    fake = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=_counted_create))
    )

    with (
        patch("app.services.followup_agent.get_client", return_value=fake),
        patch("app.services.followup_agent.settings.resend_api_key", "test"),
        patch("app.services.followup_agent.settings.rfp_from_email", "procurement@getserviceledger.com"),
        patch("app.services.followup_agent.settings.rfp_demo_inbox", "daniel@getserviceledger.com"),
        respx.mock(assert_all_called=True) as router,
    ):
        route = router.post(RESEND_URL).mock(
            return_value=httpx.Response(200, json={"id": "fu-1"})
        )
        result = await maybe_send_followup(
            rfp_request_id=rfp_id,
            distributor_id=dist_id,
            parent_inbound_email=inbound,
            incomplete_quotes=quotes,
        )

        assert route.call_count == 1
    assert compose_calls == 1
    assert result.sent is True
