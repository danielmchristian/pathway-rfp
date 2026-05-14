from decimal import Decimal

from pydantic import BaseModel


class UsageByStage(BaseModel):
    stage: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class UsageRollup(BaseModel):
    total_calls: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: Decimal
    by_stage: list[UsageByStage]
