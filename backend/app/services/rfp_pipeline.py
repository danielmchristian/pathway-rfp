"""Phase 5 — RFP send orchestrator.

End-to-end flow for `POST /api/restaurants/{id}/send_rfps`:

  1. Load restaurant + ingredients + distributors.
  2. Score distributors with Phase 4 matcher; pick top N where
     matched_ingredient_count >= min_matches (defaults: 5, 2).
  3. Aggregate weekly volumes per ingredient (covers/dish/week multiplier),
     then dedupe wording-variants under a canonical root.
  4. Create one rfp_request + rfp_request_items rows (union of in-scope
     ingredients with merged weekly quantities).
  5. Per distributor (sequential — keeps SSE order, avoids rate limits):
     - emit rfp_compose:start  →  Claude compose
     - emit rfp_compose:complete
     - emit rfp_send:start  →  Resend POST
     - emit rfp_send:complete  →  persist rfp_emails row
  6. Mark rfp_request status: 'sent' if all ok, 'partial' if any failed.

One distributor's failure (Claude exception, Resend 5xx, etc.) does NOT
abort the batch — the failure is logged as a failed rfp_emails row and
the loop continues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models.dish import Dish
from app.models.dish_ingredient import DishIngredient
from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.restaurant import Restaurant
from app.models.rfp import EmailStatus, RfpRequest, RfpRequestItem, RfpRequestStatus
from app.pipeline.events import Event, get_bus, stage
from app.services.distributor_matching import (
    is_composite_name,
    score_distributors,
    specialty_tags_for,
)
from app.services.email_sender import SendResult, send_rfp_email
from app.services.quantity_aggregator import (
    IngredientVolume,
    aggregate_weekly_volumes,
    apply_wholesale_conversion,
    collapse_for_distributor,
)
from app.services.rfp_composer import compose_rfp_email

log = structlog.get_logger("rfp_pipeline")

STAGE = "rfp_send"
SUBSTAGE_COMPOSE = "rfp_compose"
SUBSTAGE_RESEND = "rfp_resend"


@dataclass
class DistributorOutcome:
    distributor_id: int
    distributor_name: str
    matched_ingredient_count: int
    ingredients_emailed: int
    status: str
    message_id: str | None
    resend_id: str | None
    recipient_actual: str | None
    recipient_nominal: str | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class RfpSendResult:
    rfp_request_id: int
    deadline: str
    distributors_targeted: int
    emails_sent: int
    emails_failed: int
    items_count: int
    # Phase 5.1 — ingredients with no specialty match across the selected
    # distributors (composites + items with no matching tag). Honest
    # surfacing of the gap; do not silently drop on a weak match.
    unassigned_ingredients: list[str] = field(default_factory=list)
    breakdown: list[DistributorOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rfp_request_id": self.rfp_request_id,
            "deadline": self.deadline,
            "distributors_targeted": self.distributors_targeted,
            "emails_sent": self.emails_sent,
            "emails_failed": self.emails_failed,
            "items_count": self.items_count,
            "unassigned_ingredients": self.unassigned_ingredients,
            "breakdown": [b.to_dict() for b in self.breakdown],
        }


def _scope_ingredients_for_distributor(
    *,
    distributor: Distributor,
    volumes: list[IngredientVolume],
    ingredient_index: dict[int, Ingredient],
) -> list[IngredientVolume]:
    """Return the subset of weekly-volume rows whose ingredient tags match
    at least one of this distributor's specialties."""
    d_specs = {s.lower() for s in (distributor.specialties or [])}
    if not d_specs:
        return []
    out: list[IngredientVolume] = []
    for v in volumes:
        ing = ingredient_index.get(v.ingredient_id)
        if ing is None:
            continue
        if specialty_tags_for(ing) & d_specs:
            out.append(v)
    return out


@stage(STAGE)
async def send_rfps(
    *,
    restaurant_id: int,
    distributor_limit: int = 5,
    min_matches: int = 2,
    deadline_days: int = 5,
) -> RfpSendResult:
    bus = get_bus()
    deadline = datetime.now(UTC) + timedelta(days=deadline_days)

    # ---- 1. Load (one read session) -----------------------------------
    async with SessionLocal() as session:
        restaurant = await session.get(Restaurant, restaurant_id)
        if restaurant is None:
            raise LookupError(f"restaurant {restaurant_id} not found")

        distributors = (await session.execute(select(Distributor))).scalars().all()

        ingredient_rows = (
            (
                await session.execute(
                    select(Ingredient)
                    .join(
                        DishIngredient,
                        DishIngredient.ingredient_id == Ingredient.id,
                    )
                    .join(Dish, Dish.id == DishIngredient.dish_id)
                    .where(Dish.restaurant_id == restaurant_id)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        ingredient_index: dict[int, Ingredient] = {i.id: i for i in ingredient_rows}

        volumes = await aggregate_weekly_volumes(
            session=session,
            restaurant_id=restaurant_id,
            covers_per_dish_per_week=settings.covers_per_dish_per_week,
        )

    if not distributors:
        raise LookupError(
            f"no distributors found — run discover_distributors first "
            f"(restaurant {restaurant_id})"
        )
    if not ingredient_rows:
        raise LookupError(
            f"no ingredients found — run parse_menu first (restaurant {restaurant_id})"
        )

    # ---- 2. Score + pick top N (Phase 5.1: re-threshold AFTER tightened
    #         scoping so leak-driven matches don't pass min_matches) ----
    scored = score_distributors(
        ingredients=ingredient_rows,
        distributors=distributors,
        restaurant=restaurant,
    )
    # First pass: drop distributors whose actual scoped count (under the
    # tightened matcher) falls below min_matches. Build the actual scope
    # map for every candidate so we don't double-compute later.
    candidate_distributors = [
        next(d for d in distributors if d.id == s.distributor_id) for s in scored
    ]
    scope_by_distributor: dict[int, list[IngredientVolume]] = {}
    for d in candidate_distributors:
        scope_by_distributor[d.id] = _scope_ingredients_for_distributor(
            distributor=d, volumes=volumes, ingredient_index=ingredient_index
        )
    selected_scored = []
    selected_distributors: list[Distributor] = []
    for s in scored:
        if len(scope_by_distributor[s.distributor_id]) < min_matches:
            continue
        selected_scored.append(s)
        selected_distributors.append(
            next(d for d in candidate_distributors if d.id == s.distributor_id)
        )
        if len(selected_distributors) >= distributor_limit:
            break

    if not selected_distributors:
        raise LookupError(
            f"no distributors with >= {min_matches} matched ingredients "
            f"after composite/word-boundary filtering"
        )
    log.info(
        "rfp.distributors_selected",
        count=len(selected_distributors),
        names=[d.name for d in selected_distributors],
    )

    # ---- 4. Determine union of in-scope ingredients (dedupe-aware) ----
    union_volumes_set: dict[str, IngredientVolume] = {}
    claimed_ingredient_ids: set[int] = set()
    for d in selected_distributors:
        scoped = scope_by_distributor[d.id]
        for v in scoped:
            union_volumes_set.setdefault(v.root, v)
            claimed_ingredient_ids.add(v.ingredient_id)
    union_volumes = list(union_volumes_set.values())

    # ---- 4b. Compute unassigned_ingredients --------------------------
    # Honest gap surfacing: every ingredient not claimed by any selected
    # distributor. Composites are included so the demo writeup can show
    # "X items were unassigned because they're in-house preparations".
    unassigned: list[str] = []
    for ing in ingredient_rows:
        if ing.id in claimed_ingredient_ids:
            continue
        # Skip if this ingredient simply has no per-week volume (likely
        # parsed without a quantity) — surfacing those is misleading.
        vol_match = next((v for v in volumes if v.ingredient_id == ing.id), None)
        if vol_match is None:
            continue
        unassigned.append(ing.name)
    # Sort + dedupe by display name for stable output.
    unassigned = sorted(set(unassigned))
    log.info("rfp.unassigned_count", n=len(unassigned))

    # ---- 5. Create rfp_request + items in a short tx -------------------
    async with SessionLocal() as session, session.begin():
        req = RfpRequest(
            restaurant_id=restaurant_id,
            status=RfpRequestStatus.draft,
            deadline=deadline,
        )
        session.add(req)
        await session.flush()
        rfp_request_id = req.id
        for v in union_volumes:
            session.add(
                RfpRequestItem(
                    rfp_request_id=rfp_request_id,
                    ingredient_id=v.ingredient_id,
                    quantity=v.weekly_quantity,
                    unit=v.unit,
                )
            )

    # ---- 6. Per-distributor compose + send ----------------------------
    outcomes: list[DistributorOutcome] = []
    sent_count = 0
    failed_count = 0

    async with httpx.AsyncClient(timeout=30.0) as http:
        for d, s in zip(selected_distributors, selected_scored, strict=True):
            scoped = scope_by_distributor[d.id]
            scoped = collapse_for_distributor(scoped)
            # Phase 5.1: convert per-serving units to wholesale units AFTER
            # collapse (collapse rebuilds IngredientVolume objects, so we'd
            # lose the wholesale fields if we converted earlier).
            apply_wholesale_conversion(scoped)
            if not scoped:
                # Shouldn't happen — score filter requires matches — but defend.
                outcomes.append(
                    DistributorOutcome(
                        distributor_id=d.id,
                        distributor_name=d.name,
                        matched_ingredient_count=s.matched_ingredient_count,
                        ingredients_emailed=0,
                        status="skipped",
                        message_id=None,
                        resend_id=None,
                        recipient_actual=None,
                        recipient_nominal=d.email,
                        error="no scoped ingredients after dedupe",
                    )
                )
                continue

            # 5a. compose
            bus.emit(
                Event(
                    restaurant_id=restaurant_id,
                    stage=SUBSTAGE_COMPOSE,
                    status="start",
                    payload={
                        "distributor": d.name,
                        "ingredient_count": len(scoped),
                    },
                )
            )
            try:
                content = await compose_rfp_email(
                    restaurant=restaurant,
                    distributor=d,
                    ingredients=scoped,
                    deadline=deadline,
                    covers_per_dish_per_week=settings.covers_per_dish_per_week,
                )
            except Exception as exc:
                log.exception("rfp.compose.failed", distributor=d.name)
                bus.emit(
                    Event(
                        restaurant_id=restaurant_id,
                        stage=SUBSTAGE_COMPOSE,
                        status="error",
                        payload={"distributor": d.name, "error": str(exc)},
                    )
                )
                outcomes.append(
                    DistributorOutcome(
                        distributor_id=d.id,
                        distributor_name=d.name,
                        matched_ingredient_count=s.matched_ingredient_count,
                        ingredients_emailed=len(scoped),
                        status="compose_failed",
                        message_id=None,
                        resend_id=None,
                        recipient_actual=None,
                        recipient_nominal=d.email,
                        error=f"compose: {exc}",
                    )
                )
                failed_count += 1
                continue
            bus.emit(
                Event(
                    restaurant_id=restaurant_id,
                    stage=SUBSTAGE_COMPOSE,
                    status="complete",
                    payload={
                        "distributor": d.name,
                        "subject_tail": content.subject_tail,
                        "body_chars": len(content.body),
                    },
                )
            )

            subject = f"[RFP-{rfp_request_id}] {content.subject_tail}"

            # 5b. send
            bus.emit(
                Event(
                    restaurant_id=restaurant_id,
                    stage=SUBSTAGE_RESEND,
                    status="start",
                    payload={"distributor": d.name},
                )
            )
            send: SendResult = await send_rfp_email(
                rfp_request_id=rfp_request_id,
                distributor=d,
                content=content,
                subject=subject,
                http=http,
            )

            # Persist the rfp_email row regardless of success/failure.
            async with SessionLocal() as session, session.begin():
                session.add(send.rfp_email)

            bus.emit(
                Event(
                    restaurant_id=restaurant_id,
                    stage=SUBSTAGE_RESEND,
                    status="complete",
                    payload={
                        "distributor": d.name,
                        "ok": send.ok,
                        "message_id": send.rfp_email.message_id,
                        "resend_id": send.rfp_email.resend_id,
                        "recipient_actual": send.rfp_email.recipient_actual,
                        "error": send.error,
                    },
                )
            )

            if send.ok:
                sent_count += 1
            else:
                failed_count += 1
            outcomes.append(
                DistributorOutcome(
                    distributor_id=d.id,
                    distributor_name=d.name,
                    matched_ingredient_count=s.matched_ingredient_count,
                    ingredients_emailed=len(scoped),
                    status=send.rfp_email.status.value
                    if isinstance(send.rfp_email.status, EmailStatus)
                    else str(send.rfp_email.status),
                    message_id=send.rfp_email.message_id,
                    resend_id=send.rfp_email.resend_id,
                    recipient_actual=send.rfp_email.recipient_actual,
                    recipient_nominal=send.rfp_email.recipient_nominal,
                    error=send.error,
                )
            )

    # ---- 7. Final rfp_request status ----------------------------------
    final_status = (
        RfpRequestStatus.sent
        if failed_count == 0
        else (RfpRequestStatus.partial if sent_count > 0 else RfpRequestStatus.draft)
    )
    async with SessionLocal() as session, session.begin():
        req = await session.get(RfpRequest, rfp_request_id)
        if req is not None:
            req.status = final_status

    return RfpSendResult(
        rfp_request_id=rfp_request_id,
        deadline=deadline.isoformat(),
        distributors_targeted=len(selected_distributors),
        emails_sent=sent_count,
        emails_failed=failed_count,
        items_count=len(union_volumes),
        unassigned_ingredients=unassigned,
        breakdown=outcomes,
    )


# Suppress unused-import lint (kept for clarity that we use Decimal in
# transitive types — Mapped[Decimal] columns on RfpRequestItem etc.).
_ = Decimal
_ = is_composite_name
