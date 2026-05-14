from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.dish import Dish
from app.models.dish_ingredient import DishIngredient
from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.restaurant import Restaurant
from app.schemas.distributors import DistributorOut, ScoredDistributorOut
from app.services.distributor_matching import score_distributors

router = APIRouter(prefix="/api", tags=["distributors"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/distributors", response_model=list[DistributorOut])
async def list_distributors(session: SessionDep) -> list[Distributor]:
    rows = (await session.execute(select(Distributor).order_by(Distributor.id))).scalars().all()
    return list(rows)


@router.get(
    "/restaurants/{restaurant_id}/distributors",
    response_model=list[ScoredDistributorOut],
)
async def scored_distributors(
    restaurant_id: int, session: SessionDep
) -> list[ScoredDistributorOut]:
    restaurant = await session.get(Restaurant, restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail=f"restaurant {restaurant_id} not found")

    ingredient_ids_stmt = (
        select(Ingredient)
        .join(DishIngredient, DishIngredient.ingredient_id == Ingredient.id)
        .join(Dish, Dish.id == DishIngredient.dish_id)
        .where(Dish.restaurant_id == restaurant_id)
        .distinct()
    )
    ingredients = (await session.execute(ingredient_ids_stmt)).scalars().all()
    distributors = (await session.execute(select(Distributor))).scalars().all()

    scored = score_distributors(
        ingredients=list(ingredients),
        distributors=list(distributors),
        restaurant=restaurant,
    )
    return [
        ScoredDistributorOut(
            distributor_id=s.distributor_id,
            name=s.name,
            specialties=s.specialties,
            source=s.source,
            matched_ingredient_count=s.matched_ingredient_count,
            total_ingredients=s.total_ingredients,
            match_pct=s.match_pct,
            sample_matched_ingredients=s.sample_matched_ingredients,
            distance_km=s.distance_km,
        )
        for s in scored
    ]
