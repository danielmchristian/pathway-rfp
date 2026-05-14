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

PARSE_QUOTE = {
    "name": "parse_quote",
    "description": (
        "Extract structured quote data from a distributor's reply to a "
        "wholesale RFP email. You are given the reply body plus the list of "
        "ingredients we asked this distributor to quote.\n\n"
        "For each ingredient MENTIONED IN THE REPLY, produce one quotes[] "
        "entry. **Critical rule**: if an asked-for ingredient is NOT "
        "mentioned anywhere in the reply body, do NOT produce a quotes[] "
        "entry for it — its absence will be inferred separately. Only "
        "emit a quotes[] entry when the distributor's reply explicitly "
        "addresses that ingredient (even to decline pricing on it). If "
        "the distributor quoted an ingredient that wasn't in our ask "
        "list, still include it (we'll flag it on our side) — DO NOT "
        "skip an ingredient the distributor explicitly addressed.\n\n"
        "Fields per quote:\n"
        "  - `ingredient_name`: the item the quote covers (use the name the "
        "distributor used; we'll fuzzy-match to our list).\n"
        "  - `unit_price`: number, in USD per the quoted unit. Null if the "
        "distributor did not state a price.\n"
        "  - `unit`: the unit the price refers to (e.g. 'lb', 'case', 'each', "
        "'bunch', 'gallon'). Null if not stated.\n"
        "  - `min_order_qty`: minimum order quantity in the quoted unit; null "
        "if not stated.\n"
        "  - `delivery_days`: integer days from order to delivery; null if "
        "not stated.\n"
        "  - `terms`: short string of payment / delivery terms (e.g. 'net 30', "
        "'COD'); null if not stated.\n"
        "  - `missing_fields`: list of which of the four fields above were "
        "absent from the quote — values must be exactly: 'unit_price', "
        "'unit', 'min_order_qty', 'delivery_days', or 'terms'.\n"
        "  - `parse_confidence`: 0.0–1.0 — how clean was this single item?\n\n"
        "Top-level fields:\n"
        "  - `quotes`: the array above.\n"
        "  - `overall_parse_confidence`: 0.0–1.0 — how clean was the reply?\n"
        "  - `off_topic`: true if the reply is NOT actually a quote (auto-"
        "responder, OOO, unrelated message, marketing). When true, `quotes` "
        "should be empty.\n"
        "  - `note`: optional one-sentence operator note (e.g. 'reply is a "
        "vacation auto-responder', 'distributor declined to quote on items "
        "X, Y because they are out of season')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "quotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ingredient_name": {"type": "string"},
                        "unit_price": {"type": ["number", "null"]},
                        "unit": {"type": ["string", "null"]},
                        "min_order_qty": {"type": ["number", "null"]},
                        "delivery_days": {"type": ["integer", "null"]},
                        "terms": {"type": ["string", "null"]},
                        "missing_fields": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "parse_confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": [
                        "ingredient_name",
                        "missing_fields",
                        "parse_confidence",
                    ],
                },
            },
            "overall_parse_confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "off_topic": {"type": "boolean"},
            "note": {"type": ["string", "null"]},
        },
        "required": ["quotes", "overall_parse_confidence", "off_topic"],
    },
}


COMPOSE_FOLLOWUP_EMAIL = {
    "name": "compose_followup_email",
    "description": (
        "Compose a SINGLE follow-up email to a distributor whose RFP reply "
        "was incomplete. You are given the original ask, the distributor's "
        "reply body, and the list of fields/items they still owe us. "
        "Compose a short, polite, plain-text email asking specifically for "
        "the missing fields — do NOT re-ask for fields they already provided. "
        "Keep it under 150 words.\n\n"
        "OPENING (use this exact pattern): 'Thanks for your reply — to "
        "finalize our planning, could you confirm the following:'. NEVER "
        "write 'My name is'. Close with 'Best regards, / Procurement Team / "
        "{restaurant name}'.\n\n"
        "Return: `subject_tail` (no [RFP-id] prefix — we add it) and `body`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject_tail": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["subject_tail", "body"],
    },
}


COMPOSE_RFP_EMAIL = {
    "name": "compose_rfp_email",
    "description": (
        "Compose a wholesale RFP (Request for Pricing) email from a restaurant "
        "to a specific food distributor. You are writing on behalf of the "
        "restaurant's procurement team. The body must:\n\n"
        "  * OPENING: open with EXACTLY this pattern — 'I'm reaching out from "
        "the procurement team at {restaurant name} in {city, state}.' NEVER "
        "write 'My name is' or any sentence that requires a personal first "
        "name. Do not invent a person's name and do not leave a name "
        "placeholder blank.\n"
        "  * Briefly state the restaurant's positioning (one short clause).\n"
        "  * List the SPECIFIC ingredients we want quoted — provided to you in "
        "the user message. Use the ingredient's display name and weekly "
        "volume estimate verbatim. The list is COMPLETE: do NOT mention any "
        "ingredient outside it, even hypothetically. Do NOT hedge about items "
        "that 'might fall outside' the distributor's specialty — the list has "
        "already been filtered to ingredients this distributor can supply.\n"
        "  * Explicitly label volume numbers as planning estimates based on a "
        "covers-per-dish-per-week assumption — distributors should quote at "
        "their standard wholesale tiers, not treat this as a firm purchase order.\n"
        "  * If an item has a `conversion_note`, quote it verbatim in parentheses "
        "or in a short footnote so the distributor knows our planning conversion.\n"
        "  * Request: unit price, minimum order, delivery frequency, lead time, "
        "and any organic / sourcing certifications relevant to the ingredients.\n"
        "  * State the response deadline.\n"
        "  * Close with: 'Best regards, / Procurement Team / {restaurant name}'. "
        "Do NOT invent a personal signature.\n\n"
        "Style: professional, concise, no marketing fluff. Plain text only — no "
        "HTML or markdown formatting. Aim for ~200-350 words.\n\n"
        "Return TWO fields: `subject_tail` (a short, descriptive subject WITHOUT "
        "any [RFP-id] prefix — we add that ourselves) and `body` (the full "
        "plain-text body)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "subject_tail": {
                "type": "string",
                "description": (
                    "Short descriptive subject line, no [RFP-id] prefix. "
                    "e.g. 'Ingredient quote request — Sweetgreen Charlotte'."
                ),
            },
            "body": {
                "type": "string",
                "description": "Full plain-text email body.",
            },
        },
        "required": ["subject_tail", "body"],
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
