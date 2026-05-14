"""USDA FoodData Central matcher.

Algorithm:
  1. POST /foods/search with the ingredient name, excluding Branded (too noisy).
  2. If the top hit is a clear winner (high absolute score OR much higher than
     #2), accept it without an LLM call.
  3. Otherwise hand the top 5 to Claude via the `pick_fdc_match` tool — Claude
     either picks one or returns null. The Claude call is logged to `llm_usage`
     under stage='usda_match'.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.config import settings
from app.llm import MODEL_ID
from app.llm.client import get_client
from app.llm.tools import PICK_FDC_MATCH
from app.llm.usage import traced_call
from app.utils.http_retry import request_with_retry

log = structlog.get_logger("usda_fdc")

STAGE_NAME = "usda_match"
FDC_BASE = "https://api.nal.usda.gov/fdc/v1"
FDC_SEARCH_DATA_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)"]
SEARCH_PAGE_SIZE = 5

# Disambiguation thresholds. If top_score >= ABSOLUTE_CONFIDENT or
# top_score / second_score >= RATIO_CONFIDENT, accept the top hit
# without invoking Claude.
ABSOLUTE_CONFIDENT = 200.0
RATIO_CONFIDENT = 1.5


@dataclass
class FdcCandidate:
    fdc_id: int
    description: str
    food_category: str | None
    score: float
    data_type: str | None

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "fdc_id": self.fdc_id,
            "description": self.description,
            "food_category": self.food_category,
            "score": self.score,
            "data_type": self.data_type,
        }


@dataclass
class FdcMatch:
    fdc_id: int
    description: str
    food_category: str | None
    chosen_by: str  # "score" | "claude"
    rationale: str | None = None


def _parse_hit(hit: dict[str, Any]) -> FdcCandidate | None:
    fdc_id = hit.get("fdcId")
    if not isinstance(fdc_id, int):
        return None
    return FdcCandidate(
        fdc_id=fdc_id,
        description=hit.get("description", ""),
        food_category=hit.get("foodCategory"),
        score=float(hit.get("score", 0.0)),
        data_type=hit.get("dataType"),
    )


async def search_fdc(
    client: httpx.AsyncClient, query: str, *, api_key: str | None = None
) -> list[FdcCandidate]:
    key = api_key or settings.usda_fdc_api_key or "DEMO_KEY"
    response = await request_with_retry(
        client,
        "POST",
        f"{FDC_BASE}/foods/search?api_key={key}",
        json={
            "query": query,
            "dataType": FDC_SEARCH_DATA_TYPES,
            "pageSize": SEARCH_PAGE_SIZE,
            "sortBy": "score",
            "sortOrder": "desc",
        },
        timeout=15.0,
        label=f"fdc.search[{query}]",
    )
    if response is None or response.status_code >= 400:
        log.warning(
            "fdc.search.failed",
            query=query,
            status=response.status_code if response else None,
        )
        return []
    payload = response.json()
    candidates: list[FdcCandidate] = []
    for hit in payload.get("foods", []):
        c = _parse_hit(hit)
        if c is not None:
            candidates.append(c)
    return candidates


def _confident_top(candidates: list[FdcCandidate]) -> FdcCandidate | None:
    """Apply the disambiguation thresholds. Returns the winner or None."""
    if not candidates:
        return None
    top = candidates[0]
    if top.score >= ABSOLUTE_CONFIDENT:
        return top
    if len(candidates) == 1:
        return top
    second = candidates[1]
    if second.score <= 0:
        return top
    if top.score / second.score >= RATIO_CONFIDENT:
        return top
    return None


async def _ask_claude(ingredient_name: str, candidates: list[FdcCandidate]) -> FdcMatch | None:
    client = get_client()
    async with traced_call(STAGE_NAME) as call:
        response = await client.messages.create(
            model=MODEL_ID,
            max_tokens=1024,
            tools=[PICK_FDC_MATCH],
            tool_choice={"type": "tool", "name": "pick_fdc_match"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Menu ingredient: {ingredient_name!r}\n\n"
                        "Candidates:\n"
                        + json.dumps(
                            [c.to_prompt_dict() for c in candidates],
                            indent=2,
                        )
                    ),
                }
            ],
        )
        call.bind(response)

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None or not isinstance(tool_use.input, dict):
        log.error("fdc.claude.no_tool_use", ingredient=ingredient_name)
        return None
    chosen_id = tool_use.input.get("fdc_id")
    rationale = tool_use.input.get("rationale")
    if chosen_id is None:
        log.info("fdc.claude.no_match", ingredient=ingredient_name, rationale=rationale)
        return None
    chosen = next((c for c in candidates if c.fdc_id == chosen_id), None)
    if chosen is None:
        log.warning("fdc.claude.chose_unknown_id", ingredient=ingredient_name, fdc_id=chosen_id)
        return None
    return FdcMatch(
        fdc_id=chosen.fdc_id,
        description=chosen.description,
        food_category=chosen.food_category,
        chosen_by="claude",
        rationale=rationale,
    )


async def match_ingredient(client: httpx.AsyncClient, ingredient_name: str) -> FdcMatch | None:
    """Best-effort match for one ingredient. Returns None when no credible match."""
    candidates = await search_fdc(client, ingredient_name)
    if not candidates:
        log.info("fdc.match.no_candidates", ingredient=ingredient_name)
        return None

    confident = _confident_top(candidates)
    if confident is not None:
        log.info(
            "fdc.match.confident",
            ingredient=ingredient_name,
            fdc_id=confident.fdc_id,
            score=confident.score,
        )
        return FdcMatch(
            fdc_id=confident.fdc_id,
            description=confident.description,
            food_category=confident.food_category,
            chosen_by="score",
        )

    log.info(
        "fdc.match.ambiguous",
        ingredient=ingredient_name,
        top_score=candidates[0].score,
        second_score=candidates[1].score if len(candidates) > 1 else None,
    )
    return await _ask_claude(ingredient_name, candidates)
