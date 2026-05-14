"""Phase 5.1 hardening tests for the matcher."""

from app.models.ingredient import Ingredient
from app.services.distributor_matching import is_composite_name, specialty_tags_for


def _ing(name: str, category: str | None = None) -> Ingredient:
    return Ingredient(
        id=hash(name) & 0xFFFFFF,
        name=name,
        normalized_name=name.lower(),
        category=category,
    )


def test_composite_guard_drops_sauces() -> None:
    # The original v1 bug — "cilantro" substring fires leafy_greens inside
    # a sauce name. After 5.1 the composite guard zeroes the tag set.
    assert specialty_tags_for(_ing("Lime Cilantro Jalapeño Sauce")) == set()
    assert specialty_tags_for(_ing("Miso Ginger Dressing")) == set()
    assert specialty_tags_for(_ing("Smoky Tomatillo Salsa")) == set()
    assert specialty_tags_for(_ing("Lemon Tahini")) == set()
    assert specialty_tags_for(_ing("Spicy Cashew Aioli")) == set()


def test_composite_guard_preserves_raw_ingredients() -> None:
    # Raw produce/proteins/herbs still get their tags.
    assert "leafy_greens" in specialty_tags_for(_ing("Shredded Kale"))
    assert "leafy_greens" in specialty_tags_for(_ing("Basil"))
    assert "protein_meat" in specialty_tags_for(_ing("Caramelized Garlic Steak"))


def test_word_boundary_fix_tea_inside_steak() -> None:
    # The original v1 bug — "tea" substring matches "stea**k**". Word-boundary
    # regex now prevents the false positive.
    tags = specialty_tags_for(_ing("caramelized garlic steak"))
    assert "beverages" not in tags
    assert "protein_meat" in tags


def test_real_beverage_still_matches_beverages() -> None:
    # The fix must NOT break legitimate beverage matches.
    assert "beverages" in specialty_tags_for(_ing("Kombucha (brewed tea)"))
    assert "beverages" in specialty_tags_for(_ing("Jasmine Green Tea"))
    assert "beverages" in specialty_tags_for(_ing("Lemonade"))


def test_is_composite_name_detects_common_patterns() -> None:
    assert is_composite_name("Miso Ginger Dressing")
    assert is_composite_name("Honey Dijon Vinaigrette")
    assert is_composite_name("Pesto Genovese")
    assert is_composite_name("Compound Butter — Lemon Herb")
    # Not composites:
    assert not is_composite_name("Shredded Kale")
    assert not is_composite_name("Roasted Chicken")
    assert not is_composite_name("Olive Oil")  # raw ingredient
