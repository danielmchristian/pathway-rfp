from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.services.pricing_trends import PriceObservation, compute_trend


def _now() -> datetime:
    return datetime(2026, 5, 14, tzinfo=UTC)


def _obs(days_ago: int, price: str) -> PriceObservation:
    return PriceObservation(
        observed_at=_now() - timedelta(days=days_ago),
        price_per_unit=Decimal(price),
        unit_normalized="lb",
    )


def test_trend_up_when_above_threshold() -> None:
    rows = [_obs(28, "1.00"), _obs(14, "1.06"), _obs(2, "1.10")]
    trend = compute_trend(rows, now=_now())
    assert trend.direction == "up"
    assert trend.latest_price == Decimal("1.10")
    assert trend.observations_count == 3
    assert trend.delta_pct_30d > Decimal("3")


def test_trend_down_when_below_negative_threshold() -> None:
    rows = [_obs(28, "1.00"), _obs(14, "0.96"), _obs(2, "0.90")]
    trend = compute_trend(rows, now=_now())
    assert trend.direction == "down"
    assert trend.delta_pct_30d < Decimal("-3")


def test_trend_flat_within_threshold() -> None:
    rows = [_obs(28, "1.00"), _obs(2, "1.02")]
    trend = compute_trend(rows, now=_now())
    assert trend.direction == "flat"
    assert -Decimal("3") <= trend.delta_pct_30d <= Decimal("3")


def test_trend_unknown_with_single_observation() -> None:
    rows = [_obs(2, "1.00")]
    trend = compute_trend(rows, now=_now())
    assert trend.direction == "unknown"
    assert trend.observations_count == 1
    assert trend.latest_price == Decimal("1.00")
    assert trend.delta_pct_30d is None


def test_trend_unknown_with_no_recent_observations() -> None:
    rows = [_obs(60, "1.00"), _obs(45, "1.10")]
    trend = compute_trend(rows, now=_now())
    assert trend.direction == "unknown"
    assert trend.observations_count == 0
    assert trend.latest_price is None
