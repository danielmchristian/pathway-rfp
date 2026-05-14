"""F7 — pipeline resilience: one parse failure must not block the batch."""

from __future__ import annotations

from decimal import Decimal
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import select

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
from app.services.email_sender import RESEND_URL
from app.services.quote_pipeline import poll_and_process


def _raw(message_id: str, in_reply_to: str, body: str, subject: str) -> bytes:
    headers = [
        "From: distributor@x.example",
        "To: daniel+carolina-fresh@getserviceledger.com",
        f"Subject: {subject}",
        f"Date: {formatdate(usegmt=True)}",
        f"Message-ID: {message_id}",
        f"In-Reply-To: {in_reply_to}",
        f"References: {in_reply_to}",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
    ]
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")


async def _build_two_distributor_fixture(db_session) -> tuple[int, int, int, str, str]:
    r = Restaurant(name="T", city="Charlotte", state="NC")
    d1 = Distributor(
        name="Carolina Fresh",
        specialties=["produce"],
        source="seed",
        email="a@x.example",
    )
    d2 = Distributor(
        name="Foothills Organic",
        specialties=["produce"],
        source="seed",
        email="b@x.example",
    )
    kale = Ingredient(name="Kale", normalized_name="kale", category=None)
    db_session.add_all([r, d1, d2, kale])
    await db_session.commit()
    for x in (r, d1, d2, kale):
        await db_session.refresh(x)
    req = RfpRequest(restaurant_id=r.id, status=RfpRequestStatus.sent)
    db_session.add(req)
    await db_session.commit()
    await db_session.refresh(req)
    db_session.add(
        RfpRequestItem(
            rfp_request_id=req.id,
            ingredient_id=kale.id,
            quantity=Decimal("400"),
            unit="cup",
        )
    )
    out1_mid = f"<rfp-{req.id}-{d1.id}-aaaa@x>"
    out2_mid = f"<rfp-{req.id}-{d2.id}-bbbb@x>"
    db_session.add_all(
        [
            RfpEmail(
                rfp_request_id=req.id,
                distributor_id=d1.id,
                direction=EmailDirection.out,
                subject=f"[RFP-{req.id}] X",
                body="orig",
                message_id=out1_mid,
                status=EmailStatus.sent,
            ),
            RfpEmail(
                rfp_request_id=req.id,
                distributor_id=d2.id,
                direction=EmailDirection.out,
                subject=f"[RFP-{req.id}] X",
                body="orig",
                message_id=out2_mid,
                status=EmailStatus.sent,
            ),
        ]
    )
    await db_session.commit()
    return req.id, d1.id, d2.id, out1_mid, out2_mid


def _claude_response(payload: dict):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="parse_quote", input=payload)],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=200,
            output_tokens=100,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


@pytest.mark.asyncio
async def test_one_parse_error_does_not_block_others(db_session, monkeypatch) -> None:
    """F7: if parse_quote_email raises on email #2, emails #1 and #3
    must still be processed cleanly. Email #2's row gets
    parse_status='parse_failed'."""
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_user", "u")
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_password", "p")
    monkeypatch.setattr("app.services.email_sender.settings.resend_api_key", "test")
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_from_email",
        "procurement@getserviceledger.com",
    )
    monkeypatch.setattr(
        "app.services.email_sender.settings.rfp_demo_inbox",
        "daniel@getserviceledger.com",
    )

    rfp_id, d1_id, d2_id, out1_mid, out2_mid = await _build_two_distributor_fixture(
        db_session
    )

    # Three inbound: #0 to d1 (parses fine), #1 to d2 (raises), #2 to d1
    # (parses fine). The third reply goes to d1 again — same outbound
    # message-id, just to keep attribution simple.
    raws = [
        (
            1001,
            _raw(
                message_id="<r-1@x>",
                in_reply_to=out1_mid,
                body="Kale: $4/lb",
                subject="Re: quote",
            ),
        ),
        (
            1002,
            _raw(
                message_id="<r-2@x>",
                in_reply_to=out2_mid,
                body="Kale: $5/lb",
                subject="Re: quote",
            ),
        ),
        (
            1003,
            _raw(
                message_id="<r-3@x>",
                in_reply_to=out1_mid,
                body="Kale: $4.50/lb",
                subject="Re: quote",
            ),
        ),
    ]

    call_count = 0
    good_payload = {
        "quotes": [
            {
                "ingredient_name": "Kale",
                "unit_price": 4.0,
                "unit": "lb",
                "min_order_qty": 20.0,
                "delivery_days": 2,
                "terms": "net 30",
                "missing_fields": [],
                "parse_confidence": 0.9,
            }
        ],
        "overall_parse_confidence": 0.9,
        "off_topic": False,
    }

    async def _flaky_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated parser crash on second email")
        return _claude_response(good_payload)

    fake = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=_flaky_create))
    )

    with (
        patch(
            "app.services.inbox_monitor._fetch_unseen_messages_sync",
            return_value=(500, raws),
        ),
        patch("app.services.quote_parser.get_client", return_value=fake),
        patch("app.services.followup_agent.get_client", return_value=fake),
        respx.mock(assert_all_called=False) as router,
    ):
        router.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "x"}))
        result = await poll_and_process(
            restaurant_id=1, rfp_request_id=rfp_id, force_recommendation=False
        )

    # 3 inbound persisted, 1 parse failed, 2 parsed.
    assert result.poll.inbound_count == 3
    assert len(result.parse_results) == 2
    assert len(result.parse_failed_email_ids) == 1

    # The failed email row's parse_status is 'parse_failed'.
    failed_id = result.parse_failed_email_ids[0]
    failed_row = await db_session.get(RfpEmail, failed_id)
    assert failed_row.parse_status == "parse_failed"

    # 2 successful quote rows inserted.
    quotes = (
        await db_session.execute(select(Quote).where(Quote.rfp_request_id == rfp_id))
    ).scalars().all()
    assert len(quotes) == 2


# ---------------------------------------------------------------------------
# Pipeline-driven F2 confirmation: unattributed reply doesn't break the run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_handles_unattributed_alongside_attributed(
    db_session, monkeypatch
) -> None:
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_user", "u")
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_password", "p")
    rfp_id, d1_id, _, out1_mid, _ = await _build_two_distributor_fixture(db_session)

    raws = [
        (
            2001,
            _raw(
                message_id="<r-a@x>",
                in_reply_to=out1_mid,
                body="Kale: $4/lb",
                subject="Re: quote",
            ),
        ),
        (
            2002,
            _raw(
                message_id="<r-b@x>",
                in_reply_to="<does-not-exist@nowhere>",  # tier 1 misses
                body="random email",
                subject="hi",  # tier 3 misses
            ),
        ),
    ]
    # Strip the To header on #2 so plus-tag also misses.
    raws[1] = (
        2002,
        b"From: x@y.example\r\nTo: daniel@nope.example\r\nSubject: hi\r\nDate: "
        + formatdate(usegmt=True).encode()
        + b"\r\nMessage-ID: <r-b@x>\r\n\r\nrandom",
    )

    good_payload = {
        "quotes": [
            {
                "ingredient_name": "Kale",
                "unit_price": 4.0,
                "unit": "lb",
                "min_order_qty": 20.0,
                "delivery_days": 2,
                "terms": "net 30",
                "missing_fields": [],
                "parse_confidence": 0.9,
            }
        ],
        "overall_parse_confidence": 0.9,
        "off_topic": False,
    }
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(side_effect=lambda **kw: _claude_response(good_payload))
        )
    )
    with (
        patch(
            "app.services.inbox_monitor._fetch_unseen_messages_sync",
            return_value=(600, raws),
        ),
        patch("app.services.quote_parser.get_client", return_value=fake),
    ):
        result = await poll_and_process(
            restaurant_id=1, rfp_request_id=rfp_id, force_recommendation=False
        )

    assert result.poll.attributed_count == 1
    assert result.poll.unattributed_count == 1
    # parse_results only includes the attributed one (the unattributed
    # row is parsed but produces 0 quotes — quotes_inserted=0).
    parsed_for_d1 = [p for p in result.parse_results if p.distributor_id == d1_id]
    assert parsed_for_d1
    assert parsed_for_d1[0].quotes_inserted == 1
