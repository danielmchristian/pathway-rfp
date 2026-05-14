"""Pure-compute trend math over a list of price observations.

Trends are NOT persisted — computed on read so they always reflect the latest
ingredient_prices rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

Direction = Literal["up", "down", "flat", "unknown"]

# ±3% over 30 days is the line between "up/down" and "flat". Below 3% we treat
# fluctuation as noise rather than a meaningful trend signal.
DIRECTION_THRESHOLD_PCT = Decimal("3.0")
TREND_WINDOW_DAYS = 30


@dataclass
class PriceObservation:
    observed_at: datetime
    price_per_unit: Decimal | None
    unit_normalized: str | None


@dataclass
class PriceTrend:
    latest_price: Decimal | None
    avg_30d: Decimal | None
    delta_pct_30d: Decimal | None
    direction: Direction
    observations_count: int


def _now_utc() -> datetime:
    return datetime.now(UTC)


def compute_trend(
    observations: list[PriceObservation], *, now: datetime | None = None
) -> PriceTrend:
    """Compute trend metrics over price_per_unit observations.

    Filters to the last 30 days. Direction is `unknown` when fewer than 2
    observations remain after filtering; `up`/`down` requires |delta| > 3%.
    """
    now = now or _now_utc()
    cutoff = now - timedelta(days=TREND_WINDOW_DAYS)
    window = [o for o in observations if o.price_per_unit is not None and o.observed_at >= cutoff]
    window.sort(key=lambda o: o.observed_at)

    if not window:
        return PriceTrend(
            latest_price=None,
            avg_30d=None,
            delta_pct_30d=None,
            direction="unknown",
            observations_count=0,
        )

    latest = window[-1].price_per_unit
    total = sum((o.price_per_unit for o in window), Decimal("0"))
    avg = total / Decimal(len(window))

    if len(window) < 2:
        return PriceTrend(
            latest_price=latest,
            avg_30d=avg,
            delta_pct_30d=None,
            direction="unknown",
            observations_count=len(window),
        )

    first = window[0].price_per_unit
    if first == 0:
        delta_pct = None
        direction: Direction = "unknown"
    else:
        delta_pct = ((latest - first) / first) * Decimal("100")
        if delta_pct > DIRECTION_THRESHOLD_PCT:
            direction = "up"
        elif delta_pct < -DIRECTION_THRESHOLD_PCT:
            direction = "down"
        else:
            direction = "flat"

    return PriceTrend(
        latest_price=latest,
        avg_30d=avg,
        delta_pct_30d=delta_pct,
        direction=direction,
        observations_count=len(window),
    )
