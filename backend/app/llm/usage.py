from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from app.db import SessionLocal
from app.llm import MODEL_ID, compute_cost_usd
from app.models.llm_usage import LlmUsage

log = structlog.get_logger("llm.usage")


@dataclass
class TracedCall:
    """Captures usage data for one Claude call.

    Caller invokes `.bind(response)` once the SDK returns; on exit the row
    is written to `llm_usage` on a fresh session (independent of the caller's
    transaction, so usage is logged even on rollback).
    """

    stage: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    bound: bool = field(default=False, init=False)

    def bind(self, response: Any) -> None:
        u = getattr(response, "usage", None)
        if u is None:
            return
        self.input_tokens = getattr(u, "input_tokens", 0) or 0
        self.output_tokens = getattr(u, "output_tokens", 0) or 0
        self.cache_write_tokens = getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens = getattr(u, "cache_read_input_tokens", 0) or 0
        self.bound = True

    @property
    def cost_usd(self) -> Decimal:
        return compute_cost_usd(
            model=self.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_write_tokens=self.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens,
        )


@asynccontextmanager
async def traced_call(stage: str, model: str = MODEL_ID):
    call = TracedCall(stage=stage, model=model)
    try:
        yield call
    finally:
        async with SessionLocal() as session:
            session.add(
                LlmUsage(
                    stage=call.stage,
                    model=call.model,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    cost_usd=call.cost_usd,
                )
            )
            await session.commit()
        log.info(
            "llm.usage",
            stage=call.stage,
            model=call.model,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cost_usd=str(call.cost_usd),
            bound=call.bound,
        )
