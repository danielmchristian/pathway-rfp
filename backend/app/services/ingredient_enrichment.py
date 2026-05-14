"""Per-restaurant enrichment orchestrator.

Walks every ingredient referenced by the restaurant's dishes, FDC-matches
ingredients that don't yet have a usda_fdc_id, then AMS-fetches prices for
each. Fan-out is bounded by an asyncio.Semaphore. Emits substage events on
the pipeline event bus so the UI can show step-by-step progress.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.llm import compute_cost_usd
from app.models.dish import Dish
from app.models.ingredient import Ingredient
from app.models.llm_usage import LlmUsage
from app.models.restaurant import Restaurant
from app.pipeline.events import Event, get_bus, stage
from app.services.usda_ams import AmsResult, fetch_prices_for_ingredient
from app.services.usda_fdc import match_ingredient

log = structlog.get_logger("enrichment")

STAGE_NAME = "ingredient_enrich"
SUBSTAGE_FDC = "usda_match"
SUBSTAGE_AMS = "ams_fetch"
CONCURRENCY = 5


@dataclass
class EnrichResult:
    ingredients_matched: int
    ingredients_already_matched: int
    prices_inserted: int
    pricing_unavailable_count: int
    cost_usd: Decimal
    source_breakdown: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ingredients_matched": self.ingredients_matched,
            "ingredients_already_matched": self.ingredients_already_matched,
            "prices_inserted": self.prices_inserted,
            "pricing_unavailable_count": self.pricing_unavailable_count,
            "cost_usd": str(self.cost_usd),
            "source_breakdown": self.source_breakdown,
        }


async def _ingredients_for_restaurant(restaurant_id: int) -> list[Ingredient]:
    async with SessionLocal() as session:
        stmt = (
            select(Dish)
            .where(Dish.restaurant_id == restaurant_id)
            .options(selectinload(Dish.ingredients))
        )
        dishes = (await session.execute(stmt)).scalars().all()
        ingredient_ids = {di.ingredient_id for d in dishes for di in d.ingredients}
        if not ingredient_ids:
            return []
        rows = (
            (await session.execute(select(Ingredient).where(Ingredient.id.in_(ingredient_ids))))
            .scalars()
            .all()
        )
        return list(rows)


async def _usda_match_cost_since(starting_call_id: int) -> Decimal:
    async with SessionLocal() as session:
        stmt = select(LlmUsage.cost_usd).where(
            LlmUsage.id > starting_call_id, LlmUsage.stage == SUBSTAGE_FDC
        )
        return sum(
            (c or Decimal("0") for c in (await session.execute(stmt)).scalars()), Decimal("0")
        )


async def _last_llm_usage_id() -> int:
    async with SessionLocal() as session:
        row = (
            await session.execute(select(LlmUsage.id).order_by(LlmUsage.id.desc()).limit(1))
        ).first()
        return row[0] if row else 0


@stage(STAGE_NAME)
async def enrich_restaurant(*, restaurant_id: int) -> EnrichResult:
    async with SessionLocal() as session:
        if await session.get(Restaurant, restaurant_id) is None:
            raise LookupError(f"restaurant {restaurant_id} not found")

    bus = get_bus()
    ingredients = await _ingredients_for_restaurant(restaurant_id)
    log.info(
        "enrich.scope",
        restaurant_id=restaurant_id,
        ingredient_count=len(ingredients),
    )

    bus.emit(
        Event(
            restaurant_id=restaurant_id,
            stage=SUBSTAGE_FDC,
            status="start",
            payload={"total": len(ingredients)},
        )
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)
    starting_id = await _last_llm_usage_id()
    matched_count = 0
    already_matched = 0

    async with httpx.AsyncClient() as http:

        async def _fdc_one(ing: Ingredient) -> None:
            nonlocal matched_count, already_matched
            async with semaphore:
                if ing.usda_fdc_id is not None:
                    already_matched += 1
                    bus.emit(
                        Event(
                            restaurant_id=restaurant_id,
                            stage=SUBSTAGE_FDC,
                            status="progress",
                            payload={
                                "ingredient": ing.name,
                                "skipped": True,
                                "reason": "already_matched",
                            },
                        )
                    )
                    return
                match = await match_ingredient(http, ing.name)
                async with SessionLocal() as session, session.begin():
                    fresh = await session.get(Ingredient, ing.id)
                    if fresh is None:
                        return
                    if match is not None:
                        fresh.usda_fdc_id = match.fdc_id
                        if not fresh.category:
                            fresh.category = match.food_category
                        ing.usda_fdc_id = match.fdc_id
                        ing.category = match.food_category
                        matched_count += 1
                bus.emit(
                    Event(
                        restaurant_id=restaurant_id,
                        stage=SUBSTAGE_FDC,
                        status="progress",
                        payload={
                            "ingredient": ing.name,
                            "matched": match is not None,
                            "fdc_id": match.fdc_id if match else None,
                            "chosen_by": match.chosen_by if match else None,
                        },
                    )
                )

        await asyncio.gather(*(_fdc_one(i) for i in ingredients))

    bus.emit(
        Event(
            restaurant_id=restaurant_id,
            stage=SUBSTAGE_FDC,
            status="complete",
            payload={
                "matched": matched_count,
                "already_matched": already_matched,
            },
        )
    )

    bus.emit(
        Event(
            restaurant_id=restaurant_id,
            stage=SUBSTAGE_AMS,
            status="start",
            payload={"total": len(ingredients)},
        )
    )

    ams_result = AmsResult()

    async with httpx.AsyncClient() as http:

        async def _ams_one(ing: Ingredient) -> None:
            async with semaphore:
                async with SessionLocal() as session, session.begin():
                    fresh = await session.get(Ingredient, ing.id)
                    if fresh is None:
                        return
                    await fetch_prices_for_ingredient(
                        session=session,
                        ingredient=fresh,
                        client=http,
                        result=ams_result,
                    )
                bus.emit(
                    Event(
                        restaurant_id=restaurant_id,
                        stage=SUBSTAGE_AMS,
                        status="progress",
                        payload={
                            "ingredient": ing.name,
                            "running_inserted": ams_result.prices_inserted,
                            "running_unavailable": ams_result.pricing_unavailable_count,
                        },
                    )
                )

        await asyncio.gather(*(_ams_one(i) for i in ingredients))

    bus.emit(
        Event(
            restaurant_id=restaurant_id,
            stage=SUBSTAGE_AMS,
            status="complete",
            payload={
                "prices_inserted": ams_result.prices_inserted,
                "pricing_unavailable_count": ams_result.pricing_unavailable_count,
                "source_breakdown": ams_result.source_breakdown,
            },
        )
    )

    cost_usd = await _usda_match_cost_since(starting_id)
    # If usage didn't insert (e.g. all matches were score-confident),
    # cost stays at zero. Compute defensively if pricing rates change.
    if cost_usd is None:
        cost_usd = compute_cost_usd(model="claude-sonnet-4-6", input_tokens=0, output_tokens=0)

    return EnrichResult(
        ingredients_matched=matched_count,
        ingredients_already_matched=already_matched,
        prices_inserted=ams_result.prices_inserted,
        pricing_unavailable_count=ams_result.pricing_unavailable_count,
        cost_usd=cost_usd,
        source_breakdown=ams_result.source_breakdown,
    )
