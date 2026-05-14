"""Anthropic tool-use schemas. Each constant is ready to pass as a `tools` entry."""

CLASSIFY_DISTRIBUTORS = {
    "name": "classify_distributor_candidates",
    "description": (
        "Decide which of the Google Places candidates are genuine wholesale food "
        "distributors that a restaurant would actually send an RFP to.\n\n"
        "ACCEPT examples: wholesale produce distributor, foodservice broadline, "
        "meat / seafood / dairy wholesaler, specialty foods importer, "
        "restaurant-supply foodservice company.\n\n"
        "REJECT examples (return false): retail grocery chains (Harris Teeter, "
        "Publix, Whole Foods, Trader Joe's), warehouse clubs (Costco, Sam's "
        "Club, BJ's), retail restaurant-supply stores aimed at consumers, "
        "convenience stores, fast-food outlets, gas-station markets, "
        "individual restaurants, farms not operating as distributors, "
        "non-food businesses caught by a generic 'wholesale' keyword "
        "(electronics wholesale, beauty supply, etc.).\n\n"
        "You receive an array of candidates with `index`, `name`, "
        "`address`, and `types`. For EACH candidate return an object "
        "`{index, is_wholesale_distributor, reason}`. Keep `reason` "
        "to one short sentence."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "is_wholesale_distributor": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["index", "is_wholesale_distributor", "reason"],
                },
            }
        },
        "required": ["decisions"],
    },
}


PICK_FDC_MATCH = {
    "name": "pick_fdc_match",
    "description": (
        "You are choosing the best USDA FoodData Central match for an ingredient "
        "name extracted from a restaurant menu. You will be given:\n"
        "  - the ingredient name as it appeared on the menu\n"
        "  - up to 5 candidate FDC foods, each with `fdc_id`, `description`, "
        "`food_category`, `score`, and `data_type`.\n\n"
        "Pick the single candidate that best represents the raw ingredient a "
        "restaurant would actually purchase, and return its `fdc_id`. "
        "Prefer Foundation Foods > SR Legacy > Survey (FNDDS); branded snack "
        "products are usually wrong matches for menu ingredients (e.g. don't "
        "pick a granola bar for 'oats'). Return `fdc_id: null` if NONE of the "
        "candidates are a credible match — that's the correct answer when the "
        "search returned unrelated items. Always provide a short `rationale`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "fdc_id": {
                "type": ["integer", "null"],
                "description": "The chosen FDC id, or null if no candidate fits.",
            },
            "rationale": {
                "type": "string",
                "description": "One-sentence justification.",
            },
        },
        "required": ["fdc_id", "rationale"],
    },
}

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
