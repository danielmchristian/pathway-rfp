from decimal import Decimal

# Verified via claude-api skill on 2026-05-13. The skill is explicit that
# the model ID is the bare alias — do NOT append a date suffix.
MODEL_ID = "claude-sonnet-4-6"

# USD per 1M tokens. Cache fields are kept for future stages that reuse a
# stable prefix (USDA matcher, quote parser); Phase 2 doesn't use caching.
PRICING: dict[str, dict[str, Decimal]] = {
    "claude-sonnet-4-6": {
        "input_per_mtok": Decimal("3.00"),
        "output_per_mtok": Decimal("15.00"),
        "cache_write_5m_per_mtok": Decimal("3.75"),
        "cache_write_1h_per_mtok": Decimal("6.00"),
        "cache_read_per_mtok": Decimal("0.30"),
    },
}

ONE_MILLION = Decimal("1000000")


def compute_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> Decimal:
    rates = PRICING[model]
    return (
        (Decimal(input_tokens) * rates["input_per_mtok"])
        + (Decimal(output_tokens) * rates["output_per_mtok"])
        + (Decimal(cache_write_tokens) * rates["cache_write_5m_per_mtok"])
        + (Decimal(cache_read_tokens) * rates["cache_read_per_mtok"])
    ) / ONE_MILLION
