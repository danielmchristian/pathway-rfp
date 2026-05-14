"""Phase 6 — Follow-up agent.

Sends at most ONE follow-up per (rfp_request_id, distributor_id). The
cap is enforced atomically at the DB level via a partial unique index
on `rfp_emails (rfp_request_id, distributor_id) WHERE is_followup=true`
(migration 0004, Amendment A). The application catches the resulting
IntegrityError on conflict and logs `followup.skipped.cap_reached` —
F3 is therefore a DB invariant, not just an application convention.

A follow-up is only triggered by an INBOUND reply being parsed (the
quote_pipeline orchestrator drives it). Never by a timer, never
recursively (F4).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import SessionLocal
from app.llm import MODEL_ID
from app.llm.client import get_client
from app.llm.tools import COMPOSE_FOLLOWUP_EMAIL
from app.llm.usage import traced_call
from app.models.distributor import Distributor
from app.models.quote import Quote
from app.models.restaurant import Restaurant
from app.models.rfp import EmailDirection, EmailStatus, RfpEmail
from app.services.email_sender import RESEND_URL, _demo_recipient
from app.utils.http_retry import request_with_retry

log = structlog.get_logger("followup_agent")

FOLLOWUP_STAGE = "followup_compose"
MAX_TOKENS = 800


@dataclass
class FollowupResult:
    sent: bool
    skipped_reason: str | None
    rfp_email_id: int | None
    message_id: str | None
    resend_id: str | None
    missing_fields_asked: list[str]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _aggregate_missing_fields(quotes: list[Quote]) -> dict[str, list[str]]:
    """Group missing fields by ingredient so the follow-up is specific.

    Returns {ingredient_name_or_id: [missing_fields]} as a flat dict
    keyed on the SQLAlchemy ingredient_id (we look up names in the
    compose step). Items with empty missing_fields are excluded.
    """
    out: dict[int, list[str]] = {}
    for q in quotes:
        if q.missing_fields:
            out[q.ingredient_id] = list(q.missing_fields)
    return out


async def _compose_followup_via_claude(
    *,
    restaurant_name: str,
    distributor: Distributor,
    asked_lines: list[str],
    inbound_body: str,
) -> tuple[str, str]:
    """Returns (subject_tail, body)."""
    user_msg = (
        f"Compose a single follow-up email FROM {restaurant_name}'s procurement "
        f"team TO {distributor.name}. They replied to our RFP but their quote "
        f"is incomplete. Ask SPECIFICALLY and ONLY for the missing fields "
        f"below; do NOT re-ask for fields they already provided. Keep it under "
        f"150 words.\n\n"
        f"DISTRIBUTOR'S REPLY (excerpt):\n---\n"
        f"{inbound_body[:1500]}\n---\n\n"
        f"MISSING FIELDS THEY STILL OWE US:\n"
        + "\n".join(f"  - {line}" for line in asked_lines)
        + "\n\nUse the compose_followup_email tool."
    )

    client = get_client()
    async with traced_call(FOLLOWUP_STAGE) as t:
        resp = await client.messages.create(
            model=MODEL_ID,
            max_tokens=MAX_TOKENS,
            tools=[COMPOSE_FOLLOWUP_EMAIL],
            tool_choice={"type": "tool", "name": "compose_followup_email"},
            messages=[{"role": "user", "content": user_msg}],
        )
        t.bind(resp)
    tool_use = next(
        (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_use is None:
        raise RuntimeError(
            f"compose_followup_email: no tool_use block; stop_reason={resp.stop_reason}"
        )
    payload = tool_use.input
    if isinstance(payload, str):
        payload = json.loads(payload)
    subject_tail = (payload.get("subject_tail") or "Follow-up on RFP").strip()
    body = (payload.get("body") or "").strip()
    if not body:
        raise RuntimeError("compose_followup_email returned empty body")
    return subject_tail, body


async def maybe_send_followup(
    *,
    rfp_request_id: int,
    distributor_id: int,
    parent_inbound_email: RfpEmail,
    incomplete_quotes: list[Quote],
) -> FollowupResult:
    """Compose + send ONE follow-up. F3-safe via DB partial unique index.

    Caller is responsible for only invoking this when there's actually
    something to ask about (incomplete_quotes non-empty).
    """
    if not incomplete_quotes:
        return FollowupResult(
            sent=False,
            skipped_reason="no_missing_fields",
            rfp_email_id=None,
            message_id=None,
            resend_id=None,
            missing_fields_asked=[],
        )

    # Pre-flight application check — saves a Claude call + Resend POST
    # if we already follow-ed up. The DB unique index is still the
    # invariant; this just avoids the wasted work in the common case.
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(RfpEmail.id).where(
                    RfpEmail.rfp_request_id == rfp_request_id,
                    RfpEmail.distributor_id == distributor_id,
                    RfpEmail.is_followup.is_(True),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            log.info(
                "followup.skipped.cap_reached_preflight",
                rfp_request_id=rfp_request_id,
                distributor_id=distributor_id,
                existing_email_id=existing,
            )
            return FollowupResult(
                sent=False,
                skipped_reason="cap_reached",
                rfp_email_id=None,
                message_id=None,
                resend_id=None,
                missing_fields_asked=[],
            )

        distributor = await session.get(Distributor, distributor_id)
        rfp_email_parent = await session.get(RfpEmail, parent_inbound_email.id)
        if distributor is None or rfp_email_parent is None:
            return FollowupResult(
                sent=False,
                skipped_reason="missing_fk_target",
                rfp_email_id=None,
                message_id=None,
                resend_id=None,
                missing_fields_asked=[],
            )
        rfp_req = await session.get(
            type(rfp_email_parent).rfp_request.property.mapper.class_, rfp_request_id
        )
        restaurant = await session.get(Restaurant, rfp_req.restaurant_id) if rfp_req else None
        restaurant_name = restaurant.name if restaurant else "our procurement team"

        # Build human-readable lines: "Shredded Kale: min_order_qty, delivery_days"
        asked_lines: list[str] = []
        for q in incomplete_quotes:
            from app.models.ingredient import Ingredient

            ing = await session.get(Ingredient, q.ingredient_id)
            ing_name = ing.name if ing else f"ingredient {q.ingredient_id}"
            asked_lines.append(f"{ing_name}: {', '.join(q.missing_fields or [])}")

    try:
        subject_tail, body = await _compose_followup_via_claude(
            restaurant_name=restaurant_name,
            distributor=distributor,
            asked_lines=asked_lines,
            inbound_body=rfp_email_parent.body or "",
        )
    except Exception as exc:
        log.exception("followup.compose.failed", distributor_id=distributor_id)
        return FollowupResult(
            sent=False,
            skipped_reason=f"compose_error: {type(exc).__name__}",
            rfp_email_id=None,
            message_id=None,
            resend_id=None,
            missing_fields_asked=[],
        )

    # Mint a follow-up Message-ID with `fu1` marker so future-us can grep
    # for follow-ups in the raw inbox.
    _, _, domain = settings.rfp_from_email.partition("@")
    domain = domain or "getserviceledger.com"
    message_id = f"<rfp-{rfp_request_id}-{distributor_id}-fu1-" f"{secrets.token_hex(4)}@{domain}>"
    actual = _demo_recipient(distributor.name, settings.rfp_demo_inbox)
    nominal = distributor.email
    subject = f"Re: [RFP-{rfp_request_id}] {subject_tail}"

    payload = {
        "from": settings.rfp_from_email,
        "to": [actual],
        "reply_to": settings.rfp_from_email,
        "subject": subject,
        "text": body,
        "headers": {
            "Message-ID": message_id,
            # Thread under the distributor's reply so Gmail groups it.
            "In-Reply-To": parent_inbound_email.message_id or "",
            "References": parent_inbound_email.message_id or "",
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }

    resend_id: str | None = None
    sent_ok = False
    if not settings.resend_api_key:
        log.warning("followup.send.skipped_no_api_key")
        send_error = "RESEND_API_KEY not configured"
    else:
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await request_with_retry(
                http,
                "POST",
                RESEND_URL,
                json=payload,
                headers=headers,
                label=f"resend.followup distributor={distributor_id}",
            )
        if response is None:
            send_error = "resend: retries exhausted"
        elif response.status_code >= 300:
            send_error = f"resend status {response.status_code}: {response.text[:300]}"
        else:
            send_error = None
            sent_ok = True
            try:
                resend_id = (response.json() or {}).get("id")
            except ValueError:
                resend_id = None

    # Persist the follow-up rfp_emails row. The partial unique index
    # enforces F3 atomically here. On conflict we mark as cap_reached.
    async with SessionLocal() as session:
        try:
            async with session.begin():
                row = RfpEmail(
                    rfp_request_id=rfp_request_id,
                    distributor_id=distributor_id,
                    direction=EmailDirection.out,
                    subject=subject,
                    body=body,
                    message_id=message_id,
                    in_reply_to=parent_inbound_email.message_id,
                    status=(EmailStatus.sent if sent_ok else EmailStatus.failed),
                    sent_at=(parent_inbound_email.received_at if sent_ok else None),
                    recipient_actual=actual,
                    recipient_nominal=nominal,
                    resend_id=resend_id,
                    raw_payload={
                        "followup_for_email_id": parent_inbound_email.id,
                        "send_error": send_error,
                        "missing_field_lines": asked_lines,
                    },
                    is_followup=True,
                )
                session.add(row)
                await session.flush()
                row_id = row.id
        except IntegrityError as exc:
            # Amendment A — DB invariant fired. Someone else (or a
            # concurrent poll batch) already inserted a follow-up for
            # this (rfp_request, distributor). Treat as cap reached.
            log.info(
                "followup.skipped.cap_reached",
                rfp_request_id=rfp_request_id,
                distributor_id=distributor_id,
                err=str(exc.orig)[:200],
            )
            return FollowupResult(
                sent=False,
                skipped_reason="cap_reached",
                rfp_email_id=None,
                message_id=None,
                resend_id=None,
                missing_fields_asked=[],
            )

    log.info(
        "followup.sent" if sent_ok else "followup.persist_failed",
        rfp_request_id=rfp_request_id,
        distributor_id=distributor_id,
        rfp_email_id=row_id,
        sent_ok=sent_ok,
    )
    return FollowupResult(
        sent=sent_ok,
        skipped_reason=None if sent_ok else "send_failed",
        rfp_email_id=row_id,
        message_id=message_id,
        resend_id=resend_id,
        missing_fields_asked=asked_lines,
    )
