"""F1, F2, F8 — inbox monitor failure-mode tests + happy-path attribution."""

from __future__ import annotations

from decimal import Decimal
from email.utils import formatdate
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.distributor import Distributor
from app.models.imap_seen_uid import ImapSeenUid
from app.models.restaurant import Restaurant
from app.models.rfp import (
    EmailDirection,
    EmailStatus,
    RfpEmail,
    RfpRequest,
    RfpRequestStatus,
)
from app.services.inbox_monitor import (
    ATTR_IN_REPLY_TO,
    ATTR_PLUS_TAG,
    ATTR_SUBJECT_PREFIX,
    ATTR_UNATTRIBUTED,
    attribute_reply,
    parse_message,
    poll_inbox,
)

# ---------------------------------------------------------------------------
# Helpers — fabricate raw RFC-2822 bytes
# ---------------------------------------------------------------------------


def _raw_email(
    *,
    from_addr: str = "carolina@distributor.example",
    to_addr: str = "daniel+carolina-fresh-produce-co@getserviceledger.com",
    subject: str = "Re: [RFP-1] Quote",
    in_reply_to: str | None = None,
    message_id: str = "<reply-abc@distributor.example>",
    body: str = "Kale: $4/lb, MOQ 20 lb, delivery 2 days, net 30.",
) -> bytes:
    headers = [
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        f"Date: {formatdate(usegmt=True)}",
        f"Message-ID: {message_id}",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
        headers.append(f"References: {in_reply_to}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")


async def _seed_outbound(
    db_session, message_id: str, distributor_name: str = "Carolina Fresh Produce Co."
) -> int:
    """Fixture: a restaurant, a distributor, an RFP, and one outbound email."""
    r = Restaurant(name="Sweetgreen Test", city="Charlotte", state="NC")
    d = Distributor(
        name=distributor_name,
        specialties=["produce"],
        source="seed",
        email="orders@example.com",
        latitude=Decimal("35.26"),
        longitude=Decimal("-80.84"),
    )
    db_session.add_all([r, d])
    await db_session.commit()
    await db_session.refresh(r)
    await db_session.refresh(d)
    req = RfpRequest(restaurant_id=r.id, status=RfpRequestStatus.sent)
    db_session.add(req)
    await db_session.commit()
    await db_session.refresh(req)
    out = RfpEmail(
        rfp_request_id=req.id,
        distributor_id=d.id,
        direction=EmailDirection.out,
        subject=f"[RFP-{req.id}] Quote request",
        body="Original RFP body",
        message_id=message_id,
        status=EmailStatus.sent,
        recipient_actual="daniel+carolina-fresh-produce-co@getserviceledger.com",
        recipient_nominal="orders@example.com",
    )
    db_session.add(out)
    await db_session.commit()
    await db_session.refresh(out)
    return req.id


# ---------------------------------------------------------------------------
# Happy path: each attribution tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attribute_in_reply_to(db_session) -> None:
    outbound_mid = "<rfp-1-1-abcd@getserviceledger.com>"
    rfp_id = await _seed_outbound(db_session, outbound_mid)
    raw = _raw_email(in_reply_to=outbound_mid)
    reply = parse_message(raw, uid=10, uid_validity=100, mailbox="INBOX")
    result = await attribute_reply(reply)
    assert result.method == ATTR_IN_REPLY_TO
    assert result.rfp_request_id == rfp_id
    assert result.distributor_id is not None


@pytest.mark.asyncio
async def test_attribute_plus_tag(db_session) -> None:
    rfp_id = await _seed_outbound(db_session, "<other@x>")
    raw = _raw_email(
        in_reply_to=None,  # tier 1 misses
        message_id="<new-thread@distributor.example>",
        to_addr="daniel+carolina-fresh-produce-co@getserviceledger.com",
        subject="Hello — quote attached",  # tier 3 misses too
    )
    reply = parse_message(raw, uid=11, uid_validity=100, mailbox="INBOX")
    result = await attribute_reply(reply)
    assert result.method == ATTR_PLUS_TAG
    assert result.rfp_request_id == rfp_id
    assert result.distributor_id is not None


@pytest.mark.asyncio
async def test_attribute_subject_prefix_to_rfp_only(db_session) -> None:
    rfp_id = await _seed_outbound(db_session, "<other@x>")
    raw = _raw_email(
        in_reply_to=None,
        to_addr="daniel@somewhere-unknown.example",  # no plus-tag match
        subject=f"[RFP-{rfp_id}] Cold quote from new vendor",
    )
    reply = parse_message(raw, uid=12, uid_validity=100, mailbox="INBOX")
    result = await attribute_reply(reply)
    assert result.method == ATTR_SUBJECT_PREFIX
    assert result.rfp_request_id == rfp_id
    # Strategy 3 attributes to RFP only — distributor stays NULL.
    assert result.distributor_id is None


# ---------------------------------------------------------------------------
# F2 — unattributed reply is logged honestly, never dropped, never crashes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unattributed_reply_logged_not_dropped(db_session, monkeypatch) -> None:
    """F2: a reply with no Message-ID / no plus-tag / no [RFP-id] subject
    MUST persist with status='unattributed', rfp_request_id=NULL,
    distributor_id=NULL — never raise, never silently drop."""
    monkeypatch.setattr("app.config.settings.imap_user", "user")
    monkeypatch.setattr("app.config.settings.imap_password", "pw")
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_user", "user")
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_password", "pw")

    raw = _raw_email(
        in_reply_to=None,
        to_addr="daniel@getserviceledger.com",  # no plus-tag
        subject="Random vendor outreach",  # no [RFP-id]
        message_id="<unrelated@example.com>",
    )

    with patch(
        "app.services.inbox_monitor._fetch_unseen_messages_sync",
        return_value=(100, [(42, raw)]),
    ):
        result = await poll_inbox()

    assert result.error is None
    assert result.inbound_count == 1
    assert result.unattributed_count == 1
    assert result.attributed_count == 0

    row = (
        await db_session.execute(select(RfpEmail).where(RfpEmail.direction == EmailDirection.in_))
    ).scalar_one()
    assert row.attribution_method == ATTR_UNATTRIBUTED
    assert row.rfp_request_id is None
    assert row.distributor_id is None
    assert row.status == EmailStatus.received  # status='received', not 'failed'
    assert row.body  # body is preserved


# ---------------------------------------------------------------------------
# F1 — idempotency: same UID processed twice yields ONE rfp_emails row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_uid_processing(db_session, monkeypatch) -> None:
    """F1: the same UID returned across two poll_inbox calls MUST produce
    exactly one rfp_emails row. The UNIQUE(mailbox, uid_validity, uid)
    constraint plus the same-transaction insert order (Amendment B)
    enforces this."""
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_user", "user")
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_password", "pw")

    raw = _raw_email(in_reply_to=None, to_addr="daniel@nope.example", subject="hi")

    with patch(
        "app.services.inbox_monitor._fetch_unseen_messages_sync",
        return_value=(200, [(99, raw)]),
    ):
        first = await poll_inbox()
        second = await poll_inbox()

    assert first.inbound_count == 1
    assert second.inbound_count == 0
    assert second.duplicate_uids_skipped == 1

    emails = (
        (await db_session.execute(select(RfpEmail).where(RfpEmail.direction == EmailDirection.in_)))
        .scalars()
        .all()
    )
    assert len(emails) == 1

    uids = (
        (await db_session.execute(select(ImapSeenUid).where(ImapSeenUid.uid == 99))).scalars().all()
    )
    assert len(uids) == 1


# ---------------------------------------------------------------------------
# F8 — IMAP failure is non-fatal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_imap_connection_failure_returns_cleanly(monkeypatch) -> None:
    """F8: a connection / auth / network failure MUST NOT raise to the
    caller. It returns InboxPollResult.error set."""
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_user", "user")
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_password", "pw")

    def _fail() -> None:
        import imaplib

        raise imaplib.IMAP4.error("authentication failed")

    with patch(
        "app.services.inbox_monitor._fetch_unseen_messages_sync",
        side_effect=lambda: _fail(),
    ):
        result = await poll_inbox()

    assert result.error is not None
    assert "imap" in result.error.lower()
    assert result.inbound_count == 0


@pytest.mark.asyncio
async def test_poll_inbox_skips_when_no_creds(monkeypatch) -> None:
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_user", "")
    monkeypatch.setattr("app.services.inbox_monitor.settings.imap_password", "")
    result = await poll_inbox()
    assert result.error and "credentials" in result.error
    assert result.inbound_count == 0
