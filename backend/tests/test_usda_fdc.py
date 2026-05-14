from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.services.usda_fdc import match_ingredient


def _hit(fdc_id: int, description: str, score: float, category: str = "Vegetables") -> dict:
    return {
        "fdcId": fdc_id,
        "description": description,
        "foodCategory": category,
        "score": score,
        "dataType": "Foundation",
    }


@pytest.mark.asyncio
async def test_confident_top_wins_without_claude() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=r"https://api.nal.usda.gov/fdc/v1/foods/search.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "foods": [
                        _hit(1, "Kale, raw", 800.0),
                        _hit(2, "Kale chips", 100.0),
                    ]
                },
            )
        )
        async with httpx.AsyncClient() as http:
            match = await match_ingredient(http, "kale")

    assert match is not None
    assert match.fdc_id == 1
    assert match.chosen_by == "score"


@pytest.mark.asyncio
async def test_ambiguous_calls_claude_and_picks() -> None:
    fake_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="pick_fdc_match",
                input={"fdc_id": 22, "rationale": "raw form"},
            )
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=400,
            output_tokens=80,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=fake_response))
    )

    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=r"https://api.nal.usda.gov/fdc/v1/foods/search.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "foods": [
                        _hit(11, "Tomato, red, ripe, raw", 100.0),
                        _hit(22, "Tomato, generic", 90.0),  # close to top
                    ]
                },
            )
        )
        with patch("app.services.usda_fdc.get_client", return_value=fake_client):
            async with httpx.AsyncClient() as http:
                match = await match_ingredient(http, "tomato")

    assert match is not None
    assert match.fdc_id == 22
    assert match.chosen_by == "claude"
    assert "raw" in (match.rationale or "")


@pytest.mark.asyncio
async def test_claude_returns_null_means_no_match() -> None:
    fake_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                name="pick_fdc_match",
                input={"fdc_id": None, "rationale": "none credible"},
            )
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=200,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )
    fake_client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=fake_response))
    )

    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=r"https://api.nal.usda.gov/fdc/v1/foods/search.*").mock(
            return_value=httpx.Response(
                200,
                json={"foods": [_hit(1, "irrelevant snack", 30), _hit(2, "another snack", 28)]},
            )
        )
        with patch("app.services.usda_fdc.get_client", return_value=fake_client):
            async with httpx.AsyncClient() as http:
                match = await match_ingredient(http, "chef's special blend")

    assert match is None


@pytest.mark.asyncio
async def test_empty_results_no_match() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=r"https://api.nal.usda.gov/fdc/v1/foods/search.*").mock(
            return_value=httpx.Response(200, json={"foods": []})
        )
        async with httpx.AsyncClient() as http:
            match = await match_ingredient(http, "unobtainium")
    assert match is None
