from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class DistributorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    source: str | None = None
    specialties: list[str] = []


class ScoredDistributorOut(BaseModel):
    distributor_id: int
    name: str
    specialties: list[str]
    source: str | None
    matched_ingredient_count: int
    total_ingredients: int
    match_pct: float
    sample_matched_ingredients: list[str]
    distance_km: float | None = None


class DiscoveryResponse(BaseModel):
    seed_loaded: int
    places_found: int
    places_filtered_out: int
    duplicates_merged: int
    total_distributors: int
