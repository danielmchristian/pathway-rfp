"""Phase 5 — send a composed RFP email via Resend.

Per Phase 5 demo override: the actual To address is `daniel+{slug}@<domain>`
(plus-addressed Google Workspace mailbox), but the distributor's nominal
`.example` placeholder is recorded alongside on every row.

We mint our own RFC-822 Message-ID and pass it via Resend's `headers`
parameter. Resend's response `id` is captured separately as `resend_id` —
it's an internal handle, not the on-the-wire Message-ID.

Resilience: one failed send (network, 4xx, 5xx) is persisted as
status='failed' and does NOT raise — the orchestrator continues the batch.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.config import settings
from app.models.distributor import Distributor
from app.models.rfp import EmailDirection, EmailStatus, RfpEmail
from app.services.rfp_composer import RfpEmailContent
from app.utils.http_retry import request_with_retry

log = structlog.get_logger("email_sender")

RESEND_URL = "https://api.resend.com/emails"
_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass
class SendResult:
    ok: bool
    rfp_email: RfpEmail
    error: str | None = None


def _distributor_slug(name: str) -> str:
    """`Carolina Fresh Foods Inc.` → `carolina-fresh-foods-inc`. 40 char cap."""
    s = _SLUG_NON_ALNUM.sub("-", (name or "").lower()).strip("-")
    return s[:40] or "distributor"


def _demo_recipient(distributor_name: str, demo_inbox: str) -> str:
    """daniel@getserviceledger.com + slug → daniel+slug@getserviceledger.com."""
    local, _, domain = demo_inbox.partition("@")
    if not domain:
        raise ValueError(f"RFP_DEMO_INBOX must be a full email address: {demo_inbox!r}")
    return f"{local}+{_distributor_slug(distributor_name)}@{domain}"


def _mint_message_id(*, rfp_request_id: int, distributor_id: int, from_email: str) -> str:
    """`<rfp-{req}-{dist}-{hex}@domain>` — Phase 6 reply matcher key."""
    _, _, domain = from_email.partition("@")
    domain = domain or "getserviceledger.com"
    return f"<rfp-{rfp_request_id}-{distributor_id}-{secrets.token_hex(4)}@{domain}>"


async def send_rfp_email(
    *,
    rfp_request_id: int,
    distributor: Distributor,
    content: RfpEmailContent,
    subject: str,
    http: httpx.AsyncClient,
) -> SendResult:
    """Send one email via Resend. Always returns a SendResult — never raises."""
    actual = _demo_recipient(distributor.name, settings.rfp_demo_inbox)
    nominal = distributor.email
    message_id = _mint_message_id(
        rfp_request_id=rfp_request_id,
        distributor_id=distributor.id,
        from_email=settings.rfp_from_email,
    )

    payload: dict[str, Any] = {
        "from": settings.rfp_from_email,
        "to": [actual],
        "reply_to": settings.rfp_from_email,
        "subject": subject,
        "text": content.body,
        "headers": {"Message-ID": message_id},
    }
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }

    if not settings.resend_api_key:
        rfp_email = _build_failed_row(
            rfp_request_id=rfp_request_id,
            distributor_id=distributor.id,
            subject=subject,
            body=content.body,
            message_id=message_id,
            recipient_actual=actual,
            recipient_nominal=nominal,
            error="RESEND_API_KEY is not configured",
        )
        return SendResult(ok=False, rfp_email=rfp_email, error="RESEND_API_KEY missing")

    response = await request_with_retry(
        http,
        "POST",
        RESEND_URL,
        json=payload,
        headers=headers,
        label=f"resend.send distributor={distributor.id}",
    )

    if response is None:
        rfp_email = _build_failed_row(
            rfp_request_id=rfp_request_id,
            distributor_id=distributor.id,
            subject=subject,
            body=content.body,
            message_id=message_id,
            recipient_actual=actual,
            recipient_nominal=nominal,
            error="resend: retries exhausted",
        )
        return SendResult(ok=False, rfp_email=rfp_email, error="retries exhausted")

    if response.status_code >= 300:
        body_text = response.text[:1000]
        log.warning(
            "resend.send.failed",
            status=response.status_code,
            body=body_text,
            distributor=distributor.name,
        )
        rfp_email = _build_failed_row(
            rfp_request_id=rfp_request_id,
            distributor_id=distributor.id,
            subject=subject,
            body=content.body,
            message_id=message_id,
            recipient_actual=actual,
            recipient_nominal=nominal,
            error=f"resend status {response.status_code}: {body_text}",
        )
        return SendResult(
            ok=False,
            rfp_email=rfp_email,
            error=f"resend status {response.status_code}",
        )

    resp_json: dict[str, Any] = {}
    try:
        resp_json = response.json()
    except ValueError:
        resp_json = {"raw_text": response.text[:500]}
    resend_id = resp_json.get("id") if isinstance(resp_json, dict) else None

    rfp_email = RfpEmail(
        rfp_request_id=rfp_request_id,
        distributor_id=distributor.id,
        direction=EmailDirection.out,
        subject=subject,
        body=content.body,
        message_id=message_id,
        status=EmailStatus.sent,
        sent_at=datetime.now(UTC),
        recipient_actual=actual,
        recipient_nominal=nominal,
        resend_id=resend_id,
        raw_payload={
            "resend_response": resp_json,
            "request": {
                "from": payload["from"],
                "to": payload["to"],
                "reply_to": payload["reply_to"],
                "subject": payload["subject"],
                # Body omitted — we already store it on the column.
                "headers": payload["headers"],
            },
        },
    )
    log.info(
        "resend.send.ok",
        distributor=distributor.name,
        message_id=message_id,
        resend_id=resend_id,
    )
    return SendResult(ok=True, rfp_email=rfp_email)


def _build_failed_row(
    *,
    rfp_request_id: int,
    distributor_id: int,
    subject: str,
    body: str,
    message_id: str,
    recipient_actual: str,
    recipient_nominal: str | None,
    error: str,
) -> RfpEmail:
    return RfpEmail(
        rfp_request_id=rfp_request_id,
        distributor_id=distributor_id,
        direction=EmailDirection.out,
        subject=subject,
        body=body,
        message_id=message_id,
        status=EmailStatus.failed,
        sent_at=None,
        recipient_actual=recipient_actual,
        recipient_nominal=recipient_nominal,
        resend_id=None,
        raw_payload={"error": error},
    )
