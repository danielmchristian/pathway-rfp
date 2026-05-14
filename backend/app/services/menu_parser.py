"""Stage 1: parse a restaurant menu file into dishes + ingredients via Claude tool-use.

Idempotency contract (see docs/spec.md):
  - Claude is called OUTSIDE the persistence transaction (long-running, recoverable).
  - The tool_use response is validated up front. Malformed responses raise before
    any DELETE, so the existing menu stays intact on failure.
  - The persistence tx is short: DELETE dishes for the restaurant (cascade clears
    dish_ingredients) → INSERT new dishes, dish_ingredients → upsert ingredients
    on `normalized_name`. Ingredients are never deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from bs4 import BeautifulSoup
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.llm import MODEL_ID
from app.llm.client import get_client
from app.llm.tools import EXTRACT_MENU_ITEMS
from app.llm.usage import traced_call
from app.models.dish import Dish
from app.models.dish_ingredient import DishIngredient
from app.models.ingredient import Ingredient
from app.models.restaurant import Restaurant
from app.pipeline.events import stage

log = structlog.get_logger("menu_parser")

STAGE_NAME = "menu_parse"
# Sonnet 4.6 max output is 64K; Sweetgreen-sized menus need ~5-15K. Give headroom.
MAX_TOKENS = 16384

# Tags we strip outright before extracting text — pure boilerplate that confuses Claude.
_BOILERPLATE_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "svg", "form")
_BLANK_RUN = re.compile(r"\n{3,}")
_WHITESPACE_RUN = re.compile(r"\s+")


@dataclass
class ParseResult:
    dishes_inserted: int
    ingredients_inserted: int  # new ingredients (existing upserts don't count)
    cost_usd: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "dishes_inserted": self.dishes_inserted,
            "ingredients_inserted": self.ingredients_inserted,
            "cost_usd": str(self.cost_usd),
        }


def _normalize_ingredient(name: str) -> str:
    return _WHITESPACE_RUN.sub(" ", name.strip().lower())


def _extract_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in {".html", ".htm"}:
        soup = BeautifulSoup(raw, "lxml")
        for tag in soup(_BOILERPLATE_TAGS):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
    else:
        text = raw
    return _BLANK_RUN.sub("\n\n", text).strip()


def _validate_dishes(payload: Any) -> list[dict[str, Any]]:
    """Reject malformed tool_use responses before touching the DB."""
    if not isinstance(payload, dict) or "dishes" not in payload:
        raise ValueError("tool_use response missing 'dishes'")
    dishes = payload["dishes"]
    if not isinstance(dishes, list) or not dishes:
        raise ValueError("tool_use response has no dishes")
    for i, d in enumerate(dishes):
        if not isinstance(d, dict):
            raise ValueError(f"dish[{i}] not an object")
        if "name" not in d or not isinstance(d["name"], str) or not d["name"].strip():
            raise ValueError(f"dish[{i}] missing name")
        if "parse_confidence" not in d:
            raise ValueError(f"dish[{i}] missing parse_confidence")
        ings = d.get("ingredients") or []
        if not isinstance(ings, list):
            raise ValueError(f"dish[{i}] ingredients not a list")
        for j, ing in enumerate(ings):
            if not isinstance(ing, dict) or "name" not in ing:
                raise ValueError(f"dish[{i}].ingredients[{j}] missing name")
            if "estimation_confidence" not in ing:
                raise ValueError(f"dish[{i}].ingredients[{j}] missing estimation_confidence")
    return dishes


async def _call_claude(text: str) -> tuple[list[dict[str, Any]], Decimal]:
    client = get_client()
    async with traced_call(STAGE_NAME) as call:
        response = await client.messages.create(
            model=MODEL_ID,
            max_tokens=MAX_TOKENS,
            tools=[EXTRACT_MENU_ITEMS],
            tool_choice={"type": "tool", "name": "extract_menu_items"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Here is the cleaned text of a restaurant menu. Extract every "
                        "dish you find and call the `extract_menu_items` tool with the "
                        "result. Follow the confidence guidance in the tool description.\n\n"
                        "---\n"
                        f"{text}"
                    ),
                }
            ],
        )
        call.bind(response)

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError(
            f"Claude did not call extract_menu_items (stop_reason={response.stop_reason})"
        )
    try:
        dishes = _validate_dishes(tool_use.input)
    except ValueError as exc:
        log.error(
            "menu.tool_use.invalid",
            stop_reason=response.stop_reason,
            input_keys=list(tool_use.input.keys()) if isinstance(tool_use.input, dict) else None,
            input_preview=str(tool_use.input)[:500],
            output_tokens=getattr(response.usage, "output_tokens", None),
        )
        raise ValueError(
            f"Invalid tool_use response (stop_reason={response.stop_reason}): {exc}"
        ) from exc
    return dishes, call.cost_usd


async def _persist(
    session: AsyncSession, restaurant_id: int, dishes: list[dict[str, Any]]
) -> tuple[int, int]:
    """Idempotent upsert. Returns (dishes_inserted, new_ingredients_inserted)."""
    new_ingredient_count = 0

    # Wipe the restaurant's dishes; cascade clears dish_ingredients.
    await session.execute(delete(Dish).where(Dish.restaurant_id == restaurant_id))

    # Pre-resolve every ingredient via upsert so each dish insert can reference the id.
    normalized_to_id: dict[str, int] = {}
    seen_normalized: set[str] = set()
    for d in dishes:
        for ing in d.get("ingredients") or []:
            normalized = _normalize_ingredient(ing["name"])
            if not normalized or normalized in seen_normalized:
                continue
            seen_normalized.add(normalized)
            stmt = (
                pg_insert(Ingredient)
                .values(name=ing["name"].strip(), normalized_name=normalized)
                .on_conflict_do_update(
                    index_elements=["normalized_name"],
                    set_={"name": Ingredient.name},  # no-op; needed to RETURN existing row
                )
                .returning(Ingredient.id, Ingredient.created_at)
            )
            result = await session.execute(stmt)
            row = result.one()
            normalized_to_id[normalized] = row.id

    # Count ingredients that didn't exist before this run. Approximation: any
    # ingredient whose returned id is the max-so-far is "new". We just count
    # via a fresh COUNT before/after instead — simpler and exact.
    # (See note below for why we do it this way: the upsert RETURNING doesn't
    # reliably tell us "inserted vs updated", so we count rather than guess.)

    for d in dishes:
        dish = Dish(
            restaurant_id=restaurant_id,
            name=d["name"].strip(),
            description=(d.get("description") or "").strip() or None,
            price=Decimal(str(d["price"])) if d.get("price") is not None else None,
            raw_text=None,
            parse_confidence=float(d["parse_confidence"]),
        )
        session.add(dish)
        await session.flush()  # populate dish.id

        for ing in d.get("ingredients") or []:
            normalized = _normalize_ingredient(ing["name"])
            ingredient_id = normalized_to_id.get(normalized)
            if ingredient_id is None:
                continue
            qty = ing.get("quantity")
            session.add(
                DishIngredient(
                    dish_id=dish.id,
                    ingredient_id=ingredient_id,
                    quantity=Decimal(str(qty)) if qty is not None else None,
                    unit=(ing.get("unit") or None),
                    estimation_confidence=float(ing["estimation_confidence"]),
                )
            )

    return len(dishes), new_ingredient_count


@stage(STAGE_NAME)
async def parse_menu(*, restaurant_id: int, menu_path: Path) -> ParseResult:
    """End-to-end parse stage. Owns its DB lifecycle.

    Splits the work into three phases: (1) read+strip HTML (no DB),
    (2) call Claude on a fresh `traced_call` session, (3) open a short DB tx
    to delete+insert+upsert. Stage 2 deliberately does not hold an open tx.
    """
    if not menu_path.exists():
        raise FileNotFoundError(f"menu file not found: {menu_path}")

    # Pre-flight existence check on the restaurant (no tx held during Claude call).
    async with SessionLocal() as session:
        if await session.get(Restaurant, restaurant_id) is None:
            raise LookupError(f"restaurant {restaurant_id} not found")

    text = _extract_text(menu_path)
    log.info("menu.text.extracted", restaurant_id=restaurant_id, chars=len(text))

    dishes, cost_usd = await _call_claude(text)
    log.info("menu.claude.parsed", restaurant_id=restaurant_id, dish_count=len(dishes))

    # Short persistence tx — snapshot ingredient ids, mutate, recount, all in one tx.
    async with SessionLocal() as session, session.begin():
        pre_ids = set((await session.execute(select(Ingredient.id))).scalars().all())
        dishes_inserted, _ = await _persist(session, restaurant_id, dishes)
        await session.flush()
        post_ids = set((await session.execute(select(Ingredient.id))).scalars().all())
        new_ingredients = len(post_ids - pre_ids)

    return ParseResult(
        dishes_inserted=dishes_inserted,
        ingredients_inserted=new_ingredients,
        cost_usd=cost_usd,
    )
