import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from app.db import get_session
from app.models.dish import Dish
from app.models.dish_ingredient import DishIngredient
from app.models.ingredient import Ingredient
from app.models.restaurant import Restaurant
from app.pipeline.events import get_bus
from app.schemas.restaurants import (
    DishOut,
    IngredientOut,
    ParseMenuRequest,
    ParseMenuResponse,
    RestaurantCreate,
    RestaurantOut,
)
from app.services.menu_parser import parse_menu

router = APIRouter(prefix="/api/restaurants", tags=["restaurants"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Where uploaded / pinned menu files live. menu_file_path is resolved
# relative to this dir to keep API callers from reading arbitrary files.
MENU_ROOT = Path(__file__).resolve().parents[3] / "data" / "menus"


@router.post("", response_model=RestaurantOut, status_code=status.HTTP_201_CREATED)
async def create_restaurant(body: RestaurantCreate, session: SessionDep) -> Restaurant:
    restaurant = Restaurant(**body.model_dump(exclude_none=True))
    session.add(restaurant)
    await session.commit()
    await session.refresh(restaurant)
    return restaurant


def _resolve_menu_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = MENU_ROOT / path.name
    path = path.resolve()
    # Path traversal guard
    if not str(path).startswith(str(MENU_ROOT.resolve())):
        raise HTTPException(status_code=400, detail=f"menu path outside {MENU_ROOT}: {raw}")
    return path


@router.post("/{restaurant_id}/parse_menu", response_model=ParseMenuResponse)
async def parse_menu_endpoint(
    restaurant_id: int,
    body: ParseMenuRequest,
    session: SessionDep,
) -> ParseMenuResponse:
    if await session.get(Restaurant, restaurant_id) is None:
        raise HTTPException(status_code=404, detail=f"restaurant {restaurant_id} not found")
    path = _resolve_menu_path(body.menu_file_path)
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"menu file not found: {path}")
    try:
        result = await parse_menu(restaurant_id=restaurant_id, menu_path=path)
    except (LookupError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ParseMenuResponse(**result.to_dict())


@router.get("/{restaurant_id}/dishes", response_model=list[DishOut])
async def list_dishes(restaurant_id: int, session: SessionDep) -> list[DishOut]:
    if await session.get(Restaurant, restaurant_id) is None:
        raise HTTPException(status_code=404, detail=f"restaurant {restaurant_id} not found")

    stmt = (
        select(Dish)
        .where(Dish.restaurant_id == restaurant_id)
        .options(selectinload(Dish.ingredients))
        .order_by(Dish.id)
    )
    dishes = (await session.execute(stmt)).scalars().all()
    if not dishes:
        return []

    ingredient_ids = {di.ingredient_id for d in dishes for di in d.ingredients}
    ingredients_by_id: dict[int, Ingredient] = {}
    if ingredient_ids:
        ing_rows = (
            (await session.execute(select(Ingredient).where(Ingredient.id.in_(ingredient_ids))))
            .scalars()
            .all()
        )
        ingredients_by_id = {i.id: i for i in ing_rows}

    out: list[DishOut] = []
    for d in dishes:
        ings: list[IngredientOut] = []
        for di in d.ingredients:
            ing = ingredients_by_id.get(di.ingredient_id)
            if ing is None:
                continue
            ings.append(
                IngredientOut(
                    id=ing.id,
                    name=ing.name,
                    normalized_name=ing.normalized_name,
                    quantity=di.quantity,
                    unit=di.unit,
                    estimation_confidence=di.estimation_confidence,
                )
            )
        out.append(
            DishOut(
                id=d.id,
                name=d.name,
                description=d.description,
                price=d.price,
                parse_confidence=d.parse_confidence,
                ingredients=ings,
            )
        )
    return out


@router.get("/{restaurant_id}/events")
async def restaurant_events(restaurant_id: int, request: Request) -> EventSourceResponse:
    bus = get_bus()

    async def event_source():
        async for evt in bus.subscribe(restaurant_id):
            if await request.is_disconnected():
                break
            yield {"event": evt.name, "data": json.dumps(evt.to_json())}

    return EventSourceResponse(event_source())


def _dish_ingredients_helper(d: Dish) -> list[DishIngredient]:
    return list(d.ingredients)
