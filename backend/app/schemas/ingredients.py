from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


class PriceObservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    observed_at: datetime | None = None
    price: Decimal | None = None
    price_per_unit: Decimal | None = None
    unit: str | None = None
    unit_normalized: str | None = None
    market_location: str | None = None
    ams_commodity_code: str | None = None
    source: str | None = None
    pricing_unavailable: bool = False


class PriceTrendOut(BaseModel):
    latest_price: Decimal | None = None
    avg_30d: Decimal | None = None
    delta_pct_30d: Decimal | None = None
    direction: Literal["up", "down", "flat", "unknown"] = "unknown"
    observations_count: int = 0


class IngredientPricesOut(BaseModel):
    ingredient_id: int
    ingredient_name: str
    normalized_name: str
    usda_fdc_id: int | None = None
    category: str | None = None
    trend: PriceTrendOut
    observations: list[PriceObservationOut]


class IngredientSummaryRow(BaseModel):
    ingredient_id: int
    ingredient_name: str
    normalized_name: str
    fdc_id: int | None = None
    fdc_category: str | None = None
    latest_price_per_unit: Decimal | None = None
    unit_normalized: str | None = None
    delta_pct_30d: Decimal | None = None
    direction: Literal["up", "down", "flat", "unknown"] = "unknown"
    observations_count: int = 0
    pricing_unavailable: bool = False
    source: str | None = None


class EnrichResponse(BaseModel):
    ingredients_matched: int
    ingredients_already_matched: int
    prices_inserted: int
    pricing_unavailable_count: int
    cost_usd: Decimal
    source_breakdown: dict[str, int]
