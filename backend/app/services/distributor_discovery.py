"""Phase 4 — distributor discovery orchestrator.

Seed file is the primary source of distributors. Google Places is optional
enrichment, gated on `GOOGLE_PLACES_API_KEY`. Places results pass through a
Claude-driven noise filter (retail chains, warehouse clubs, etc.) before
being merged with seed rows.

Merge policy (Phase 4 amendment 4):
  - seed wins authoritatively on: name, address, latitude, longitude
  - Places wins on: phone, email, website (whenever Places has a value)
  - specialties: union of both
  - source: 'seed' → 'google_places_merged' after a merge
  - brand-new Places records (no seed match): source='google_places'
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models.distributor import Distributor
from app.models.restaurant import Restaurant
from app.pipeline.events import Event, get_bus, stage
from app.services.google_places import (
    PlaceCandidate,
    filter_noise_with_claude,
    search_distributor_candidates,
)

log = structlog.get_logger("distributor_discovery")

STAGE_NAME = "distributor_discovery"
SUBSTAGE_SEED = "seed_load"
SUBSTAGE_PLACES = "places_query"

SOURCE_SEED = "seed"
SOURCE_PLACES = "google_places"
SOURCE_MERGED = "google_places_merged"

SEED_PATH = Path(__file__).resolve().parents[3] / "data" / "distributors_seed.json"

_NAME_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class DiscoveryResult:
    seed_loaded: int
    places_found: int
    places_filtered_out: int
    duplicates_merged: int
    total_distributors: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed_loaded": self.seed_loaded,
            "places_found": self.places_found,
            "places_filtered_out": self.places_filtered_out,
            "duplicates_merged": self.duplicates_merged,
            "total_distributors": self.total_distributors,
        }


def _normalize_name(name: str) -> str:
    return _NAME_NORMALIZE_RE.sub("", (name or "").lower())


# ---------- seed -----------------------------------------------------------


def _load_seed_records() -> list[dict[str, Any]]:
    with SEED_PATH.open() as f:
        payload = json.load(f)
    return list(payload.get("distributors") or [])


async def _upsert_seed(restaurant_id: int) -> int:
    """Insert-or-update each seed row. Idempotent."""
    bus = get_bus()
    records = _load_seed_records()
    bus.emit(
        Event(
            restaurant_id=restaurant_id,
            stage=SUBSTAGE_SEED,
            status="start",
            payload={"count": len(records)},
        )
    )

    async with SessionLocal() as session, session.begin():
        existing = (await session.execute(select(Distributor))).scalars().all()
        by_name: dict[str, Distributor] = {_normalize_name(d.name): d for d in existing}

        for rec in records:
            norm = _normalize_name(rec["name"])
            specialties = list(rec.get("specialties") or [])
            row = by_name.get(norm)
            if row is None:
                session.add(
                    Distributor(
                        name=rec["name"],
                        address=rec.get("address"),
                        phone=rec.get("phone"),
                        email=rec.get("email"),
                        website=rec.get("website"),
                        latitude=(
                            Decimal(str(rec["latitude"]))
                            if rec.get("latitude") is not None
                            else None
                        ),
                        longitude=(
                            Decimal(str(rec["longitude"]))
                            if rec.get("longitude") is not None
                            else None
                        ),
                        source=SOURCE_SEED,
                        specialties=specialties,
                    )
                )
                continue
            # Update specialties (union), backfill nulls, but never overwrite
            # seed-authoritative fields on a re-run.
            existing_specs = set(row.specialties or [])
            new_specs = existing_specs | set(specialties)
            row.specialties = sorted(new_specs) if new_specs != existing_specs else row.specialties
            if not row.phone:
                row.phone = rec.get("phone")
            if not row.email:
                row.email = rec.get("email")
            if not row.website:
                row.website = rec.get("website")
            if not row.address:
                row.address = rec.get("address")
            if row.latitude is None and rec.get("latitude") is not None:
                row.latitude = Decimal(str(rec["latitude"]))
            if row.longitude is None and rec.get("longitude") is not None:
                row.longitude = Decimal(str(rec["longitude"]))
            if row.source not in (SOURCE_MERGED,):
                row.source = SOURCE_SEED

    bus.emit(
        Event(
            restaurant_id=restaurant_id,
            stage=SUBSTAGE_SEED,
            status="complete",
            payload={"count": len(records)},
        )
    )
    return len(records)


# ---------- places ---------------------------------------------------------


async def _ingest_places(restaurant: Restaurant) -> tuple[int, int, int]:
    """Returns (places_kept, places_filtered_out, duplicates_merged)."""
    bus = get_bus()
    rid = restaurant.id
    if restaurant.latitude is None or restaurant.longitude is None:
        log.warning("places.skip.no_coords", restaurant_id=rid)
        return 0, 0, 0

    bus.emit(
        Event(
            restaurant_id=rid,
            stage=SUBSTAGE_PLACES,
            status="start",
            payload={"lat": float(restaurant.latitude), "lng": float(restaurant.longitude)},
        )
    )

    async with httpx.AsyncClient() as http:
        candidates = await search_distributor_candidates(
            http,
            latitude=float(restaurant.latitude),
            longitude=float(restaurant.longitude),
        )
    if not candidates:
        bus.emit(
            Event(
                restaurant_id=rid,
                stage=SUBSTAGE_PLACES,
                status="complete",
                payload={"raw": 0, "kept": 0, "filtered_out": 0, "merged": 0},
            )
        )
        return 0, 0, 0

    bus.emit(
        Event(
            restaurant_id=rid,
            stage=SUBSTAGE_PLACES,
            status="progress",
            payload={"raw_results": len(candidates), "filtering": True},
        )
    )

    filtered = await filter_noise_with_claude(candidates)
    log.info(
        "places.filter.applied",
        raw=len(candidates),
        kept=len(filtered.kept),
        rejected=len(filtered.rejected),
    )

    duplicates_merged = await _persist_places(filtered.kept)

    bus.emit(
        Event(
            restaurant_id=rid,
            stage=SUBSTAGE_PLACES,
            status="complete",
            payload={
                "raw": len(candidates),
                "kept": len(filtered.kept),
                "filtered_out": len(filtered.rejected),
                "merged": duplicates_merged,
            },
        )
    )
    return len(filtered.kept), len(filtered.rejected), duplicates_merged


def _places_types_to_specialties(types: list[str]) -> list[str]:
    """Best-effort projection from Google Place types into our specialty vocab."""
    tags: set[str] = set()
    lc = {t.lower() for t in types}
    if any(t in lc for t in ("food_store", "grocery_store", "supermarket")):
        tags.add("produce")
        tags.add("dry_goods")
    if "wholesaler" in lc:
        tags.add("dry_goods")
    if any(t in lc for t in ("meat_shop", "butcher_shop")):
        tags.add("protein_meat")
    if "seafood" in lc or "fish_market" in lc:
        tags.add("protein_seafood")
    if "bakery" in lc:
        tags.add("bakery")
    return sorted(tags)


async def _persist_places(candidates: list[PlaceCandidate]) -> int:
    """Insert new Places rows or merge into existing seed rows. Returns merge count."""
    merged = 0
    async with SessionLocal() as session, session.begin():
        existing = (await session.execute(select(Distributor))).scalars().all()
        by_name: dict[str, Distributor] = {_normalize_name(d.name): d for d in existing}

        for c in candidates:
            norm = _normalize_name(c.name)
            row = by_name.get(norm)
            place_specs = _places_types_to_specialties(c.types)

            if row is None:
                session.add(
                    Distributor(
                        name=c.name,
                        address=c.address,
                        phone=c.phone,
                        email=None,
                        website=c.website,
                        latitude=(Decimal(str(c.latitude)) if c.latitude is not None else None),
                        longitude=(Decimal(str(c.longitude)) if c.longitude is not None else None),
                        source=SOURCE_PLACES,
                        specialties=place_specs,
                    )
                )
                continue

            # Merge path — Places wins on phone/email/website, seed keeps the rest.
            if c.phone:
                row.phone = c.phone
            if c.website:
                row.website = c.website
            existing_specs = set(row.specialties or [])
            new_specs = existing_specs | set(place_specs)
            if new_specs != existing_specs:
                row.specialties = sorted(new_specs)
            row.source = SOURCE_MERGED
            merged += 1
    return merged


# ---------- orchestrator ---------------------------------------------------


@stage(STAGE_NAME)
async def discover_distributors(*, restaurant_id: int) -> DiscoveryResult:
    async with SessionLocal() as session:
        restaurant = await session.get(Restaurant, restaurant_id)
        if restaurant is None:
            raise LookupError(f"restaurant {restaurant_id} not found")

    seed_loaded = await _upsert_seed(restaurant_id)

    places_found = 0
    places_filtered_out = 0
    duplicates_merged = 0
    if settings.google_places_api_key:
        places_found, places_filtered_out, duplicates_merged = await _ingest_places(restaurant)
    else:
        log.info("places.skip.no_key", restaurant_id=restaurant_id)

    async with SessionLocal() as session:
        total = (await session.execute(select(func.count()).select_from(Distributor))).scalar_one()

    return DiscoveryResult(
        seed_loaded=seed_loaded,
        places_found=places_found,
        places_filtered_out=places_filtered_out,
        duplicates_merged=duplicates_merged,
        total_distributors=int(total),
    )
