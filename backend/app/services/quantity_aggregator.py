"""Per-ingredient weekly volume aggregator with wording-variant dedupe.

Joins dishes → dish_ingredients → ingredients for one restaurant and sums
per-serving quantities × `covers_per_dish_per_week`, then collapses
wording-variant rows (e.g. "shredded kale" + "kale" + "organic kale")
under one canonical root so a single distributor isn't asked for the same
physical ingredient three times under different names.

The aggregator returns one IngredientVolume per ingredient row (preserving
ingredient_id for the foreign key on rfp_request_items) plus a `root` field
the orchestrator uses to merge per-distributor display lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dish import Dish
from app.models.dish_ingredient import DishIngredient
from app.models.ingredient import Ingredient

# Adjectives we strip when computing a canonical root for wording-variant
# dedupe. Order doesn't matter — we strip every occurrence. Kept conservative
# so we don't accidentally merge unrelated ingredients (e.g., we *do not*
# strip color words like "red"/"green" because red onion ≠ green onion).
_QUALIFIER_WORDS = {
    "organic",
    "fresh",
    "raw",
    "cooked",
    "roasted",
    "grilled",
    "shredded",
    "chopped",
    "diced",
    "sliced",
    "whole",
    "baby",
    "wild",
    "farm-raised",
    "antibiotic-free",
    "grass-fed",
    "pasture-raised",
    "free-range",
    "extra",
    "virgin",
    "vine",
    "ripe",
    "fresh-cracked",
    "freshly",
    "house-made",
    "house",
    "made",
    "cage-free",
}

_PUNCT_RE = re.compile(r"[^a-z0-9\s-]+")
_WS_RE = re.compile(r"\s+")
# Simple suffix stripper for plurals. Conservative: only "s" or "es" at end
# of multi-character words. "tomatoes" → "tomato"; "kale" → "kale".
_PLURAL_RE = re.compile(r"(?<=\w\w)(es|s)$")


# ---------------------------------------------------------------------------
# Phase 5.1 — Wholesale-unit conversion
# ---------------------------------------------------------------------------
#
# Per-serving units (tbsp/cup/fl oz/slice) don't map to how wholesalers
# actually quote. Each rule below is keyed on (raw_unit, name-category) and
# returns a (factor, wholesale_unit, note) triple. Factors are planning
# approximations — distributors are asked to confirm in the email.
#
# Selection precedence (first match wins):
#   1. Composite items — leave raw (composite guard upstream already drops
#      these from distributor scope; this is belt-and-suspenders).
#   2. Name-category × raw-unit rule.
#   3. Generic unit rule (e.g. any oz → lb).
#   4. Fallback: keep original unit, flag with `conversion_note`.

HERB_TOKENS = ("basil", "cilantro", "parsley", "mint", "oregano", "thyme", "rosemary", "dill", "sage", "chive")
LEAFY_TOKENS = ("kale", "romaine", "spinach", "arugula", "lettuce", "spring mix")
GRAIN_TOKENS = ("rice", "quinoa", "farro", "oats", "barley", "couscous")
LEGUME_TOKENS = ("chickpea", "black bean", "lentil", "white bean", "pinto")
TOMATO_TOKENS = ("tomato", "tomatoes")
PROTEIN_TOKENS = (
    "chicken", "steak", "beef", "pork", "salmon", "tuna", "shrimp",
    "tofu", "tempeh", "turkey",
)
BREAD_TOKENS = ("bread", "bagel", "pita", "tortilla", "naan", "slice")
LIQUID_TOKENS = ("kombucha", "lemonade", "juice", "tea", "broth", "stock", "milk", "cream", "oil")

# Sanity ceilings on output. If conversion produces a value above the
# absurd-ceiling or below the rounded-to-zero floor, the rule fires but
# conversion_note flags the row.
SANITY_CEILINGS_LB = Decimal("10000")  # >10,000 lb/wk is suspicious for a single ingredient
SANITY_CEILINGS_GAL = Decimal("1000")   # >1,000 gal/wk for one liquid item
SANITY_CEILING_GENERIC = Decimal("50000")
SANITY_FLOOR = Decimal("0.01")  # below this, we flag (likely conversion-factor error)


def _name_in(name_lower: str, tokens: tuple[str, ...]) -> bool:
    return any(re.search(r"\b" + re.escape(t) + r"\b", name_lower) for t in tokens)


def normalize_to_wholesale_unit(
    ingredient_name: str,
    quantity: Decimal | None,
    unit: str | None,
) -> tuple[Decimal | None, str | None, str | None]:
    """Convert per-serving aggregate → plausible wholesale unit.

    Returns (wholesale_quantity, wholesale_unit, conversion_note). If the
    conversion is ambiguous or no rule applies, returns the original
    quantity/unit with a `conversion_note` flagging the gap so the email
    explicitly says "please quote in your standard unit."

    Sanity-check: results outside (SANITY_FLOOR, SANITY_CEILING) get
    flagged in `conversion_note` instead of shipping silently.
    """
    if quantity is None or unit is None:
        return None, None, None

    name_lower = (ingredient_name or "").lower()
    raw_unit = unit.lower().strip()

    # Resolve rule.
    wholesale_qty, wholesale_unit, base_note = _resolve_rule(
        name_lower=name_lower, qty=quantity, raw_unit=raw_unit
    )
    if wholesale_qty is None or wholesale_unit is None:
        # Fallback — keep raw, flag the gap.
        return (
            quantity,
            unit,
            "no wholesale rule applies; please quote in your standard unit",
        )

    # Sanity ceilings.
    sanity_note = _check_sanity(wholesale_qty, wholesale_unit)
    note = base_note
    if sanity_note:
        note = f"{base_note}; {sanity_note}" if base_note else sanity_note
    return wholesale_qty, wholesale_unit, note


def _resolve_rule(
    *, name_lower: str, qty: Decimal, raw_unit: str
) -> tuple[Decimal | None, str | None, str | None]:
    # 1. Volume-of-fresh-herbs → bunch (user-approved unit).
    if raw_unit in ("tbsp", "tablespoon", "tablespoons") and _name_in(name_lower, HERB_TOKENS):
        # ~50 tbsp per bunch at planning density; bunches vary by distributor.
        bunches = (qty / Decimal("50")).quantize(Decimal("1"))
        return (
            bunches,
            "bunch",
            f"~{qty} tbsp/week ≈ {bunches} bunches at planning density; "
            f"please confirm your standard bunch size",
        )
    if raw_unit in ("cup", "cups") and _name_in(name_lower, HERB_TOKENS):
        bunches = (qty / Decimal("3")).quantize(Decimal("1"))
        return (
            bunches,
            "bunch",
            f"~{qty} cups/week ≈ {bunches} bunches at planning density; "
            f"please confirm your standard bunch size",
        )

    # 2. Leafy greens cup → lb (~1 oz/cup chopped = 0.0625 lb/cup).
    if raw_unit in ("cup", "cups") and _name_in(name_lower, LEAFY_TOKENS):
        lb = (qty * Decimal("0.0625")).quantize(Decimal("0.1"))
        return (
            lb,
            "lb",
            "converted from cups at ~1 oz/cup chopped (planning density)",
        )

    # 3. Tomatoes cup → lb (~6.5 oz/cup chopped).
    if raw_unit in ("cup", "cups") and _name_in(name_lower, TOMATO_TOKENS):
        lb = (qty * Decimal("0.40")).quantize(Decimal("0.1"))
        return (
            lb,
            "lb",
            "converted from cups at ~6.5 oz/cup chopped (planning density)",
        )

    # 4. Grains cup → lb (dry, ~6.4 oz/cup).
    if raw_unit in ("cup", "cups") and _name_in(name_lower, GRAIN_TOKENS):
        lb = (qty * Decimal("0.40")).quantize(Decimal("0.1"))
        return (
            lb,
            "lb",
            "converted from dry cups at ~6.4 oz/cup (planning density)",
        )

    # 5. Legumes/beans cup → lb (dry, ~7.2 oz/cup).
    if raw_unit in ("cup", "cups") and _name_in(name_lower, LEGUME_TOKENS):
        lb = (qty * Decimal("0.45")).quantize(Decimal("0.1"))
        return (
            lb,
            "lb",
            "converted from dry cups at ~7.2 oz/cup (planning density)",
        )

    # 6. Fluid ounce (any) → gallon (128 fl oz = 1 gal).
    if raw_unit in ("fl oz", "fluid oz", "floz", "fl. oz", "fluid ounce", "fluid ounces"):
        gal = (qty / Decimal("128")).quantize(Decimal("0.01"))
        return gal, "gallon", "converted from fl oz (128 fl oz/gallon)"

    # 7. Weight ounce → lb (16 oz = 1 lb). Especially for proteins.
    if raw_unit in ("oz", "ounce", "ounces"):
        lb = (qty / Decimal("16")).quantize(Decimal("0.1"))
        if _name_in(name_lower, PROTEIN_TOKENS):
            return lb, "lb", "converted from ounces (16 oz/lb)"
        return lb, "lb", "converted from ounces (16 oz/lb)"

    # 8. Slices / pieces → dozen for bakery items.
    if raw_unit in ("slice", "slices", "piece", "pieces") and _name_in(name_lower, BREAD_TOKENS):
        dozen = (qty / Decimal("12")).quantize(Decimal("0.1"))
        return dozen, "dozen", "converted from individual pieces (12 per dozen)"

    # 9. Already wholesale-friendly: ea, each, bunch, head, case, lb, gallon, dozen.
    if raw_unit in ("ea", "each", "bunch", "bunches", "head", "case", "lb", "lbs", "pound", "pounds"):
        return qty, raw_unit, None
    if raw_unit in ("gallon", "gallons", "gal"):
        return qty, "gallon", None
    if raw_unit in ("dozen", "doz"):
        return qty, "dozen", None

    # 10. Liquid name + cup → gallon (sometimes Claude reports beverage as "cup").
    if raw_unit in ("cup", "cups") and _name_in(name_lower, LIQUID_TOKENS):
        gal = (qty / Decimal("16")).quantize(Decimal("0.01"))  # 16 cups = 1 gallon
        return gal, "gallon", "converted from cups (16 cups/gallon)"

    # No rule.
    return None, None, None


def _check_sanity(qty: Decimal, unit: str) -> str | None:
    """Return a flag string if the converted value is absurd or rounded-to-zero."""
    if qty < SANITY_FLOOR:
        return (
            f"converted quantity rounds below {SANITY_FLOOR} {unit} — likely "
            f"a conversion-factor mismatch; please confirm needed volume"
        )
    if unit == "lb" and qty > SANITY_CEILINGS_LB:
        return (
            f"converted quantity exceeds {SANITY_CEILINGS_LB} lb/week — please "
            f"confirm; this may indicate a conversion mismatch"
        )
    if unit == "gallon" and qty > SANITY_CEILINGS_GAL:
        return (
            f"converted quantity exceeds {SANITY_CEILINGS_GAL} gal/week — "
            f"please confirm; this may indicate a conversion mismatch"
        )
    if qty > SANITY_CEILING_GENERIC:
        return (
            f"converted quantity exceeds {SANITY_CEILING_GENERIC} {unit}/week — "
            f"please confirm; this may indicate a conversion mismatch"
        )
    return None


def apply_wholesale_conversion(volumes: list[IngredientVolume]) -> None:
    """Mutate `volumes` in place, populating wholesale_quantity/unit/note."""
    for v in volumes:
        wq, wu, note = normalize_to_wholesale_unit(
            v.ingredient_name, v.weekly_quantity, v.unit
        )
        v.wholesale_quantity = wq
        v.wholesale_unit = wu
        v.conversion_note = note


def canonical_root(name: str) -> str:
    """Best-effort canonical root for wording-variant dedupe.

    "Shredded Kale" → "kale", "vine ripe tomatoes" → "tomato",
    "Organic Cilantro" → "cilantro".
    """
    s = (name or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    tokens = [t for t in _WS_RE.split(s) if t and t not in _QUALIFIER_WORDS]
    if not tokens:
        return s.strip()
    # Drop trailing tokens that are just qualifiers themselves; rejoin.
    root = " ".join(tokens).strip()
    return _PLURAL_RE.sub("", root)


@dataclass
class IngredientVolume:
    ingredient_id: int
    ingredient_name: str
    normalized_name: str
    category: str | None
    root: str
    weekly_quantity: Decimal | None
    unit: str | None
    dishes_used: int
    # Set by collapse_for_distributor() — counts how many *physical*
    # variants in this distributor's scope collapsed under the same root.
    variant_count: int = 1
    # Phase 5.1 — wholesale-unit conversion. The raw per-serving aggregate
    # stays in weekly_quantity/unit (used for rfp_request_items.quantity);
    # the email body uses these fields when populated.
    wholesale_quantity: Decimal | None = None
    wholesale_unit: str | None = None
    conversion_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ingredient_id": self.ingredient_id,
            "name": self.ingredient_name,
            "normalized_name": self.normalized_name,
            "category": self.category,
            "root": self.root,
            "weekly_quantity": (str(self.weekly_quantity) if self.weekly_quantity else None),
            "unit": self.unit,
            "dishes_used": self.dishes_used,
            "variant_count": self.variant_count,
            "wholesale_quantity": (
                str(self.wholesale_quantity) if self.wholesale_quantity else None
            ),
            "wholesale_unit": self.wholesale_unit,
            "conversion_note": self.conversion_note,
        }


async def aggregate_weekly_volumes(
    *,
    session: AsyncSession,
    restaurant_id: int,
    covers_per_dish_per_week: int,
) -> list[IngredientVolume]:
    """Sum per-serving quantities × covers/week per ingredient row.

    Returns ONE row per Ingredient.id (no dedupe yet) so the orchestrator
    has the full picture before it scopes to a single distributor.
    """
    stmt = (
        select(DishIngredient, Ingredient, Dish)
        .join(Ingredient, Ingredient.id == DishIngredient.ingredient_id)
        .join(Dish, Dish.id == DishIngredient.dish_id)
        .where(Dish.restaurant_id == restaurant_id)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []

    by_ingredient: dict[int, IngredientVolume] = {}
    # Track which units we've seen per ingredient so we don't accidentally
    # add cups + ounces. If an ingredient appears with conflicting units,
    # we drop quantity (orchestrator will surface unit=None as a hint to
    # the distributor that we don't have a clean weekly estimate).
    seen_units: dict[int, set[str]] = {}

    for di, ing, _dish in rows:
        per_serving = di.quantity
        per_week = (
            (per_serving * Decimal(covers_per_dish_per_week))
            if per_serving is not None
            else None
        )
        unit = di.unit
        entry = by_ingredient.get(ing.id)
        if entry is None:
            entry = IngredientVolume(
                ingredient_id=ing.id,
                ingredient_name=ing.name,
                normalized_name=ing.normalized_name,
                category=ing.category,
                root=canonical_root(ing.name),
                weekly_quantity=per_week,
                unit=unit,
                dishes_used=1,
            )
            by_ingredient[ing.id] = entry
            if unit:
                seen_units[ing.id] = {unit}
            continue

        entry.dishes_used += 1
        if unit:
            seen_units.setdefault(ing.id, set()).add(unit)
        # Sum quantities only if units agree (or both rows lack a unit).
        if per_week is None or entry.weekly_quantity is None:
            # One side is unknown — keep whatever total we have, don't add
            # uncertainty silently. The label in the email already disclaims
            # planning estimates.
            entry.weekly_quantity = entry.weekly_quantity or per_week
        elif len(seen_units.get(ing.id, set())) <= 1:
            entry.weekly_quantity = entry.weekly_quantity + per_week
        else:
            # Unit conflict — abandon the quantity for this ingredient.
            entry.weekly_quantity = None
            entry.unit = None

    return list(by_ingredient.values())


def collapse_for_distributor(
    volumes: list[IngredientVolume],
) -> list[IngredientVolume]:
    """Wording-variant dedupe.

    Given the ingredients in *one distributor's* matched scope, merge rows
    that share a canonical root. The first row's display name + ingredient_id
    win (used for the rfp_request_items FK); subsequent rows contribute
    their weekly_quantity (only when units agree).
    """
    by_root: dict[str, IngredientVolume] = {}
    for v in volumes:
        head = by_root.get(v.root)
        if head is None:
            # Copy so we don't mutate the caller's list.
            by_root[v.root] = IngredientVolume(
                ingredient_id=v.ingredient_id,
                ingredient_name=v.ingredient_name,
                normalized_name=v.normalized_name,
                category=v.category,
                root=v.root,
                weekly_quantity=v.weekly_quantity,
                unit=v.unit,
                dishes_used=v.dishes_used,
                variant_count=1,
            )
            continue
        head.variant_count += 1
        head.dishes_used += v.dishes_used
        if head.weekly_quantity is None or v.weekly_quantity is None:
            # If either side lacks a clean number, prefer the one we have
            # but stop adding — partial sums are misleading. Also adopt
            # the unit that came with the quantity we kept.
            if head.weekly_quantity is None and v.weekly_quantity is not None:
                head.weekly_quantity = v.weekly_quantity
                head.unit = v.unit
            continue
        if (head.unit or "") == (v.unit or ""):
            head.weekly_quantity = head.weekly_quantity + v.weekly_quantity
        else:
            # Unit conflict — drop quantity rather than mix units.
            head.weekly_quantity = None
            head.unit = None
    return sorted(by_root.values(), key=lambda x: x.ingredient_name.lower())
