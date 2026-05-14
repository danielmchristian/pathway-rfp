"""Phase 6 — IMAP inbox monitor with 3-tier attribution + atomic
idempotency.

Design notes:
  * stdlib `imaplib` + `asyncio.to_thread` (no new deps).
  * `email.message_from_bytes` for parsing fetched messages (no
    hand-rolled header extraction).
  * Amendment B — `imap_seen_uids` and the corresponding `rfp_emails`
    row are persisted in the SAME transaction so a crash between fetch
    and INSERT cannot silently lose a reply. A duplicate fetch on the
    next poll is harmless (the UNIQUE constraint catches it).
  * F8 — IMAP failures (connection refused, auth, network drop) are
    caught and surfaced via the InboxPollResult.error field; we never
    raise to the API caller for transient mail-server problems.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import re
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.models.distributor import Distributor
from app.models.imap_seen_uid import ImapSeenUid
from app.models.rfp import EmailDirection, EmailStatus, RfpEmail, RfpRequest

log = structlog.get_logger("inbox_monitor")

ATTR_IN_REPLY_TO = "in_reply_to"
ATTR_PLUS_TAG = "plus_tag"
ATTR_SUBJECT_PREFIX = "subject_prefix"
ATTR_UNATTRIBUTED = "unattributed"

PARSE_STATUS_UNPARSED = "unparsed"

# `Message-ID` is RFC-canonical; some clients output `Message-Id`.
_SUBJECT_RFP_RE = re.compile(r"\[RFP-(\d+)\]", re.IGNORECASE)
# daniel+slug@... → group 1 = slug.
_PLUS_TAG_RE = re.compile(r"^[^+@]+\+([^@]+)@", re.IGNORECASE)
# Strip everything except alnum so we can match against the distributor
# name slug we generate at send time (see email_sender._distributor_slug).
_SLUG_NORM_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class InboundReply:
    uid: int
    uid_validity: int
    mailbox: str
    message_id: str | None
    in_reply_to: str | None
    references: str | None
    subject: str | None
    from_addr: str | None
    to_addrs: list[str]
    body_text: str
    received_at: datetime
    raw_headers: dict[str, str]


@dataclass
class AttributionResult:
    method: str
    rfp_request_id: int | None
    distributor_id: int | None
    matched_rfp_email_id: int | None


@dataclass
class InboxPollResult:
    inbound_count: int = 0
    attributed_count: int = 0
    unattributed_count: int = 0
    duplicate_uids_skipped: int = 0
    persisted_email_ids: list[int] = field(default_factory=list)
    error: str | None = None
    uid_validity: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "inbound_count": self.inbound_count,
            "attributed_count": self.attributed_count,
            "unattributed_count": self.unattributed_count,
            "duplicate_uids_skipped": self.duplicate_uids_skipped,
            "persisted_email_ids": list(self.persisted_email_ids),
            "error": self.error,
            "uid_validity": self.uid_validity,
        }


# ---------------------------------------------------------------------------
# IMAP fetch (sync, runs under asyncio.to_thread)
# ---------------------------------------------------------------------------


def _fetch_unseen_messages_sync() -> tuple[int, list[tuple[int, bytes]]]:
    """Connect, SELECT INBOX, SEARCH UNSEEN, FETCH each.

    Returns (uid_validity, [(uid, raw_message_bytes), ...]).

    Raises on connection / auth failures — the async wrapper catches.
    """
    ctx = ssl.create_default_context()
    with imaplib.IMAP4_SSL(
        host=settings.imap_host, port=settings.imap_port, ssl_context=ctx
    ) as conn:
        conn.login(settings.imap_user, settings.imap_password)
        typ, _data = conn.select(settings.imap_mailbox, readonly=False)
        if typ != "OK":
            raise RuntimeError(f"IMAP SELECT failed: {typ}")

        # UIDVALIDITY — store alongside UID so a mailbox UID reset
        # doesn't make us think a brand-new email is one we've seen.
        typ, uidv_data = conn.response("UIDVALIDITY")
        uid_validity = int(uidv_data[0]) if uidv_data and uidv_data[0] else 0

        typ, search_data = conn.uid("SEARCH", None, "UNSEEN")
        if typ != "OK":
            raise RuntimeError(f"IMAP SEARCH failed: {typ}")
        uids_raw = search_data[0].split() if search_data and search_data[0] else []

        messages: list[tuple[int, bytes]] = []
        for uid_bytes in uids_raw:
            uid = int(uid_bytes)
            typ, fetch_data = conn.uid("FETCH", uid_bytes, "(BODY.PEEK[])")
            if typ != "OK" or not fetch_data:
                log.warning("imap.fetch.skip", uid=uid, status=typ)
                continue
            # fetch_data shape: [(b'1 (UID 12 BODY[] {N}', b'<raw>'), b')']
            raw = next(
                (part[1] for part in fetch_data if isinstance(part, tuple)),
                None,
            )
            if raw is None:
                continue
            messages.append((uid, raw))
        return uid_validity, messages


# ---------------------------------------------------------------------------
# Parsing & attribution
# ---------------------------------------------------------------------------


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_body_text(msg: Message) -> str:
    """Prefer text/plain; fall back to text/html stripped of tags."""
    if msg.is_multipart():
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                plain_parts.append(_decode_part(part))
            elif ct == "text/html":
                html_parts.append(_decode_part(part))
        if plain_parts:
            return "\n".join(plain_parts).strip()
        if html_parts:
            from bs4 import BeautifulSoup

            return BeautifulSoup("\n".join(html_parts), "lxml").get_text("\n").strip()
        return ""
    # Single-part — use directly.
    if msg.get_content_type() == "text/html":
        from bs4 import BeautifulSoup

        return BeautifulSoup(_decode_part(msg), "lxml").get_text("\n").strip()
    return _decode_part(msg).strip()


def parse_message(raw: bytes, uid: int, uid_validity: int, mailbox: str) -> InboundReply:
    """`email.message_from_bytes` does the heavy lifting; no hand-rolled regex."""
    msg = email.message_from_bytes(raw)
    headers = {k: v for k, v in msg.items()}
    received_at = datetime.now(UTC)
    if (date_hdr := msg.get("Date")):
        import contextlib

        with contextlib.suppress(TypeError, ValueError):
            received_at = parsedate_to_datetime(date_hdr) or received_at
    to_addrs = [
        addr for _name, addr in getaddresses(msg.get_all("To", []) + msg.get_all("Delivered-To", []))
        if addr
    ]
    from_pair = getaddresses(msg.get_all("From", []))
    from_addr = from_pair[0][1] if from_pair else None
    return InboundReply(
        uid=uid,
        uid_validity=uid_validity,
        mailbox=mailbox,
        message_id=(msg.get("Message-ID") or msg.get("Message-Id") or "").strip() or None,
        in_reply_to=(msg.get("In-Reply-To") or "").strip() or None,
        references=(msg.get("References") or "").strip() or None,
        subject=msg.get("Subject"),
        from_addr=from_addr,
        to_addrs=to_addrs,
        body_text=_extract_body_text(msg),
        received_at=received_at,
        raw_headers=headers,
    )


def _normalize_slug(s: str) -> str:
    return _SLUG_NORM_RE.sub("-", (s or "").lower()).strip("-")


async def attribute_reply(reply: InboundReply) -> AttributionResult:
    """Three-tier attribution. Falls back to 'unattributed' on no match."""
    async with SessionLocal() as session:
        # Tier 1 — In-Reply-To / References → rfp_emails.message_id (outbound)
        candidate_mids: list[str] = []
        if reply.in_reply_to:
            candidate_mids.append(reply.in_reply_to)
        if reply.references:
            # References can be a whitespace-separated list of message-ids.
            candidate_mids.extend(reply.references.split())
        for mid in candidate_mids:
            mid = mid.strip()
            if not mid:
                continue
            stmt = select(RfpEmail).where(
                RfpEmail.message_id == mid,
                RfpEmail.direction == EmailDirection.out,
            )
            match = (await session.execute(stmt)).scalar_one_or_none()
            if match is not None:
                return AttributionResult(
                    method=ATTR_IN_REPLY_TO,
                    rfp_request_id=match.rfp_request_id,
                    distributor_id=match.distributor_id,
                    matched_rfp_email_id=match.id,
                )

        # Tier 2 — Plus-tag in any To / Delivered-To → distributor slug.
        for addr in reply.to_addrs:
            m = _PLUS_TAG_RE.match(addr)
            if not m:
                continue
            slug = _normalize_slug(m.group(1))
            distributors = (await session.execute(select(Distributor))).scalars().all()
            for d in distributors:
                if _normalize_slug(d.name) == slug:
                    # Find the most recent RFP this distributor was on.
                    latest = (
                        await session.execute(
                            select(RfpEmail)
                            .where(
                                RfpEmail.distributor_id == d.id,
                                RfpEmail.direction == EmailDirection.out,
                            )
                            .order_by(RfpEmail.id.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    return AttributionResult(
                        method=ATTR_PLUS_TAG,
                        rfp_request_id=latest.rfp_request_id if latest else None,
                        distributor_id=d.id,
                        matched_rfp_email_id=latest.id if latest else None,
                    )

        # Tier 3 — [RFP-{id}] in subject. RFP only; distributor stays NULL.
        if reply.subject:
            sm = _SUBJECT_RFP_RE.search(reply.subject)
            if sm:
                rid = int(sm.group(1))
                rfp = await session.get(RfpRequest, rid)
                if rfp is not None:
                    return AttributionResult(
                        method=ATTR_SUBJECT_PREFIX,
                        rfp_request_id=rid,
                        distributor_id=None,
                        matched_rfp_email_id=None,
                    )

    # No match — honest "unattributed" rather than silent drop.
    return AttributionResult(
        method=ATTR_UNATTRIBUTED,
        rfp_request_id=None,
        distributor_id=None,
        matched_rfp_email_id=None,
    )


# ---------------------------------------------------------------------------
# Atomic persistence (Amendment B)
# ---------------------------------------------------------------------------


async def _persist_atomic(
    reply: InboundReply, attribution: AttributionResult
) -> int | None:
    """Insert rfp_emails + imap_seen_uids in a SINGLE transaction.

    Returns the inserted rfp_email_id, or None if the UID was already
    seen (UNIQUE conflict — duplicate fetch, safely skipped).
    """
    async with SessionLocal() as session:
        try:
            async with session.begin():
                rfp_email = RfpEmail(
                    rfp_request_id=attribution.rfp_request_id,
                    distributor_id=attribution.distributor_id,
                    direction=EmailDirection.in_,
                    subject=reply.subject,
                    body=reply.body_text,
                    message_id=reply.message_id,
                    in_reply_to=reply.in_reply_to,
                    status=EmailStatus.received,
                    received_at=reply.received_at,
                    raw_payload={
                        "headers": reply.raw_headers,
                        "to_addrs": reply.to_addrs,
                        "from_addr": reply.from_addr,
                        "uid": reply.uid,
                    },
                    attribution_method=attribution.method,
                    parse_status=PARSE_STATUS_UNPARSED,
                )
                session.add(rfp_email)
                await session.flush()
                rfp_email_id = rfp_email.id
                session.add(
                    ImapSeenUid(
                        mailbox=reply.mailbox,
                        uid_validity=reply.uid_validity,
                        uid=reply.uid,
                        rfp_email_id=rfp_email_id,
                    )
                )
            return rfp_email_id
        except IntegrityError as exc:
            # UNIQUE(mailbox, uid_validity, uid) conflict — duplicate fetch.
            # Safe to skip; the original row is already there.
            log.info(
                "inbox.duplicate_uid.skipped",
                uid=reply.uid,
                uid_validity=reply.uid_validity,
                err=str(exc.orig)[:200],
            )
            return None


# ---------------------------------------------------------------------------
# Top-level poll cycle (F8 — IMAP failures non-fatal)
# ---------------------------------------------------------------------------


async def poll_inbox() -> InboxPollResult:
    """One IMAP poll cycle. Non-fatal on connection / auth errors."""
    if not (settings.imap_user and settings.imap_password):
        log.warning("inbox.poll.skipped_no_creds")
        return InboxPollResult(error="IMAP credentials not configured")

    result = InboxPollResult()
    try:
        uid_validity, messages = await asyncio.to_thread(_fetch_unseen_messages_sync)
    except (imaplib.IMAP4.error, OSError, ssl.SSLError, RuntimeError) as exc:
        log.warning(
            "inbox.connect.failed", error_type=type(exc).__name__, msg=str(exc)
        )
        return InboxPollResult(error=f"imap: {type(exc).__name__}: {exc}")

    result.uid_validity = uid_validity

    for uid, raw in messages:
        try:
            reply = parse_message(raw, uid=uid, uid_validity=uid_validity, mailbox=settings.imap_mailbox)
        except Exception as exc:  # noqa: BLE001 — defensive
            log.exception("inbox.parse.failed", uid=uid)
            # Still record the UID so we don't loop forever on a bad message.
            await _record_unparseable_uid(uid=uid, uid_validity=uid_validity, error=str(exc))
            continue

        # Skip our own outbound emails — they land in the same Workspace
        # inbox via plus-addressing (procurement@ alias on daniel@). Mark
        # the UID seen so we don't re-fetch.
        if reply.from_addr and reply.from_addr.lower() == settings.rfp_from_email.lower():
            log.info("inbox.skip.own_send", uid=uid, from_addr=reply.from_addr)
            await _record_unparseable_uid(uid=uid, uid_validity=uid_validity, error="own_send")
            continue

        attribution = await attribute_reply(reply)
        rfp_email_id = await _persist_atomic(reply, attribution)
        if rfp_email_id is None:
            result.duplicate_uids_skipped += 1
            continue

        result.inbound_count += 1
        result.persisted_email_ids.append(rfp_email_id)
        if attribution.method == ATTR_UNATTRIBUTED:
            result.unattributed_count += 1
        else:
            result.attributed_count += 1

    log.info(
        "inbox.poll.complete",
        inbound=result.inbound_count,
        attributed=result.attributed_count,
        unattributed=result.unattributed_count,
        duplicates=result.duplicate_uids_skipped,
    )
    return result


async def _record_unparseable_uid(*, uid: int, uid_validity: int, error: str) -> None:
    """Record a UID we couldn't email.message_from_bytes so we don't refetch it."""
    async with SessionLocal() as session:
        try:
            async with session.begin():
                session.add(
                    ImapSeenUid(
                        mailbox=settings.imap_mailbox,
                        uid_validity=uid_validity,
                        uid=uid,
                        rfp_email_id=None,
                    )
                )
        except IntegrityError:
            pass  # already recorded — fine
