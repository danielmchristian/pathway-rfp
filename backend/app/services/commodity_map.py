"""Ingredient → AMS commodity slug mapping.

**Verified 2026-05-14** against the live USDA AMS Market News API via
`scripts/verify_ams_map.py`. Slug strings are exact Title-Case matches as
returned by `/services/v1.2/commodities` and confirmed present in the
Atlanta Terminal report payloads (slug_id 2278 vegetables, 2277 fruits).
Output committed at `scripts/verify_ams_map.out.txt` for audit.

Atlanta Terminal Market is the primary reference market for Charlotte NC,
the closest USDA terminal to our Sweetgreen restaurant.
"""

from __future__ import annotations

from dataclasses import dataclass

# Verified slug_ids from /reports — Atlanta Terminal Market.
ATLANTA_VEG_REPORT_ID = 2278  # AJ_FV020
ATLANTA_FRUIT_REPORT_ID = 2277  # AJ_FV010
MARKET_LOCATION = "Atlanta Terminal Market"  # exact market_location_name returned by API


@dataclass(frozen=True)
class AmsCommodity:
    slug: str
    report_id: int


# Verified slugs (exact case + punctuation per the live /commodities response).
# Note: kale is reported as "Greens, Kale" in the Atlanta vegetable feed; the
# bare "Kale" commodity exists in the catalog but doesn't appear in 2278.
COMMODITY_MAP: dict[str, AmsCommodity] = {
    "kale": AmsCommodity("Greens, Kale", ATLANTA_VEG_REPORT_ID),
    "romaine": AmsCommodity("Lettuce, Romaine", ATLANTA_VEG_REPORT_ID),
    "chopped romaine": AmsCommodity("Lettuce, Romaine", ATLANTA_VEG_REPORT_ID),
    "spinach": AmsCommodity("Spinach", ATLANTA_VEG_REPORT_ID),
    "baby spinach": AmsCommodity("Spinach", ATLANTA_VEG_REPORT_ID),
    "tomato": AmsCommodity("Tomatoes", ATLANTA_VEG_REPORT_ID),
    "tomatoes": AmsCommodity("Tomatoes", ATLANTA_VEG_REPORT_ID),
    "cucumber": AmsCommodity("Cucumbers", ATLANTA_VEG_REPORT_ID),
    "cucumbers": AmsCommodity("Cucumbers", ATLANTA_VEG_REPORT_ID),
    "onion": AmsCommodity("Onions, Dry", ATLANTA_VEG_REPORT_ID),
    "red onion": AmsCommodity("Onions, Dry", ATLANTA_VEG_REPORT_ID),
    "avocado": AmsCommodity("Avocados", ATLANTA_VEG_REPORT_ID),
    "broccoli": AmsCommodity("Broccoli", ATLANTA_VEG_REPORT_ID),
    "sweet potato": AmsCommodity("Sweet Potatoes", ATLANTA_VEG_REPORT_ID),
    "sweet potatoes": AmsCommodity("Sweet Potatoes", ATLANTA_VEG_REPORT_ID),
    "carrot": AmsCommodity("Carrots", ATLANTA_VEG_REPORT_ID),
    "carrots": AmsCommodity("Carrots", ATLANTA_VEG_REPORT_ID),
    "cabbage": AmsCommodity("Cabbage", ATLANTA_VEG_REPORT_ID),
    "bell pepper": AmsCommodity("Peppers (Bell Type)", ATLANTA_VEG_REPORT_ID),
    "bell peppers": AmsCommodity("Peppers (Bell Type)", ATLANTA_VEG_REPORT_ID),
    "cilantro": AmsCommodity("Cilantro", ATLANTA_VEG_REPORT_ID),
    "lemon": AmsCommodity("Lemons", ATLANTA_FRUIT_REPORT_ID),
    "lemons": AmsCommodity("Lemons", ATLANTA_FRUIT_REPORT_ID),
    "lime": AmsCommodity("Limes", ATLANTA_FRUIT_REPORT_ID),
    "limes": AmsCommodity("Limes", ATLANTA_FRUIT_REPORT_ID),
}


def lookup_commodity(normalized_name: str) -> AmsCommodity | None:
    """Look up a commodity by exact normalized name, then by token containment.

    Handles cases like 'organic kale', 'roasted sweet potato', 'cherry tomatoes'
    by checking if any mapped key appears as a token in the input.
    """
    if normalized_name in COMMODITY_MAP:
        return COMMODITY_MAP[normalized_name]
    tokens = set(normalized_name.split())
    for key, commodity in COMMODITY_MAP.items():
        if " " in key:
            if key in normalized_name:
                return commodity
        elif key in tokens:
            return commodity
    return None
