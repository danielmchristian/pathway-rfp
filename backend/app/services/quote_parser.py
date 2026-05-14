"""Phase 6 — parse a distributor's inbound reply into structured Quote rows.

Claude `parse_quote` tool. Logged under stage='quote_parse'. Handles:
  - complete quotes (all 4 fields)
  - partial quotes (missing_fields populated)
  - prose-only replies (Claude extracts what it can)
  - off-topic replies (auto-responders, OOO) — persists with empty quotes
  - quotes on ingredients not in the ask list (logged, persisted)
  - quotes on TBD-quantity items (persisted; recommender handles the TBD)

Fuzzy ingredient resolution: Claude returns the ingredient name as the
distributor wrote it. We resolve to our Ingredient.id by exact-match
against the rfp_request_items list first, then by canonical-root match.
Unmatched names are logged but the row is still persisted with
ingredient_id = the best-effort fallback (the first asked ingredient) and
a warning in the parse_confidence note.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.llm import MODEL_ID
from app.llm.client import get_client
from app.llm.tools import PARSE_QUOTE
from app.llm.usage import traced_call
from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.quote import Quote
from app.models.rfp import RfpEmail, RfpRequestItem
from app.services.distributor_matching import specialty_tags_for
from app.services.quantity_aggregator import canonical_root

log = structlog.get_logger("quote_parser")

STAGE = "quote_parse"
MAX_TOKENS = 2500

PARSE_STATUS_PARSED = "parsed"
PARSE_STATUS_PARSE_FAILED = "parse_failed"


@dataclass
class ParsedQuotesResult:
    rfp_email_id: int
    rfp_request_id: int | None
    distributor_id: int | None
    quotes_inserted: int
    off_topic: bool
    overall_parse_confidence: float | None
    note: str | None
    unmatched_ingredient_names: list[str]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


async def _resolve_ingredient_id(
    *,
    session: AsyncSession,
    rfp_request_id: int,
    distributor_id: int | None,
    quoted_name: str,
) -> tuple[int | None, bool]:
    """Resolve the distributor's free-text name to one of the asked Ingredient.ids.

    Returns (ingredient_id, matched_exactly). On no match returns (None, False).
    """
    # Pull the rfp_request_items for this RFP, joining to Ingredient name.
    rows = (
        await session.execute(
            select(Ingredient, RfpRequestItem)
            .join(RfpRequestItem, RfpRequestItem.ingredient_id == Ingredient.id)
            .where(RfpRequestItem.rfp_request_id == rfp_request_id)
        )
    ).all()
    asked = [(ing, item) for ing, item in rows]
    if not asked:
        return None, False

    q_lower = (quoted_name or "").strip().lower()
    if not q_lower:
        return None, False

    # 1. Exact name match (case-insensitive).
    for ing, _item in asked:
        if (ing.name or "").lower() == q_lower:
            return ing.id, True
    # 2. Canonical-root match (so "Shredded Kale" matches "Kale").
    q_root = canonical_root(quoted_name)
    for ing, _item in asked:
        if canonical_root(ing.name or "") == q_root:
            return ing.id, True
    # 3. Substring containment fallback.
    for ing, _item in asked:
        if q_root and q_root in (ing.name or "").lower():
            return ing.id, False
    return None, False


def _normalize_missing_fields(raw: list[Any] | None) -> list[str]:
    """Accept only the 5 canonical field names; ignore anything else Claude returns."""
    valid = {"unit_price", "unit", "min_order_qty", "delivery_days", "terms"}
    return [m for m in (raw or []) if isinstance(m, str) and m in valid]


def _add_missing_from_nulls(quote_obj: dict[str, Any]) -> list[str]:
    """Augment `missing_fields` with any field Claude returned as null
    but didn't include in missing_fields — guards against the parser
    forgetting to flag a null field."""
    declared = set(_normalize_missing_fields(quote_obj.get("missing_fields") or []))
    for fld in ("unit_price", "unit", "min_order_qty", "delivery_days", "terms"):
        if quote_obj.get(fld) is None:
            declared.add(fld)
    return sorted(declared)


async def parse_quote_email(*, rfp_email_id: int) -> ParsedQuotesResult:
    """Parse one inbound rfp_emails row into structured Quote rows.

    Caller catches exceptions — on raise, the orchestrator marks the
    rfp_emails.parse_status='parse_failed' so the batch keeps moving (F7).
    """
    async with SessionLocal() as session:
        email_row = await session.get(RfpEmail, rfp_email_id)
        if email_row is None:
            raise LookupError(f"rfp_email {rfp_email_id} not found")
        if email_row.rfp_request_id is None:
            # Unattributed reply — no items to score against; skip parse.
            log.info("quote.parse.skipped_unattributed", rfp_email_id=rfp_email_id)
            return ParsedQuotesResult(
                rfp_email_id=rfp_email_id,
                rfp_request_id=None,
                distributor_id=None,
                quotes_inserted=0,
                off_topic=False,
                overall_parse_confidence=None,
                note="unattributed — no parse",
                unmatched_ingredient_names=[],
            )

        # Build the "asked list" — scoped to THIS distributor's specialties,
        # not the union of all rfp_request_items (which would include
        # ingredients other distributors were quoted on and cause Claude
        # to generate spurious "missing field" entries for items the
        # distributor was never asked about).
        union_rows = (
            await session.execute(
                select(Ingredient)
                .join(RfpRequestItem, RfpRequestItem.ingredient_id == Ingredient.id)
                .where(RfpRequestItem.rfp_request_id == email_row.rfp_request_id)
            )
        ).scalars().all()
        asked_rows: list[Ingredient]
        if email_row.distributor_id is not None:
            distributor = await session.get(Distributor, email_row.distributor_id)
            d_specs = {s.lower() for s in (distributor.specialties or [])} if distributor else set()
            asked_rows = [
                ing
                for ing in union_rows
                if specialty_tags_for(ing) & d_specs
            ]
            # Defensive: if scoping by tags somehow yields empty, fall
            # back to the full union — better to over-ask Claude than
            # silently lose the parse.
            if not asked_rows:
                asked_rows = list(union_rows)
        else:
            asked_rows = list(union_rows)
        asked_names = [i.name for i in asked_rows]

    user_msg = (
        "Parse the distributor reply below into structured quotes.\n\n"
        "INGREDIENTS WE ASKED THIS DISTRIBUTOR TO QUOTE:\n"
        + "\n".join(f"  - {n}" for n in asked_names)
        + "\n\nREPLY BODY:\n---\n"
        + (email_row.body or "")
        + "\n---\n\n"
        "Use the parse_quote tool. If the reply is an auto-responder / OOO / "
        "marketing message, set `off_topic=true` and return `quotes=[]`. "
        "Otherwise extract one quotes[] entry per ingredient the distributor "
        "addressed, including items they declined to quote (those will have "
        "nulls + entries in missing_fields)."
    )

    client = get_client()
    async with traced_call(STAGE) as t:
        resp = await client.messages.create(
            model=MODEL_ID,
            max_tokens=MAX_TOKENS,
            tools=[PARSE_QUOTE],
            tool_choice={"type": "tool", "name": "parse_quote"},
            messages=[{"role": "user", "content": user_msg}],
        )
        t.bind(resp)

    tool_use = next(
        (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_use is None:
        raise RuntimeError(
            f"Claude did not return a tool_use block for parse_quote "
            f"(rfp_email_id={rfp_email_id}); stop_reason={resp.stop_reason}"
        )
    payload = tool_use.input
    if isinstance(payload, str):
        payload = json.loads(payload)

    quotes_data: list[dict[str, Any]] = list(payload.get("quotes") or [])
    off_topic = bool(payload.get("off_topic", False))
    overall_conf = payload.get("overall_parse_confidence")
    note = payload.get("note")

    unmatched_names: list[str] = []
    inserted = 0

    async with SessionLocal() as session, session.begin():
        email_row = await session.get(RfpEmail, rfp_email_id)
        if email_row is None:
            raise LookupError(f"rfp_email {rfp_email_id} vanished")
        rfp_request_id = email_row.rfp_request_id
        distributor_id = email_row.distributor_id

        if not off_topic and quotes_data and rfp_request_id is not None:
            for q in quotes_data:
                ing_id, matched_exact = await _resolve_ingredient_id(
                    session=session,
                    rfp_request_id=rfp_request_id,
                    distributor_id=distributor_id,
                    quoted_name=q.get("ingredient_name", ""),
                )
                if ing_id is None:
                    unmatched_names.append(q.get("ingredient_name", ""))
                    log.info(
                        "quote.parse.unmatched_ingredient",
                        quoted=q.get("ingredient_name"),
                        rfp_email_id=rfp_email_id,
                    )
                    continue
                # Skip persisting a quote without a known distributor —
                # ingredient_id+distributor_id form the comparison key.
                if distributor_id is None:
                    log.info(
                        "quote.parse.skipped_no_distributor",
                        ingredient_id=ing_id,
                        rfp_email_id=rfp_email_id,
                    )
                    continue
                missing = _add_missing_from_nulls(q)
                quote = Quote(
                    rfp_request_id=rfp_request_id,
                    distributor_id=distributor_id,
                    ingredient_id=ing_id,
                    unit_price=q.get("unit_price"),
                    unit=q.get("unit"),
                    min_order_qty=q.get("min_order_qty"),
                    delivery_days=q.get("delivery_days"),
                    terms=q.get("terms"),
                    source_email_id=rfp_email_id,
                    parse_confidence=q.get("parse_confidence"),
                    missing_fields=missing,
                )
                session.add(quote)
                inserted += 1

        email_row.parse_status = PARSE_STATUS_PARSED

    log.info(
        "quote.parsed",
        rfp_email_id=rfp_email_id,
        quotes=inserted,
        off_topic=off_topic,
        unmatched=len(unmatched_names),
    )
    return ParsedQuotesResult(
        rfp_email_id=rfp_email_id,
        rfp_request_id=email_row.rfp_request_id,
        distributor_id=email_row.distributor_id,
        quotes_inserted=inserted,
        off_topic=off_topic,
        overall_parse_confidence=overall_conf,
        note=note,
        unmatched_ingredient_names=unmatched_names,
    )
