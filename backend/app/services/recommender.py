"""Phase 6 — Recommendation engine with explicit null-safety.

Scoring weights:
    0.35 × cost_score          (per-item rank — apples-to-apples)
    0.20 × delivery_score
    0.10 × moq_fit_score
    0.15 × completeness_score
    0.20 × coverage_score      (fraction of RFP basket quoted)

**Cost is scored per-item, not by basket total.** For each ingredient
with ≥2 priced quotes across the cohort, distributors are ranked by
unit_price (cheapest=1.0, most expensive=0.0, linear). A distributor's
cost_score is the mean of its per-item ranks. A distributor that
quoted fewer items can NOT gain a cost advantage just by having a
smaller basket — they're scored only on the ingredients where direct
comparison is possible. Distributors with no comparable items get 0.5
(neutral — uncomparable, not punished).

**Coverage is a first-class weighted component** at 20% — a partial
quote shouldn't outrank a complete one unless its per-item pricing is
genuinely better. The basket_total is still computed and surfaced in
the rationale as informational context (it's what the buyer pays for
that distributor's subset), but it does NOT drive ranking.

Null-safety rules (asymmetric, intentional — documented in spec.md):

  * `unit_price`=NULL OR `wholesale_quantity`=NULL (TBD)
        → that ingredient is EXCLUDED from the informational basket
          sum, excluded from per-item cost comparison, and the basket
          is flagged `incomplete_comparison=true`.
  * `delivery_days`=NULL
        → scored at 0.0 (WORST-CASE). Reason: a distributor refusing
          to commit to delivery is a real negative signal, not absent
          data. Distinct from price NULL on purpose — price NULL means
          "they're working on it"; delivery NULL means "they won't
          commit". The rationale text says this verbatim.
  * `min_order_qty`=NULL → scored at 0.5 (neutral). Reason: unknown
        MOQ is genuinely ambiguous (some distributors don't enforce one).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.quote import Quote
from app.models.recommendation import Recommendation
from app.models.rfp import RfpRequest, RfpRequestItem
from app.services.quantity_aggregator import normalize_to_wholesale_unit

log = structlog.get_logger("recommender")

WEIGHT_COST = 0.35
WEIGHT_DELIVERY = 0.20
WEIGHT_MOQ = 0.10
WEIGHT_COMPLETENESS = 0.15
WEIGHT_COVERAGE = 0.20

# MOQ fit: <= 4 weeks of demand scored 1.0, > 12 weeks scored 0.0.
MOQ_FIT_GOOD_WEEKS = Decimal("4")
MOQ_FIT_BAD_WEEKS = Decimal("12")


@dataclass
class ComponentScore:
    name: str
    raw_value: float | None
    normalized: float
    null_imputed: bool
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class DistributorRecommendation:
    distributor_id: int
    distributor_name: str
    score: float
    coverage_pct: Decimal
    quoted_ingredient_count: int
    requested_ingredient_count: int
    incomplete_comparison: bool
    components: list[ComponentScore]
    rationale: str
    excluded_for_cost: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "distributor_id": self.distributor_id,
            "distributor_name": self.distributor_name,
            "score": self.score,
            "coverage_pct": str(self.coverage_pct),
            "quoted_ingredient_count": self.quoted_ingredient_count,
            "requested_ingredient_count": self.requested_ingredient_count,
            "incomplete_comparison": self.incomplete_comparison,
            "components": [c.to_dict() for c in self.components],
            "rationale": self.rationale,
            "excluded_for_cost": self.excluded_for_cost,
        }


@dataclass
class RecommendationResult:
    rfp_request_id: int
    ready: bool
    deadline_passed: bool
    all_replied: bool
    pick: DistributorRecommendation | None
    ranked: list[DistributorRecommendation]
    not_ready_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rfp_request_id": self.rfp_request_id,
            "ready": self.ready,
            "deadline_passed": self.deadline_passed,
            "all_replied": self.all_replied,
            "pick": self.pick.to_dict() if self.pick else None,
            "ranked": [d.to_dict() for d in self.ranked],
            "not_ready_reason": self.not_ready_reason,
        }


# ---------------------------------------------------------------------------
# Component scoring
# ---------------------------------------------------------------------------


def _wholesale_quantity_for(item: RfpRequestItem, ingredient_name: str) -> Decimal | None:
    """Re-derive the wholesale quantity used in the email body so the
    basket-cost math matches what the distributor was asked to quote on."""
    if item.quantity is None or item.unit is None:
        return None
    wq, _unit, _note = normalize_to_wholesale_unit(ingredient_name, item.quantity, item.unit)
    return wq


def _score_cost(
    quotes_by_ingredient: dict[int, Quote],
    items_by_ingredient: dict[int, RfpRequestItem],
    ingredient_names: dict[int, str],
) -> tuple[Decimal | None, list[str], bool]:
    """Returns (basket_total_or_None, excluded_ingredient_names, incomplete_flag).

    basket_total is the sum of `unit_price × wholesale_quantity` across
    ingredients where both are non-null. If ANY ingredient in this
    distributor's quoted set has a null price or null quantity, that
    ingredient is excluded AND `incomplete_flag=True`.
    """
    total = Decimal("0")
    excluded: list[str] = []
    incomplete = False
    counted = 0
    for ing_id, quote in quotes_by_ingredient.items():
        item = items_by_ingredient.get(ing_id)
        name = ingredient_names.get(ing_id, f"ingredient {ing_id}")
        if quote.unit_price is None:
            excluded.append(f"{name} (no price)")
            incomplete = True
            continue
        wq = _wholesale_quantity_for(item, name) if item else None
        if wq is None:
            excluded.append(f"{name} (TBD quantity)")
            incomplete = True
            continue
        total += Decimal(str(quote.unit_price)) * Decimal(str(wq))
        counted += 1
    if counted == 0:
        return None, excluded, True
    return total.quantize(Decimal("0.01")), excluded, incomplete


def _per_item_cost_scores(
    quotes_by_distributor: dict[int, list[Quote]],
) -> tuple[dict[int, float], dict[int, int]]:
    """Per-item unit-price ranking. Replaces basket-total comparison.

    For each ingredient with ≥2 priced quotes across the cohort, the
    distributors are ranked by unit_price (cheapest=1.0, most expensive=0.0,
    linear). Each distributor's cost_score is the mean of their per-item
    ranks across the comparable items. Distributors with no comparable
    items get 0.5 (neutral — uncomparable, not punished and not rewarded).

    The previous basket-total scoring rewarded incompleteness: a distributor
    that quoted fewer items had a smaller basket sum and looked "cheaper"
    by default. Per-item ranking is apples-to-apples — it asks "who's
    cheaper on the same goods" instead of "who quoted a smaller total."

    Returns:
        (cost_scores_by_dist_id, comparable_item_count_by_dist_id)
    """
    per_ingredient: dict[int, list[tuple[int, Decimal]]] = {}
    for dist_id, quotes in quotes_by_distributor.items():
        for q in quotes:
            if q.unit_price is None:
                continue
            per_ingredient.setdefault(q.ingredient_id, []).append(
                (dist_id, Decimal(str(q.unit_price)))
            )

    per_dist_scores: dict[int, list[float]] = {d: [] for d in quotes_by_distributor}
    for entries in per_ingredient.values():
        if len(entries) < 2:
            # Only one distributor quoted this ingredient — uncomparable,
            # skip rather than auto-award. Avoids monopoly-of-quote credit.
            continue
        prices = [p for _, p in entries]
        lo, hi = min(prices), max(prices)
        if hi == lo:
            for d, _ in entries:
                per_dist_scores[d].append(1.0)
            continue
        for d, p in entries:
            per_dist_scores[d].append(float((hi - p) / (hi - lo)))

    scores: dict[int, float] = {}
    counts: dict[int, int] = {}
    for d, sc in per_dist_scores.items():
        counts[d] = len(sc)
        scores[d] = (sum(sc) / len(sc)) if sc else 0.5
    return scores, counts


def _score_delivery(quotes: list[Quote]) -> tuple[float, ComponentScore]:
    """Asymmetric null-safety: any quote with delivery_days=NULL pulls
    the distributor's score to 0.0 — refusing to commit is a real
    negative signal (NOT absent data; we treat absent data via the
    cost-component flag instead)."""
    if not quotes:
        return 0.0, ComponentScore(
            name="delivery",
            raw_value=None,
            normalized=0.0,
            null_imputed=True,
            note="no quotes",
        )
    nulls = [q for q in quotes if q.delivery_days is None]
    if nulls:
        return 0.0, ComponentScore(
            name="delivery",
            raw_value=None,
            normalized=0.0,
            null_imputed=True,
            note=(
                "scored worst-case (0.0) because the distributor did not "
                "commit a delivery_days value on at least one quoted "
                "ingredient — refusing to commit is treated as a real "
                "negative signal (asymmetric vs price NULL)"
            ),
        )
    avg_days = sum(q.delivery_days for q in quotes) / len(quotes)
    # 1 day = 1.0; 7 days = 0.0; linear in between, clamped.
    score = max(0.0, min(1.0, 1.0 - (avg_days - 1) / 6))
    return score, ComponentScore(
        name="delivery",
        raw_value=avg_days,
        normalized=score,
        null_imputed=False,
        note=f"avg delivery {avg_days:.1f} days",
    )


def _score_moq(
    quotes_by_ingredient: dict[int, Quote],
    items_by_ingredient: dict[int, RfpRequestItem],
    ingredient_names: dict[int, str],
) -> tuple[float, ComponentScore]:
    """For each quote: weeks_of_supply = moq / weekly_quantity.
    Linear penalty from 4 weeks → 12 weeks. NULL MOQ = 0.5 (ambiguous)."""
    per_item: list[tuple[float, bool]] = []
    null_count = 0
    for ing_id, quote in quotes_by_ingredient.items():
        item = items_by_ingredient.get(ing_id)
        name = ingredient_names.get(ing_id, "")
        wq = _wholesale_quantity_for(item, name) if item else None
        if quote.min_order_qty is None:
            per_item.append((0.5, True))
            null_count += 1
            continue
        if wq is None or wq == 0:
            # No weekly quantity to compare against; can't judge fit.
            per_item.append((0.5, True))
            null_count += 1
            continue
        weeks = Decimal(str(quote.min_order_qty)) / wq
        if weeks <= MOQ_FIT_GOOD_WEEKS:
            score = 1.0
        elif weeks >= MOQ_FIT_BAD_WEEKS:
            score = 0.0
        else:
            score = float((MOQ_FIT_BAD_WEEKS - weeks) / (MOQ_FIT_BAD_WEEKS - MOQ_FIT_GOOD_WEEKS))
        per_item.append((score, False))
    if not per_item:
        return 0.0, ComponentScore(
            name="moq_fit",
            raw_value=None,
            normalized=0.0,
            null_imputed=True,
            note="no quotes",
        )
    avg = sum(s for s, _ in per_item) / len(per_item)
    note = (
        f"{null_count}/{len(per_item)} MOQs unknown — scored neutral (0.5) for those"
        if null_count
        else f"avg MOQ fit across {len(per_item)} items"
    )
    return avg, ComponentScore(
        name="moq_fit",
        raw_value=avg,
        normalized=avg,
        null_imputed=bool(null_count),
        note=note,
    )


def _score_completeness(quotes: list[Quote]) -> tuple[float, ComponentScore]:
    if not quotes:
        return 0.0, ComponentScore(
            name="completeness",
            raw_value=None,
            normalized=0.0,
            null_imputed=True,
            note="no quotes",
        )
    total_fields = 5 * len(quotes)
    missing = sum(len(q.missing_fields or []) for q in quotes)
    score = max(0.0, 1.0 - missing / total_fields)
    return score, ComponentScore(
        name="completeness",
        raw_value=missing,
        normalized=score,
        null_imputed=False,
        note=f"{missing}/{total_fields} fields missing across quotes",
    )


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


async def compute_for_rfp(rfp_request_id: int, *, force: bool = False) -> RecommendationResult:
    """Compute (or refuse) a recommendation for one RFP.

    Returns ready=False if (deadline not passed AND not all distributors
    have replied) and force=False. Otherwise computes the ranked list
    and persists the top pick into `recommendations`.
    """
    async with SessionLocal() as session:
        rfp_req = await session.get(RfpRequest, rfp_request_id)
        if rfp_req is None:
            raise LookupError(f"rfp_request {rfp_request_id} not found")

        items = (
            (
                await session.execute(
                    select(RfpRequestItem).where(RfpRequestItem.rfp_request_id == rfp_request_id)
                )
            )
            .scalars()
            .all()
        )
        items_by_ingredient: dict[int, RfpRequestItem] = {i.ingredient_id: i for i in items}
        ingredient_ids = list(items_by_ingredient.keys())
        ingredient_names: dict[int, str] = {}
        if ingredient_ids:
            for ing in (
                await session.execute(select(Ingredient).where(Ingredient.id.in_(ingredient_ids)))
            ).scalars():
                ingredient_names[ing.id] = ing.name

        all_quotes = (
            (await session.execute(select(Quote).where(Quote.rfp_request_id == rfp_request_id)))
            .scalars()
            .all()
        )

        # Group quotes by distributor.
        quotes_by_distributor: dict[int, list[Quote]] = {}
        for q in all_quotes:
            quotes_by_distributor.setdefault(q.distributor_id, []).append(q)

        # Determine the cohort of distributors we *expected* replies from
        # (outbound emails for this RFP that aren't follow-ups).
        from app.models.rfp import EmailDirection, RfpEmail

        outbound_dists = (
            (
                await session.execute(
                    select(RfpEmail.distributor_id)
                    .where(
                        RfpEmail.rfp_request_id == rfp_request_id,
                        RfpEmail.direction == EmailDirection.out,
                        RfpEmail.is_followup.is_(False),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        expected_distributor_ids = {d for d in outbound_dists if d is not None}
        replied_distributor_ids = set(quotes_by_distributor.keys())
        all_replied = (
            bool(expected_distributor_ids) and replied_distributor_ids >= expected_distributor_ids
        )

        deadline_passed = rfp_req.deadline is not None and rfp_req.deadline < datetime.now(UTC)

        if not force and not all_replied and not deadline_passed:
            return RecommendationResult(
                rfp_request_id=rfp_request_id,
                ready=False,
                deadline_passed=False,
                all_replied=False,
                pick=None,
                ranked=[],
                not_ready_reason=(
                    f"awaiting {len(expected_distributor_ids - replied_distributor_ids)} "
                    f"more distributor reply(ies) and deadline not yet passed"
                ),
            )

        distributors_by_id = {
            d.id: d
            for d in (
                await session.execute(
                    select(Distributor).where(
                        Distributor.id.in_(replied_distributor_ids | expected_distributor_ids)
                    )
                )
            ).scalars()
        }

        # First pass: per-distributor basket total (informational only —
        # surfaced in the rationale, NOT used for ranking) + null-price /
        # TBD-quantity exclusions that flag incomplete_comparison.
        baskets: dict[int, Decimal | None] = {}
        excluded_by_dist: dict[int, list[str]] = {}
        incomplete_by_dist: dict[int, bool] = {}
        for dist_id, dist_quotes in quotes_by_distributor.items():
            qbi = {q.ingredient_id: q for q in dist_quotes}
            basket, excluded, incomplete = _score_cost(qbi, items_by_ingredient, ingredient_names)
            baskets[dist_id] = basket
            excluded_by_dist[dist_id] = excluded
            incomplete_by_dist[dist_id] = incomplete

        # Apples-to-apples cost: rank distributors per-ingredient on unit
        # price, average their ranks across comparable items.
        cost_scores_by_dist, comparable_counts = _per_item_cost_scores(quotes_by_distributor)

        # Second pass: full scoring.
        ranked: list[DistributorRecommendation] = []
        requested_count = len(ingredient_ids)
        for dist_id, dist_quotes in quotes_by_distributor.items():
            d = distributors_by_id.get(dist_id)
            if d is None:
                continue
            qbi = {q.ingredient_id: q for q in dist_quotes}

            cost_norm = cost_scores_by_dist[dist_id]
            comp_n = comparable_counts[dist_id]
            cost_note = (
                f"per-item rank averaged {cost_norm:.2f} across {comp_n} ingredient(s) "
                f"where another distributor also quoted a price"
                if comp_n > 0
                else "no priced-quote overlap with other distributors — scored neutral 0.5"
            )
            if baskets[dist_id] is not None:
                cost_note += f"; informational basket cost ${baskets[dist_id]:.2f}/week"
            if excluded_by_dist[dist_id]:
                cost_note += f"; excluded from basket: {', '.join(excluded_by_dist[dist_id])}"
            cost_comp = ComponentScore(
                name="cost",
                raw_value=float(baskets[dist_id]) if baskets[dist_id] is not None else None,
                normalized=cost_norm,
                null_imputed=comp_n == 0,
                note=cost_note,
            )

            delivery_norm, delivery_comp = _score_delivery(dist_quotes)
            moq_norm, moq_comp = _score_moq(qbi, items_by_ingredient, ingredient_names)
            completeness_norm, completeness_comp = _score_completeness(dist_quotes)

            quoted_count = len(qbi)
            coverage = Decimal(str(quoted_count)) / Decimal(str(requested_count)) * Decimal("100")
            coverage_norm = float(coverage) / 100.0
            coverage_comp = ComponentScore(
                name="coverage",
                raw_value=float(coverage),
                normalized=coverage_norm,
                null_imputed=False,
                note=f"quoted {quoted_count}/{requested_count} requested items ({coverage:.1f}%)",
            )

            score = (
                WEIGHT_COST * cost_norm
                + WEIGHT_DELIVERY * delivery_norm
                + WEIGHT_MOQ * moq_norm
                + WEIGHT_COMPLETENESS * completeness_norm
                + WEIGHT_COVERAGE * coverage_norm
            )

            incomplete = incomplete_by_dist[dist_id] or quoted_count < requested_count

            rationale = (
                f"{d.name} scored {score:.2f} on a basket of {quoted_count}/{requested_count} "
                f"requested items (coverage {coverage:.0f}%). "
                + (
                    f"Per-item cost rank averaged {cost_norm:.2f} across {comp_n} "
                    f"ingredient(s) where another distributor also quoted. "
                    if comp_n > 0
                    else "No priced-quote overlap with other distributors; cost scored neutral 0.5. "
                )
                + (
                    f"Informational basket cost ${baskets[dist_id]:.2f}/week. "
                    if baskets[dist_id] is not None
                    else "No priced ingredients — basket cost unavailable. "
                )
                + delivery_comp.note
                + ". "
                + moq_comp.note
                + ". "
                + completeness_comp.note
                + ". "
                + coverage_comp.note
                + "."
            )
            if excluded_by_dist[dist_id]:
                rationale += f" Excluded from basket: {', '.join(excluded_by_dist[dist_id])}."
            if incomplete:
                rationale += (
                    " Basket flagged incomplete_comparison=true; this distributor "
                    "quoted on a subset of the RFP. Coverage is reflected in the "
                    "score (20% weight); cost is compared per-item against "
                    "distributors who quoted the same ingredients, not by basket total."
                )

            ranked.append(
                DistributorRecommendation(
                    distributor_id=dist_id,
                    distributor_name=d.name,
                    score=round(score, 4),
                    coverage_pct=coverage.quantize(Decimal("0.01")),
                    quoted_ingredient_count=quoted_count,
                    requested_ingredient_count=requested_count,
                    incomplete_comparison=incomplete,
                    components=[
                        cost_comp,
                        delivery_comp,
                        moq_comp,
                        completeness_comp,
                        coverage_comp,
                    ],
                    rationale=rationale,
                    excluded_for_cost=excluded_by_dist[dist_id],
                )
            )

        ranked.sort(key=lambda r: r.score, reverse=True)
        pick = ranked[0] if ranked else None

        # Persist the pick (if any). Idempotency: delete existing
        # recommendations for this RFP first so a re-finalize replaces
        # rather than accumulates.
        await _replace_recommendation(session, rfp_request_id, pick, ranked)
        await session.commit()

    log.info(
        "recommendation.computed",
        rfp_request_id=rfp_request_id,
        ranked_count=len(ranked),
        pick_id=pick.distributor_id if pick else None,
        force=force,
        deadline_passed=deadline_passed,
        all_replied=all_replied,
    )
    return RecommendationResult(
        rfp_request_id=rfp_request_id,
        ready=True,
        deadline_passed=deadline_passed,
        all_replied=all_replied,
        pick=pick,
        ranked=ranked,
    )


async def _replace_recommendation(
    session: AsyncSession,
    rfp_request_id: int,
    pick: DistributorRecommendation | None,
    ranked: list[DistributorRecommendation],
) -> None:
    """Idempotently persist the pick. Deletes any prior row for the RFP."""
    from sqlalchemy import delete

    await session.execute(
        delete(Recommendation).where(Recommendation.rfp_request_id == rfp_request_id)
    )
    if pick is None:
        return
    session.add(
        Recommendation(
            rfp_request_id=rfp_request_id,
            distributor_id=pick.distributor_id,
            score=pick.score,
            rationale=pick.rationale,
            incomplete_comparison=pick.incomplete_comparison,
            coverage_pct=pick.coverage_pct,
            component_breakdown={"ranked": [r.to_dict() for r in ranked]},
        )
    )
