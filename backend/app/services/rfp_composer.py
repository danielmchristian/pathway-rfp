"""Phase 5 — compose a per-distributor RFP email via Claude.

The composer is scoped to a single distributor: it receives only the
ingredients we want THIS distributor to quote, plus context about the
restaurant and deadline. Claude returns `{subject_tail, body}`; the
orchestrator prepends `[RFP-{rfp_request_id}]` to the subject so reply
matching has a deterministic fallback signal.

LLM usage is logged under stage='rfp_compose' for /api/usage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from app.llm import MODEL_ID
from app.llm.client import get_client
from app.llm.tools import COMPOSE_RFP_EMAIL
from app.llm.usage import traced_call
from app.models.distributor import Distributor
from app.models.restaurant import Restaurant
from app.services.quantity_aggregator import IngredientVolume

log = structlog.get_logger("rfp_composer")

STAGE = "rfp_compose"
MAX_TOKENS = 1500


@dataclass
class RfpEmailContent:
    subject_tail: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        return {"subject_tail": self.subject_tail, "body": self.body}


def _format_quantity(qty: Decimal | None, unit: str | None) -> str:
    if qty is None:
        return "(quantity TBD — volume estimate unavailable)"
    # Round to two significant places for readability.
    if qty >= 100:
        rendered = f"{qty:.0f}"
    elif qty >= 10:
        rendered = f"{qty:.1f}"
    else:
        rendered = f"{qty:.2f}"
    return f"~{rendered} {unit or 'units'}/week"


def _build_ingredient_summary(ingredients: list[IngredientVolume]) -> str:
    """Plain text bullet list of ingredients + weekly volume estimates.

    Phase 5.1: prefer wholesale_quantity/wholesale_unit when present (the
    quantity_aggregator's wholesale converter sets these). Surfaces the
    conversion_note inline so distributors see the planning assumption.
    """
    lines: list[str] = []
    for v in ingredients:
        # Wholesale unit if normalized; otherwise raw per-serving aggregate.
        if v.wholesale_quantity is not None and v.wholesale_unit:
            qty = _format_quantity(v.wholesale_quantity, v.wholesale_unit)
        else:
            qty = _format_quantity(v.weekly_quantity, v.unit)
        annotations: list[str] = []
        if v.variant_count > 1:
            annotations.append(
                f"merged {v.variant_count} menu variants across "
                f"{v.dishes_used} dishes"
            )
        elif v.dishes_used > 1:
            annotations.append(f"used across {v.dishes_used} dishes")
        if v.conversion_note:
            annotations.append(v.conversion_note)
        suffix = f"  ({'; '.join(annotations)})" if annotations else ""
        lines.append(f"- {v.ingredient_name}: {qty}{suffix}")
    return "\n".join(lines)


def _build_user_message(
    *,
    restaurant: Restaurant,
    distributor: Distributor,
    ingredients: list[IngredientVolume],
    deadline: datetime,
    covers_per_dish_per_week: int,
) -> str:
    location = ", ".join(
        p for p in (restaurant.city, restaurant.state) if p
    ) or (restaurant.address or "")
    return (
        f"Compose an RFP email FROM the restaurant procurement team "
        f"TO the distributor below.\n\n"
        f"OPENING (use this exact pattern): 'I'm reaching out from the "
        f"procurement team at {restaurant.name} in {location}.'\n"
        f"DO NOT write 'My name is'. DO NOT leave a name placeholder blank. "
        f"DO NOT invent a person's name.\n\n"
        f"RESTAURANT:\n"
        f"  name: {restaurant.name}\n"
        f"  location: {location}\n"
        f"  positioning: fast-casual, fresh ingredients, daily prep\n\n"
        f"DISTRIBUTOR:\n"
        f"  name: {distributor.name}\n"
        f"  specialties: {', '.join(distributor.specialties or []) or '(unspecified)'}\n\n"
        f"INGREDIENTS TO QUOTE (the list below has ALREADY been filtered to "
        f"items this distributor can supply — every item on the list is in "
        f"their wheelhouse; do NOT hedge or apologize for any item. Estimates "
        f"are based on a planning assumption of {covers_per_dish_per_week} "
        f"covers per dish per week — distributors should quote at their "
        f"standard wholesale tiers, NOT treat these as a firm purchase order):\n"
        f"{_build_ingredient_summary(ingredients)}\n\n"
        f"The list above is COMPLETE — do not mention any ingredient outside "
        f"it, even hypothetically.\n\n"
        f"REPLY DEADLINE: {deadline.strftime('%A, %B %d, %Y')}\n"
        f"Reply by hitting Reply on this email — our procurement inbox will "
        f"route your quote automatically.\n\n"
        f"Compose the email now using the compose_rfp_email tool. Close with: "
        f"'Best regards, / Procurement Team / {restaurant.name}'."
    )


async def compose_rfp_email(
    *,
    restaurant: Restaurant,
    distributor: Distributor,
    ingredients: list[IngredientVolume],
    deadline: datetime,
    covers_per_dish_per_week: int,
) -> RfpEmailContent:
    if not ingredients:
        raise ValueError("compose_rfp_email called with empty ingredient list")

    client = get_client()
    user_msg = _build_user_message(
        restaurant=restaurant,
        distributor=distributor,
        ingredients=ingredients,
        deadline=deadline,
        covers_per_dish_per_week=covers_per_dish_per_week,
    )

    async with traced_call(STAGE) as t:
        resp = await client.messages.create(
            model=MODEL_ID,
            max_tokens=MAX_TOKENS,
            tools=[COMPOSE_RFP_EMAIL],
            tool_choice={"type": "tool", "name": "compose_rfp_email"},
            messages=[{"role": "user", "content": user_msg}],
        )
        t.bind(resp)

    tool_use = next(
        (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_use is None:
        raise RuntimeError(
            f"Claude did not return a tool_use block for compose_rfp_email "
            f"(distributor={distributor.name}); stop_reason={resp.stop_reason}"
        )
    payload = tool_use.input
    if isinstance(payload, str):
        # Defensive: SDK normally returns a dict here.
        payload = json.loads(payload)
    subject_tail = (payload.get("subject_tail") or "").strip()
    body = (payload.get("body") or "").strip()
    if not subject_tail or not body:
        raise RuntimeError(
            f"compose_rfp_email returned empty fields for distributor "
            f"{distributor.name}: subject_tail={subject_tail!r}"
        )
    log.info(
        "rfp.composed",
        distributor=distributor.name,
        ingredient_count=len(ingredients),
        body_chars=len(body),
    )
    return RfpEmailContent(subject_tail=subject_tail, body=body)
