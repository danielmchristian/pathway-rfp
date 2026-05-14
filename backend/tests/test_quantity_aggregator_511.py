"""Phase 5.1 — wholesale unit conversion + sanity ceilings."""

from decimal import Decimal

from app.services.quantity_aggregator import (
    IngredientVolume,
    apply_wholesale_conversion,
    canonical_root,
    normalize_to_wholesale_unit,
)


def _v(name: str, qty: Decimal | None, unit: str | None) -> IngredientVolume:
    return IngredientVolume(
        ingredient_id=hash(name) & 0xFFFFFF,
        ingredient_name=name,
        normalized_name=name.lower(),
        category=None,
        root=canonical_root(name),
        weekly_quantity=qty,
        unit=unit,
        dishes_used=1,
    )


def test_herbs_tbsp_to_bunch_with_explicit_note() -> None:
    qty, unit, note = normalize_to_wholesale_unit("Basil", Decimal("600"), "tbsp")
    assert unit == "bunch"
    assert qty == Decimal("12")
    assert note is not None and "bunch" in note and "confirm" in note


def test_leafy_greens_cup_to_lb() -> None:
    qty, unit, note = normalize_to_wholesale_unit("Shredded Kale", Decimal("1600"), "cup")
    assert unit == "lb"
    assert qty == Decimal("100.0")  # 1600 * 0.0625
    assert note and "chopped" in note


def test_tomato_cup_to_lb() -> None:
    qty, unit, _ = normalize_to_wholesale_unit("Vine Ripe Tomatoes", Decimal("400"), "cup")
    assert unit == "lb"
    assert qty == Decimal("160.0")  # 400 * 0.40


def test_fl_oz_to_gallon() -> None:
    qty, unit, note = normalize_to_wholesale_unit("Kombucha", Decimal("4800"), "fl oz")
    assert unit == "gallon"
    assert qty == Decimal("37.50")
    assert note and "fl oz" in note


def test_oz_to_lb_for_proteins() -> None:
    qty, unit, _ = normalize_to_wholesale_unit("Roasted Chicken", Decimal("800"), "oz")
    assert unit == "lb"
    assert qty == Decimal("50.0")


def test_slices_to_dozen_for_bakery() -> None:
    qty, unit, note = normalize_to_wholesale_unit(
        "Bread Slice", Decimal("60"), "slice"
    )
    assert unit == "dozen"
    assert qty == Decimal("5.0")
    assert note and "dozen" in note


def test_ambiguous_unit_falls_through_with_flag() -> None:
    # No rule for "pinch" of saffron — must keep unit + flag.
    qty, unit, note = normalize_to_wholesale_unit(
        "Saffron Threads", Decimal("12"), "pinch"
    )
    assert unit == "pinch"
    assert qty == Decimal("12")
    assert note and "standard unit" in note


def test_sanity_ceiling_flags_absurd_lb_output() -> None:
    # 200,000 oz → 12,500 lb — above 10,000 lb ceiling.
    qty, unit, note = normalize_to_wholesale_unit(
        "Chicken", Decimal("200000"), "oz"
    )
    assert unit == "lb"
    assert qty == Decimal("12500.0")
    assert note and "exceeds" in note


def test_sanity_floor_flags_rounded_to_zero() -> None:
    # Tiny tbsp → bunch is 0 (rounded). Should flag.
    qty, unit, note = normalize_to_wholesale_unit("Basil", Decimal("5"), "tbsp")
    assert unit == "bunch"
    assert qty == Decimal("0")
    assert note and ("rounds below" in note or "0.01" in note)


def test_apply_wholesale_conversion_populates_fields_in_place() -> None:
    volumes = [
        _v("Basil", Decimal("600"), "tbsp"),
        _v("Shredded Kale", Decimal("1600"), "cup"),
        _v("Kombucha", Decimal("4800"), "fl oz"),
    ]
    apply_wholesale_conversion(volumes)
    units = [v.wholesale_unit for v in volumes]
    assert units == ["bunch", "lb", "gallon"]
    assert all(v.conversion_note for v in volumes)


def test_already_wholesale_units_pass_through() -> None:
    qty, unit, note = normalize_to_wholesale_unit("Lemons", Decimal("50"), "ea")
    assert unit == "ea"
    assert qty == Decimal("50")
    assert note is None  # no conversion needed
