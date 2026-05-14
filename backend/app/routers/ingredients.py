from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.ingredient import Ingredient
from app.models.ingredient_price import IngredientPrice
from app.schemas.ingredients import (
    IngredientPricesOut,
    PriceObservationOut,
    PriceTrendOut,
)
from app.services.pricing_trends import PriceObservation, compute_trend

router = APIRouter(prefix="/api/ingredients", tags=["ingredients"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/{ingredient_id}/prices", response_model=IngredientPricesOut)
async def ingredient_prices(ingredient_id: int, session: SessionDep) -> IngredientPricesOut:
    ingredient = await session.get(Ingredient, ingredient_id)
    if ingredient is None:
        raise HTTPException(status_code=404, detail=f"ingredient {ingredient_id} not found")

    stmt = (
        select(IngredientPrice)
        .where(IngredientPrice.ingredient_id == ingredient_id)
        .order_by(IngredientPrice.observed_at.desc().nulls_last())
    )
    rows = (await session.execute(stmt)).scalars().all()

    trend = compute_trend(
        [
            PriceObservation(
                observed_at=r.observed_at,
                price_per_unit=r.price_per_unit,
                unit_normalized=r.unit_normalized,
            )
            for r in rows
            if r.observed_at is not None and not r.pricing_unavailable
        ]
    )

    return IngredientPricesOut(
        ingredient_id=ingredient.id,
        ingredient_name=ingredient.name,
        normalized_name=ingredient.normalized_name,
        usda_fdc_id=ingredient.usda_fdc_id,
        category=ingredient.category,
        trend=PriceTrendOut(
            latest_price=trend.latest_price,
            avg_30d=trend.avg_30d,
            delta_pct_30d=trend.delta_pct_30d,
            direction=trend.direction,
            observations_count=trend.observations_count,
        ),
        observations=[PriceObservationOut.model_validate(r) for r in rows],
    )
