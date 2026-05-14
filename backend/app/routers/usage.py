from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.llm_usage import LlmUsage
from app.schemas.usage import UsageByStage, UsageRollup

router = APIRouter(prefix="/api/usage", tags=["usage"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=UsageRollup)
async def usage(session: SessionDep) -> UsageRollup:
    stmt = (
        select(
            LlmUsage.stage,
            func.count().label("calls"),
            func.coalesce(func.sum(LlmUsage.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(LlmUsage.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(LlmUsage.cost_usd), Decimal("0")).label("cost_usd"),
        )
        .group_by(LlmUsage.stage)
        .order_by(LlmUsage.stage)
    )
    rows = (await session.execute(stmt)).all()

    by_stage = [
        UsageByStage(
            stage=row.stage,
            calls=row.calls,
            input_tokens=int(row.input_tokens),
            output_tokens=int(row.output_tokens),
            cost_usd=Decimal(row.cost_usd),
        )
        for row in rows
    ]
    return UsageRollup(
        total_calls=sum(s.calls for s in by_stage),
        total_input_tokens=sum(s.input_tokens for s in by_stage),
        total_output_tokens=sum(s.output_tokens for s in by_stage),
        total_cost_usd=sum((s.cost_usd for s in by_stage), Decimal("0")),
        by_stage=by_stage,
    )
