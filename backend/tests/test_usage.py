from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.llm import MODEL_ID, compute_cost_usd
from app.llm.usage import traced_call
from app.models.llm_usage import LlmUsage


def test_compute_cost_usd_sonnet_4_6() -> None:
    cost = compute_cost_usd(model=MODEL_ID, input_tokens=1000, output_tokens=500)
    expected = (Decimal("1000") * Decimal("3.00") + Decimal("500") * Decimal("15.00")) / Decimal(
        "1000000"
    )
    assert cost == expected


@pytest.mark.asyncio
async def test_traced_call_writes_llm_usage_row(db_session) -> None:
    await db_session.execute(delete(LlmUsage))
    await db_session.commit()

    fake_response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=1234,
            output_tokens=567,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
    )

    async with traced_call("menu_parse") as call:
        call.bind(fake_response)

    rows = (await db_session.execute(select(LlmUsage))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.stage == "menu_parse"
    assert row.model == MODEL_ID
    assert row.input_tokens == 1234
    assert row.output_tokens == 567
    assert row.cost_usd is not None and row.cost_usd > 0
