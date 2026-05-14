from decimal import Decimal

from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.restaurant import Restaurant
from app.services.distributor_matching import score_distributors, specialty_tags_for


def _ingredient(name: str, category: str | None) -> Ingredient:
    return Ingredient(
        id=hash(name) & 0xFFFFFF,
        name=name,
        normalized_name=name.lower(),
        category=category,
    )


def _distributor(
    id_: int, name: str, specialties: list[str], lat: float, lon: float
) -> Distributor:
    return Distributor(
        id=id_,
        name=name,
        specialties=specialties,
        source="seed",
        latitude=Decimal(str(lat)),
        longitude=Decimal(str(lon)),
    )


def test_specialty_tags_include_fresh_herbs_as_leafy_greens() -> None:
    cilantro = _ingredient("organic cilantro", "Spices and Herbs")
    tags = specialty_tags_for(cilantro)
    assert "produce" in tags
    assert "leafy_greens" in tags  # Phase 4 amendment 3
    assert "dry_goods" in tags


def test_specialty_tags_name_hint_for_tomato() -> None:
    tom = _ingredient("vine ripe tomatoes", "Vegetables and Vegetable Products")
    tags = specialty_tags_for(tom)
    assert "produce" in tags
    assert "tomatoes" in tags


def test_produce_distributor_outranks_seafood_for_sweetgreen_style_menu() -> None:
    ingredients = [
        _ingredient("organic shredded kale", "Vegetables and Vegetable Products"),
        _ingredient("vine ripe tomatoes", "Vegetables and Vegetable Products"),
        _ingredient("organic cilantro", "Spices and Herbs"),
        _ingredient("antibiotic-free roasted chicken", "Poultry Products"),
    ]
    restaurant = Restaurant(
        id=1, name="Sweetgreen", latitude=Decimal("35.18"), longitude=Decimal("-80.83")
    )
    produce = _distributor(1, "Produce Co", ["produce", "leafy_greens"], 35.26, -80.84)
    seafood = _distributor(2, "Seafood Co", ["protein_seafood"], 35.21, -80.94)
    meat = _distributor(3, "Meats Co", ["protein_meat", "protein_poultry"], 35.17, -80.87)

    scored = score_distributors(
        ingredients=ingredients,
        distributors=[seafood, produce, meat],
        restaurant=restaurant,
    )
    # Produce wins overall (3 matches: kale, tomato, cilantro)
    assert scored[0].name == "Produce Co"
    assert scored[0].matched_ingredient_count == 3
    # Meats matches just the chicken
    meat_row = next(s for s in scored if s.name == "Meats Co")
    assert meat_row.matched_ingredient_count == 1
    # Seafood is the control — nothing matches
    seafood_row = next(s for s in scored if s.name == "Seafood Co")
    assert seafood_row.matched_ingredient_count == 0


def test_distance_tie_breaker_picks_closer_when_score_equal() -> None:
    ingredients = [_ingredient("kale", "Vegetables and Vegetable Products")]
    restaurant = Restaurant(
        id=1, name="Anchor", latitude=Decimal("35.18"), longitude=Decimal("-80.83")
    )
    far = _distributor(1, "Far Produce", ["produce", "leafy_greens"], 36.00, -80.00)
    near = _distributor(2, "Near Produce", ["produce", "leafy_greens"], 35.20, -80.84)
    scored = score_distributors(
        ingredients=ingredients, distributors=[far, near], restaurant=restaurant
    )
    assert scored[0].name == "Near Produce"
    assert scored[1].name == "Far Produce"
