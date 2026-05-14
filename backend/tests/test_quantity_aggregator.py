from decimal import Decimal

from app.services.quantity_aggregator import (
    IngredientVolume,
    canonical_root,
    collapse_for_distributor,
)


def test_canonical_root_strips_qualifiers() -> None:
    assert canonical_root("Shredded Kale") == "kale"
    assert canonical_root("Organic Cilantro") == "cilantro"
    assert canonical_root("Vine Ripe Tomatoes") == "tomato"
    assert canonical_root("Antibiotic-Free Roasted Chicken") == "chicken"
    assert canonical_root("Extra Virgin Olive Oil") == "olive oil"


def test_canonical_root_preserves_distinct_ingredients() -> None:
    # Color words like "red"/"green" should NOT be stripped — different onions.
    assert canonical_root("Red Onion") != canonical_root("Green Onion")
    assert canonical_root("Goat Cheese") != canonical_root("Cheddar Cheese")


def _vol(
    id_: int,
    name: str,
    qty: Decimal | None,
    unit: str | None,
    dishes: int = 1,
) -> IngredientVolume:
    return IngredientVolume(
        ingredient_id=id_,
        ingredient_name=name,
        normalized_name=name.lower(),
        category="Vegetables and Vegetable Products",
        root=canonical_root(name),
        weekly_quantity=qty,
        unit=unit,
        dishes_used=dishes,
    )


def test_collapse_merges_kale_wording_variants() -> None:
    volumes = [
        _vol(1, "Shredded Kale", Decimal("244"), "oz", dishes=3),
        _vol(2, "Organic Kale", Decimal("60"), "oz", dishes=1),
        _vol(3, "Vine Ripe Tomatoes", Decimal("80"), "oz", dishes=2),
    ]
    out = collapse_for_distributor(volumes)
    by_root = {v.root: v for v in out}
    assert "kale" in by_root and "tomato" in by_root
    assert by_root["kale"].weekly_quantity == Decimal("304")
    assert by_root["kale"].variant_count == 2
    assert by_root["kale"].dishes_used == 4
    assert by_root["tomato"].variant_count == 1


def test_collapse_drops_quantity_on_unit_conflict() -> None:
    volumes = [
        _vol(1, "Kale", Decimal("200"), "oz"),
        _vol(2, "Shredded Kale", Decimal("5"), "lb"),
    ]
    out = collapse_for_distributor(volumes)
    assert len(out) == 1
    merged = out[0]
    assert merged.root == "kale"
    # Mixed units → drop quantity rather than mix oz+lb.
    assert merged.weekly_quantity is None
    assert merged.unit is None
    assert merged.variant_count == 2


def test_collapse_handles_missing_quantity_gracefully() -> None:
    volumes = [
        _vol(1, "Cilantro", None, None),
        _vol(2, "Organic Cilantro", Decimal("10"), "oz"),
    ]
    out = collapse_for_distributor(volumes)
    assert len(out) == 1
    # One side has None — we keep whatever clean number we have.
    assert out[0].weekly_quantity == Decimal("10")
    assert out[0].unit == "oz"
