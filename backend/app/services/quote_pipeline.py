"""Phase 6 — Quote collection pipeline orchestrator.

One on-demand poll cycle:
  1. inbox_monitor.poll_inbox() — fetch new UIDs, attribute, persist.
  2. For each persisted inbound rfp_email_id with an rfp_request_id:
       parse_quote_email(); on raise: parse_status='parse_failed', continue (F7).
       If any quote has missing_fields → maybe_send_followup (atomic cap).
  3. compute_for_rfp(rfp_request_id) — only writes a recommendation row
     if (all_replied OR deadline_passed) unless force=True.

@stage("quote_collection") emits the umbrella start/complete; inner
substage events (inbox_poll, quote_parse, followup_sent, recommendation)
fire manually so the SSE stream is granular.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select

from app.db import SessionLocal
from app.models.quote import Quote
from app.models.rfp import RfpEmail
from app.pipeline.events import Event, get_bus, stage
from app.services.followup_agent import FollowupResult, maybe_send_followup
from app.services.inbox_monitor import (
    ATTR_UNATTRIBUTED,
    InboxPollResult,
    poll_inbox,
)
from app.services.quote_parser import (
    PARSE_STATUS_PARSE_FAILED,
    ParsedQuotesResult,
    parse_quote_email,
)
from app.services.recommender import (
    RecommendationResult,
    compute_for_rfp,
)

log = structlog.get_logger("quote_pipeline")

STAGE = "quote_collection"
SUBSTAGE_POLL = "inbox_poll"
SUBSTAGE_PARSE = "quote_parse"
SUBSTAGE_FOLLOWUP = "followup"
SUBSTAGE_RECOMMENDATION = "recommendation"


@dataclass
class QuoteCollectionResult:
    rfp_request_id: int
    poll: InboxPollResult
    parse_results: list[ParsedQuotesResult] = field(default_factory=list)
    parse_failed_email_ids: list[int] = field(default_factory=list)
    followups: list[FollowupResult] = field(default_factory=list)
    recommendation: RecommendationResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rfp_request_id": self.rfp_request_id,
            "poll": self.poll.to_dict(),
            "parse_results": [p.to_dict() for p in self.parse_results],
            "parse_failed_email_ids": list(self.parse_failed_email_ids),
            "followups": [f.to_dict() for f in self.followups],
            "recommendation": (
                self.recommendation.to_dict() if self.recommendation else None
            ),
        }


async def _mark_parse_failed(rfp_email_id: int, error: str) -> None:
    """Set parse_status='parse_failed' + record the error in raw_payload."""
    async with SessionLocal() as session, session.begin():
        email_row = await session.get(RfpEmail, rfp_email_id)
        if email_row is None:
            return
        email_row.parse_status = PARSE_STATUS_PARSE_FAILED
        payload = dict(email_row.raw_payload or {})
        payload["parse_error"] = error[:1000]
        email_row.raw_payload = payload


async def _incomplete_quotes_for(
    rfp_email_id: int,
) -> tuple[int | None, int | None, list[Quote]]:
    """Return (rfp_request_id, distributor_id, [quote rows with missing_fields])."""
    async with SessionLocal() as session:
        email_row = await session.get(RfpEmail, rfp_email_id)
        if email_row is None:
            return None, None, []
        if email_row.rfp_request_id is None or email_row.distributor_id is None:
            return None, None, []
        quotes = (
            await session.execute(
                select(Quote).where(
                    Quote.source_email_id == rfp_email_id,
                )
            )
        ).scalars().all()
        incomplete = [q for q in quotes if q.missing_fields]
        return email_row.rfp_request_id, email_row.distributor_id, incomplete


@stage(STAGE)
async def poll_and_process(
    *,
    restaurant_id: int,
    rfp_request_id: int,
    force_recommendation: bool = False,
) -> QuoteCollectionResult:
    """One full poll cycle for a single RFP.

    The `restaurant_id` parameter is required by the @stage decorator
    (SSE events are keyed on restaurant). The actual work scopes to
    `rfp_request_id`.
    """
    bus = get_bus()
    result = QuoteCollectionResult(rfp_request_id=rfp_request_id, poll=InboxPollResult())

    # ---- 1. Inbox poll -------------------------------------------------
    bus.emit(
        Event(
            restaurant_id=restaurant_id,
            stage=SUBSTAGE_POLL,
            status="start",
            payload={"rfp_request_id": rfp_request_id},
        )
    )
    poll_result = await poll_inbox()
    result.poll = poll_result
    bus.emit(
        Event(
            restaurant_id=restaurant_id,
            stage=SUBSTAGE_POLL,
            status="complete",
            payload=poll_result.to_dict(),
        )
    )

    # ---- 2. Parse each persisted inbound -------------------------------
    for email_id in poll_result.persisted_email_ids:
        bus.emit(
            Event(
                restaurant_id=restaurant_id,
                stage=SUBSTAGE_PARSE,
                status="start",
                payload={"rfp_email_id": email_id},
            )
        )
        try:
            parsed = await parse_quote_email(rfp_email_id=email_id)
            result.parse_results.append(parsed)
            bus.emit(
                Event(
                    restaurant_id=restaurant_id,
                    stage=SUBSTAGE_PARSE,
                    status="complete",
                    payload=parsed.to_dict(),
                )
            )
        except Exception as exc:  # noqa: BLE001 — F7 invariant
            log.exception("quote.parse.crashed", rfp_email_id=email_id)
            await _mark_parse_failed(email_id, str(exc))
            result.parse_failed_email_ids.append(email_id)
            bus.emit(
                Event(
                    restaurant_id=restaurant_id,
                    stage=SUBSTAGE_PARSE,
                    status="error",
                    payload={
                        "rfp_email_id": email_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
            )
            # Critical: continue the loop. F7 — one bad reply must not
            # block the rest of the batch.
            continue

        # ---- 3. Follow-up if any quote has missing_fields --------------
        rid, did, incomplete = await _incomplete_quotes_for(email_id)
        if not incomplete or rid is None or did is None:
            continue
        bus.emit(
            Event(
                restaurant_id=restaurant_id,
                stage=SUBSTAGE_FOLLOWUP,
                status="start",
                payload={"rfp_email_id": email_id, "incomplete_count": len(incomplete)},
            )
        )
        # Fetch the parent inbound email (the reply we'll thread under).
        async with SessionLocal() as session:
            parent = await session.get(RfpEmail, email_id)
        if parent is None:
            continue
        fu = await maybe_send_followup(
            rfp_request_id=rid,
            distributor_id=did,
            parent_inbound_email=parent,
            incomplete_quotes=incomplete,
        )
        result.followups.append(fu)
        bus.emit(
            Event(
                restaurant_id=restaurant_id,
                stage=SUBSTAGE_FOLLOWUP,
                status="complete",
                payload=fu.to_dict(),
            )
        )

    # ---- 4. Maybe compute recommendation ------------------------------
    bus.emit(
        Event(
            restaurant_id=restaurant_id,
            stage=SUBSTAGE_RECOMMENDATION,
            status="start",
            payload={"rfp_request_id": rfp_request_id, "force": force_recommendation},
        )
    )
    rec = await compute_for_rfp(rfp_request_id, force=force_recommendation)
    result.recommendation = rec
    bus.emit(
        Event(
            restaurant_id=restaurant_id,
            stage=SUBSTAGE_RECOMMENDATION,
            status="complete",
            payload={
                "ready": rec.ready,
                "pick_distributor_id": (
                    rec.pick.distributor_id if rec.pick else None
                ),
                "score": rec.pick.score if rec.pick else None,
                "not_ready_reason": rec.not_ready_reason,
            },
        )
    )

    # Log non-poll-attributed counts so a curious dev can grep.
    unattributed_count = sum(
        1
        for e_id in poll_result.persisted_email_ids
        if e_id and not any(p.rfp_email_id == e_id for p in result.parse_results)
    )
    log.info(
        "quote.pipeline.complete",
        rfp_request_id=rfp_request_id,
        inbound=poll_result.inbound_count,
        parsed=len(result.parse_results),
        parse_failed=len(result.parse_failed_email_ids),
        followups=len(result.followups),
        unattributed=unattributed_count,
    )
    # Silence unused-import lint.
    _ = ATTR_UNATTRIBUTED
    return result
