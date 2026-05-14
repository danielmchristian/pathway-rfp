"""USDA AMS Market News pricing.

Flow per ingredient:
  1. Map ingredient.normalized_name → AmsCommodity (or None).
  2. If no commodity match → insert one `pricing_unavailable=true` sentinel row.
  3. If matched: try to fetch live prices from MARS API
     (`/services/v1.2/reports/{slug_id}/Report Details?lastReports=10`).
     If the call succeeds, persist each observation with source='ams_market_news'.
     If it fails (or USDA_AMS_API_KEY is missing), fall back to data/seed_ams_prices.json
     and persist with source='ams_seed_fallback' for honest provenance.
  4. Dedup on (ingredient_id, observed_at, ams_commodity_code, package) so re-runs
     are cheap.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ingredient import Ingredient
from app.models.ingredient_price import IngredientPrice
from app.services.commodity_map import (
    MARKET_LOCATION,
    AmsCommodity,
    lookup_commodity,
)
from app.utils.http_retry import request_with_retry

log = structlog.get_logger("usda_ams")

STAGE_NAME = "ams_fetch"
MARS_BASE = "https://marsapi.ams.usda.gov/services/v1.2"

SOURCE_LIVE = "ams_market_news"
SOURCE_SEED = "ams_seed_fallback"
SOURCE_NO_MATCH = "ams_no_match"

SEED_PATH = Path(__file__).resolve().parents[3] / "data" / "seed_ams_prices.json"


# ---------- live fetch ---------------------------------------------------


async def _fetch_live_report(
    client: httpx.AsyncClient, report_id: int
) -> list[dict[str, Any]] | None:
    if not settings.usda_ams_api_key:
        return None
    url = f"{MARS_BASE}/reports/{report_id}/Report Details"
    response = await request_with_retry(
        client,
        "GET",
        url,
        params={"lastReports": 30},  # ~30 business days for trend math
        auth=(settings.usda_ams_api_key, ""),
        timeout=15.0,
        label=f"ams.reports[{report_id}]",
    )
    if response is None or response.status_code >= 400:
        log.warning(
            "ams.live.failed",
            report_id=report_id,
            status=response.status_code if response else None,
        )
        return None
    payload = response.json()
    if isinstance(payload, dict) and "results" in payload:
        return payload["results"]
    if isinstance(payload, list):
        return payload
    log.warning("ams.live.unexpected_shape", report_id=report_id)
    return None


def _filter_to_commodity(rows: list[dict[str, Any]], slug: str) -> list[dict[str, Any]]:
    needle = slug.lower()
    return [r for r in rows if needle in str(r.get("commodity", "")).lower()]


# Package parsing: extract (units_per_package, unit_label) from the
# `package` + `item_size` fields. We handle the common shapes we see in the
# Atlanta terminal feed. Anything we can't parse falls through to None so
# the row is still stored with raw price, just without a normalized per-unit.
_LB_RE = re.compile(r"(\d+(?:\.\d+)?)\s*lb\b", re.IGNORECASE)
_COUNT_RE = re.compile(r"(\d+)\s*(?:count|ct)\b", re.IGNORECASE)
_ITEM_SIZE_RE = re.compile(r"(\d+)\s*s?\b")


def _normalize_package(package: str, item_size: str | None) -> tuple[float, str] | None:
    """Return (units_per_package, unit_label) for common AMS package descriptors."""
    p = (package or "").lower().strip()
    if not p:
        return None
    if m := _LB_RE.search(p):
        return float(m.group(1)), "lb"
    if "1 1/9 bushel" in p:
        return 30.0, "lb"  # standard conversion for produce bushels
    if m := _COUNT_RE.search(p):
        return float(m.group(1)), "ea"
    if "bunched" in p and item_size and (m := _ITEM_SIZE_RE.match(item_size.strip())):
        return float(m.group(1)), "bunch"
    if "sack" in p:
        # "50 lb sacks" is caught above; bare "sacks" without weight → unknown.
        return None
    return None


def _parse_live_row(row: dict[str, Any], commodity: AmsCommodity) -> dict[str, Any] | None:
    """Normalize a live MARS row into our internal observation shape.

    Live AMS rows ship dates as `MM/DD/YYYY`. We compute price_per_unit_*
    from package + item_size so trends work the same way as for seed data.
    """
    raw_date = row.get("report_date") or row.get("reportBeginDate") or row.get("date")
    if not raw_date:
        return None
    raw_date_str = str(raw_date).split(" ")[0]  # strip time if present
    observed_at: datetime | None = None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            observed_at = datetime.strptime(raw_date_str, fmt).replace(tzinfo=UTC)
            break
        except ValueError:
            continue
    if observed_at is None:
        return None

    low = row.get("low_price") or row.get("lowPrice")
    high = row.get("high_price") or row.get("highPrice")
    if low is None and high is None:
        return None

    package = row.get("package") or ""
    item_size = row.get("item_size") or row.get("itemSize")
    normalized = _normalize_package(package, item_size)
    price_per_unit_low = price_per_unit_high = None
    unit_normalized = None
    if normalized is not None:
        per_pkg, unit_label = normalized
        unit_normalized = unit_label
        if per_pkg > 0:
            if low is not None:
                price_per_unit_low = f"{float(low) / per_pkg:.4f}"
            if high is not None:
                price_per_unit_high = f"{float(high) / per_pkg:.4f}"

    return {
        "commodity_slug": commodity.slug,
        "report_date": observed_at.isoformat(),
        "package": package,
        "low_price": str(low) if low is not None else None,
        "high_price": str(high) if high is not None else None,
        "unit_normalized": unit_normalized,
        "price_per_unit_low": price_per_unit_low,
        "price_per_unit_high": price_per_unit_high,
        "_raw": row,
    }


# ---------- seed fallback ------------------------------------------------

_seed_cache: dict[str, list[dict[str, Any]]] | None = None


def _load_seed() -> dict[str, list[dict[str, Any]]]:
    global _seed_cache
    if _seed_cache is not None:
        return _seed_cache
    with SEED_PATH.open() as f:
        payload = json.load(f)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for obs in payload.get("observations", []):
        grouped.setdefault(obs["commodity_slug"], []).append(obs)
    _seed_cache = grouped
    return grouped


def _seed_observations(commodity: AmsCommodity) -> list[dict[str, Any]]:
    return _load_seed().get(commodity.slug, [])


# ---------- persistence --------------------------------------------------


async def _row_already_exists(
    session: AsyncSession,
    ingredient_id: int,
    observed_at: datetime,
    ams_slug: str,
    package: str,
) -> bool:
    stmt = select(IngredientPrice.id).where(
        IngredientPrice.ingredient_id == ingredient_id,
        IngredientPrice.observed_at == observed_at,
        IngredientPrice.ams_commodity_code == ams_slug,
        (IngredientPrice.raw_payload["package"].astext == package),
    )
    return (await session.execute(stmt)).first() is not None


def _midpoint(low: str | None, high: str | None) -> Decimal | None:
    if low is None and high is None:
        return None
    lo = Decimal(low) if low is not None else None
    hi = Decimal(high) if high is not None else None
    if lo is not None and hi is not None:
        return (lo + hi) / 2
    return lo or hi


async def _persist_observations(
    session: AsyncSession,
    ingredient: Ingredient,
    commodity: AmsCommodity,
    observations: list[dict[str, Any]],
    source: str,
) -> int:
    inserted = 0
    for obs in observations:
        observed_at = datetime.fromisoformat(obs["report_date"])
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        package = obs.get("package", "")
        if await _row_already_exists(session, ingredient.id, observed_at, commodity.slug, package):
            continue
        price = _midpoint(obs.get("low_price"), obs.get("high_price"))
        price_per_unit = _midpoint(obs.get("price_per_unit_low"), obs.get("price_per_unit_high"))
        session.add(
            IngredientPrice(
                ingredient_id=ingredient.id,
                usda_fdc_id=ingredient.usda_fdc_id,
                price=price,
                unit=package or None,
                source=source,
                observed_at=observed_at,
                raw_payload=obs.get("_raw") or obs,
                pricing_unavailable=False,
                ams_commodity_code=commodity.slug,
                market_location=MARKET_LOCATION,
                price_per_unit=price_per_unit,
                unit_normalized=obs.get("unit_normalized"),
            )
        )
        inserted += 1
    return inserted


async def _persist_unavailable(session: AsyncSession, ingredient: Ingredient, reason: str) -> None:
    # Idempotent: only one sentinel row per ingredient.
    existing = (
        await session.execute(
            select(IngredientPrice.id).where(
                IngredientPrice.ingredient_id == ingredient.id,
                IngredientPrice.pricing_unavailable.is_(True),
            )
        )
    ).first()
    if existing is not None:
        return
    session.add(
        IngredientPrice(
            ingredient_id=ingredient.id,
            usda_fdc_id=ingredient.usda_fdc_id,
            source=SOURCE_NO_MATCH,
            pricing_unavailable=True,
            raw_payload={"reason": reason, "ingredient_name": ingredient.name},
        )
    )


# ---------- public entrypoint --------------------------------------------


class AmsResult:
    __slots__ = ("prices_inserted", "pricing_unavailable_count", "source_breakdown")

    def __init__(self) -> None:
        self.prices_inserted: int = 0
        self.pricing_unavailable_count: int = 0
        self.source_breakdown: dict[str, int] = {SOURCE_LIVE: 0, SOURCE_SEED: 0}


async def fetch_prices_for_ingredient(
    *,
    session: AsyncSession,
    ingredient: Ingredient,
    client: httpx.AsyncClient,
    result: AmsResult,
) -> None:
    commodity = lookup_commodity(ingredient.normalized_name)
    if commodity is None:
        await _persist_unavailable(session, ingredient, "no commodity match in map")
        result.pricing_unavailable_count += 1
        return

    # Try live first if a key is configured.
    live_rows = (
        await _fetch_live_report(client, commodity.report_id) if settings.usda_ams_api_key else None
    )
    if live_rows is not None:
        filtered = _filter_to_commodity(live_rows, commodity.slug)
        observations = [obs for r in filtered if (obs := _parse_live_row(r, commodity)) is not None]
        if observations:
            inserted = await _persist_observations(
                session, ingredient, commodity, observations, SOURCE_LIVE
            )
            result.prices_inserted += inserted
            result.source_breakdown[SOURCE_LIVE] += inserted
            return
        log.info("ams.live.empty_for_commodity", commodity=commodity.slug)

    # Fallback: seed.
    seed = _seed_observations(commodity)
    if not seed:
        await _persist_unavailable(
            session, ingredient, f"no seed data for commodity {commodity.slug}"
        )
        result.pricing_unavailable_count += 1
        return
    inserted = await _persist_observations(session, ingredient, commodity, seed, SOURCE_SEED)
    result.prices_inserted += inserted
    result.source_breakdown[SOURCE_SEED] += inserted
