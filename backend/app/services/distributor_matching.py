"""Score distributors against a restaurant's ingredients.

Pure compute (no I/O). Translates each ingredient's FDC category into a set of
specialty tags from our canonical vocabulary, counts how many of the restaurant's
ingredients have at least one tag in common with the distributor's `specialties`,
and tie-breaks by Haversine distance from the restaurant.

Per Phase 4 amendment 3, Spices and Herbs is tagged `produce + leafy_greens +
dry_goods` so fresh cilantro/basil match produce distributors, not just
dry-goods/spice shops.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal

from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.restaurant import Restaurant

# Canonical specialty vocabulary used by the seed file.
SPECIALTY_VOCAB: set[str] = {
    "produce",
    "leafy_greens",
    "tomatoes",
    "protein_meat",
    "protein_poultry",
    "protein_seafood",
    "dairy_eggs",
    "dry_goods",
    "oils",
    "bakery",
    "beverages",
    "organic",
    "specialty_ethnic",
}


# FDC category → specialty tags. Empty list means "no canonical distributor
# category" (e.g., baked goods that the ingredient parser surfaces as a
# category but for which no distributor specializes).
_CATEGORY_MAP: dict[str, list[str]] = {
    "Vegetables and Vegetable Products": ["produce"],
    "Fruits and Fruit Juices": ["produce"],
    "Poultry Products": ["protein_poultry", "protein_meat"],
    "Beef Products": ["protein_meat"],
    "Pork Products": ["protein_meat"],
    "Lamb, Veal, and Game Products": ["protein_meat"],
    "Sausages and Luncheon Meats": ["protein_meat"],
    "Finfish and Shellfish Products": ["protein_seafood"],
    "Dairy and Egg Products": ["dairy_eggs"],
    "Fats and Oils": ["oils", "dry_goods"],
    "Cereal Grains and Pasta": ["dry_goods"],
    "Legumes and Legume Products": ["dry_goods"],
    "Nut and Seed Products": ["dry_goods"],
    "Spices and Herbs": ["produce", "leafy_greens", "dry_goods"],
    "Baked Products": ["bakery", "dry_goods"],
    "Beverages": ["beverages"],
    "Soups, Sauces, and Gravies": ["dry_goods", "specialty_ethnic"],
    "Sweets": ["dry_goods"],
    "Snacks": ["dry_goods"],
    "American Indian/Alaska Native Foods": [],
    "Restaurant Foods": [],
    "Meals, Entrees, and Side Dishes": [],
}

# Phase 5.1 — Composite/prepared-item guard. If an ingredient name contains
# any of these tokens it's an in-house preparation (sauce, dressing, etc.).
# Such items are returned as `unassigned` rather than routed to raw-ingredient
# distributors, because Sweetgreen-style restaurants make these in-house and
# routing them invents a supply relationship that doesn't exist.
_COMPOSITE_TOKENS = (
    "sauce",
    "dressing",
    "vinaigrette",
    "marinade",
    "glaze",
    "aioli",
    "syrup",
    "spread",
    "pesto",
    "salsa",
    "hummus",
    "tahini",
    "paste",
    "mayo",
    "mayonnaise",
    "compound butter",
)
_COMPOSITE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _COMPOSITE_TOKENS) + r")\b",
    re.IGNORECASE,
)


def is_composite_name(name: str) -> bool:
    """Is the ingredient a prepared/composite item? See _COMPOSITE_TOKENS."""
    return bool(_COMPOSITE_RE.search(name or ""))


# Phase 5.1 — Word-boundary name hints. Each entry tuple: (regex, tags).
# The regex uses `\b` so "tea" inside "steak" doesn't fire. Multi-word
# hints (e.g. "spring mix") are kept as exact phrase regexes.
# Fallback for ingredients where the FDC category is missing or too broad.
_NAME_HINT_TAGS_RAW: dict[str, list[str]] = {
    "kale": ["leafy_greens"],
    "romaine": ["leafy_greens"],
    "spinach": ["leafy_greens"],
    "arugula": ["leafy_greens"],
    "lettuce": ["leafy_greens"],
    "spring mix": ["leafy_greens"],
    "tomato": ["tomatoes"],
    "tomatoes": ["tomatoes"],
    "cilantro": ["leafy_greens"],
    "basil": ["leafy_greens"],
    "parsley": ["leafy_greens"],
    "mint": ["leafy_greens"],
    "salmon": ["protein_seafood"],
    "tuna": ["protein_seafood"],
    "shrimp": ["protein_seafood"],
    "chicken": ["protein_poultry", "protein_meat"],
    "steak": ["protein_meat"],
    "beef": ["protein_meat"],
    "pork": ["protein_meat"],
    "tea": ["beverages"],
    "kombucha": ["beverages"],
    "lemonade": ["beverages"],
    "juice": ["beverages"],
}

# Compiled word-boundary regexes. Word-boundary is sufficient because all
# hints are alphanumeric; the precompile keeps `specialty_tags_for` cheap.
_NAME_HINT_PATTERNS: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"\b" + re.escape(h) + r"\b", re.IGNORECASE), tags)
    for h, tags in _NAME_HINT_TAGS_RAW.items()
]


def specialty_tags_for(ingredient: Ingredient) -> set[str]:
    """Resolve an ingredient's specialty tags from FDC category + name hints.

    Phase 5.1 hardening:
      * Composite/prepared items (sauces, dressings, …) return an empty set
        regardless of name-hint matches. Sauces are made in-house; routing
        them invents a supply relationship.
      * Name hints use `\\b` word boundaries so "tea" inside "steak" does
        not fire.
    """
    name = ingredient.name or ""
    if is_composite_name(name):
        return set()
    tags: set[str] = set()
    if ingredient.category and ingredient.category in _CATEGORY_MAP:
        tags.update(_CATEGORY_MAP[ingredient.category])
    for pattern, hint_tags in _NAME_HINT_PATTERNS:
        if pattern.search(name):
            tags.update(hint_tags)
    return tags


def _distance_km(
    lat1: float | None,
    lon1: float | None,
    lat2: float | None,
    lon2: float | None,
) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371.0
    phi1 = math.radians(lat1)  # type: ignore[arg-type]
    phi2 = math.radians(lat2)  # type: ignore[arg-type]
    dphi = math.radians((lat2 or 0) - (lat1 or 0))
    dlam = math.radians((lon2 or 0) - (lon1 or 0))
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class ScoredDistributor:
    distributor_id: int
    name: str
    specialties: list[str]
    source: str | None
    matched_ingredient_count: int
    total_ingredients: int
    match_pct: float
    sample_matched_ingredients: list[str]
    distance_km: float | None


def _decimal_to_float(v: Decimal | float | None) -> float | None:
    if v is None:
        return None
    return float(v)


def score_distributors(
    *,
    ingredients: list[Ingredient],
    distributors: list[Distributor],
    restaurant: Restaurant | None = None,
) -> list[ScoredDistributor]:
    if not distributors:
        return []
    total_ingredients = len(ingredients)

    ingredient_tags: list[tuple[Ingredient, set[str]]] = [
        (ing, specialty_tags_for(ing)) for ing in ingredients
    ]

    r_lat = _decimal_to_float(restaurant.latitude) if restaurant else None
    r_lon = _decimal_to_float(restaurant.longitude) if restaurant else None

    out: list[ScoredDistributor] = []
    for d in distributors:
        d_specialties_lc = {s.lower() for s in (d.specialties or [])}
        matched: list[str] = []
        for ing, tags in ingredient_tags:
            if tags & d_specialties_lc:
                matched.append(ing.name)
        match_pct = (len(matched) / total_ingredients * 100.0) if total_ingredients else 0.0
        out.append(
            ScoredDistributor(
                distributor_id=d.id,
                name=d.name,
                specialties=list(d.specialties or []),
                source=d.source,
                matched_ingredient_count=len(matched),
                total_ingredients=total_ingredients,
                match_pct=round(match_pct, 2),
                sample_matched_ingredients=matched[:5],
                distance_km=_distance_km(
                    r_lat,
                    r_lon,
                    _decimal_to_float(d.latitude),
                    _decimal_to_float(d.longitude),
                ),
            )
        )
    # Sort: more matches first, then closer first (None last).
    out.sort(
        key=lambda s: (
            -s.matched_ingredient_count,
            s.distance_km if s.distance_km is not None else math.inf,
            s.distributor_id,
        )
    )
    return out
