"""Anthropic tool-use schemas. Each constant is ready to pass as a `tools` entry."""

EXTRACT_MENU_ITEMS = {
    "name": "extract_menu_items",
    "description": (
        "Extract every dish from a restaurant menu, along with the ingredients you "
        "would expect each dish to contain in a typical serving.\n\n"
        "Honest confidence scoring — both `parse_confidence` (per dish) and "
        "`estimation_confidence` (per ingredient) MUST reflect REAL uncertainty:\n"
        "  - 0.90-1.0  : ingredient is explicitly listed in the dish description "
        "(e.g. menu says 'kale, chicken, lemon-tahini dressing').\n"
        "  - 0.60-0.89 : ingredient is strongly implied but not listed (e.g. 'Caesar "
        "salad' implies romaine, parmesan, croutons, anchovies).\n"
        "  - 0.30-0.59 : reasonable guess based on category (e.g. 'House Bowl' with "
        "no description — you're inferring from cuisine).\n"
        "  - 0.00-0.29 : you're guessing; flag for human review.\n\n"
        "Quantities are per-serving estimates expressed in common units (e.g. "
        '`{"name":"chicken breast","quantity":4,"unit":"oz"}`, '
        '`{"name":"kale","quantity":1,"unit":"cup"}`). If you cannot '
        "estimate, leave quantity/unit null but still include the ingredient.\n\n"
        "Ignore non-food entries (allergen disclaimers, calorie footers, "
        "delivery-only badges, 'order now' buttons). Treat add-ons as separate "
        "dishes only when they have a price."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "dishes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "price": {
                            "type": ["number", "null"],
                            "description": "USD price; null if not listed.",
                        },
                        "parse_confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "Confidence the dish was correctly parsed.",
                        },
                        "ingredients": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "quantity": {"type": ["number", "null"]},
                                    "unit": {"type": ["string", "null"]},
                                    "estimation_confidence": {
                                        "type": "number",
                                        "minimum": 0,
                                        "maximum": 1,
                                    },
                                },
                                "required": ["name", "estimation_confidence"],
                            },
                        },
                    },
                    "required": ["name", "parse_confidence", "ingredients"],
                },
            }
        },
        "required": ["dishes"],
    },
}
