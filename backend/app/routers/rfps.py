"""Phase 5 — RFP routes.

  POST /api/restaurants/{id}/send_rfps   — kick off compose+send pipeline
  GET  /api/rfp/{rfp_request_id}         — full audit: request + items + emails
  GET  /api/restaurants/{id}/rfps        — list of RFPs ordered by created_at desc
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models.distributor import Distributor
from app.models.ingredient import Ingredient
from app.models.restaurant import Restaurant
from app.models.rfp import EmailStatus, RfpEmail, RfpRequest
from app.schemas.rfps import (
    DistributorOutcomeOut,
    RfpEmailOut,
    RfpItemOut,
    RfpRequestOut,
    RfpRequestSummaryOut,
    SendRfpsRequest,
    SendRfpsResponse,
)
from app.services.rfp_pipeline import send_rfps

SessionDep = Annotated[AsyncSession, Depends(get_session)]


send_router = APIRouter(prefix="/api/restaurants", tags=["rfps"])
view_router = APIRouter(prefix="/api/rfp", tags=["rfps"])
list_router = APIRouter(prefix="/api/restaurants", tags=["rfps"])


@send_router.post("/{restaurant_id}/send_rfps", response_model=SendRfpsResponse)
async def send_rfps_endpoint(
    restaurant_id: int,
    body: SendRfpsRequest,
    session: SessionDep,
) -> SendRfpsResponse:
    if await session.get(Restaurant, restaurant_id) is None:
        raise HTTPException(status_code=404, detail=f"restaurant {restaurant_id} not found")
    try:
        result = await send_rfps(
            restaurant_id=restaurant_id,
            distributor_limit=body.distributor_limit,
            min_matches=body.min_matches,
            deadline_days=body.deadline_days,
        )
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SendRfpsResponse(
        rfp_request_id=result.rfp_request_id,
        deadline=result.deadline,
        distributors_targeted=result.distributors_targeted,
        emails_sent=result.emails_sent,
        emails_failed=result.emails_failed,
        items_count=result.items_count,
        unassigned_ingredients=result.unassigned_ingredients,
        breakdown=[DistributorOutcomeOut(**b.to_dict()) for b in result.breakdown],
    )


@view_router.get("/{rfp_request_id}", response_model=RfpRequestOut)
async def get_rfp(rfp_request_id: int, session: SessionDep) -> RfpRequestOut:
    stmt = (
        select(RfpRequest)
        .where(RfpRequest.id == rfp_request_id)
        .options(
            selectinload(RfpRequest.items),
            selectinload(RfpRequest.emails),
        )
    )
    req = (await session.execute(stmt)).scalar_one_or_none()
    if req is None:
        raise HTTPException(status_code=404, detail=f"rfp_request {rfp_request_id} not found")

    ingredient_ids = {i.ingredient_id for i in req.items}
    ing_by_id: dict[int, Ingredient] = {}
    if ingredient_ids:
        ing_rows = (
            (
                await session.execute(
                    select(Ingredient).where(Ingredient.id.in_(ingredient_ids))
                )
            )
            .scalars()
            .all()
        )
        ing_by_id = {i.id: i for i in ing_rows}

    distributor_ids = {e.distributor_id for e in req.emails}
    dist_by_id: dict[int, Distributor] = {}
    if distributor_ids:
        dist_rows = (
            (
                await session.execute(
                    select(Distributor).where(Distributor.id.in_(distributor_ids))
                )
            )
            .scalars()
            .all()
        )
        dist_by_id = {d.id: d for d in dist_rows}

    items_out = [
        RfpItemOut(
            id=i.id,
            ingredient_id=i.ingredient_id,
            ingredient_name=(ing_by_id.get(i.ingredient_id).name if i.ingredient_id in ing_by_id else None),
            normalized_name=(
                ing_by_id.get(i.ingredient_id).normalized_name if i.ingredient_id in ing_by_id else None
            ),
            quantity=i.quantity,
            unit=i.unit,
        )
        for i in req.items
    ]
    emails_out = [
        RfpEmailOut(
            id=e.id,
            distributor_id=e.distributor_id,
            distributor_name=(dist_by_id.get(e.distributor_id).name if e.distributor_id in dist_by_id else None),
            direction=e.direction.value,
            subject=e.subject,
            body=e.body,
            message_id=e.message_id,
            in_reply_to=e.in_reply_to,
            status=e.status.value,
            sent_at=e.sent_at,
            received_at=e.received_at,
            recipient_actual=e.recipient_actual,
            recipient_nominal=e.recipient_nominal,
            resend_id=e.resend_id,
        )
        for e in req.emails
    ]
    return RfpRequestOut(
        id=req.id,
        restaurant_id=req.restaurant_id,
        status=req.status.value,
        deadline=req.deadline,
        created_at=req.created_at,
        items=items_out,
        emails=emails_out,
    )


@list_router.get("/{restaurant_id}/rfps", response_model=list[RfpRequestSummaryOut])
async def list_rfps(restaurant_id: int, session: SessionDep) -> list[RfpRequestSummaryOut]:
    if await session.get(Restaurant, restaurant_id) is None:
        raise HTTPException(status_code=404, detail=f"restaurant {restaurant_id} not found")

    stmt = (
        select(RfpRequest)
        .where(RfpRequest.restaurant_id == restaurant_id)
        .options(
            selectinload(RfpRequest.items),
            selectinload(RfpRequest.emails),
        )
        .order_by(RfpRequest.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    out: list[RfpRequestSummaryOut] = []
    for r in rows:
        sent = sum(1 for e in r.emails if e.status == EmailStatus.sent)
        failed = sum(1 for e in r.emails if e.status == EmailStatus.failed)
        out.append(
            RfpRequestSummaryOut(
                id=r.id,
                restaurant_id=r.restaurant_id,
                status=r.status.value,
                deadline=r.deadline,
                created_at=r.created_at,
                items_count=len(r.items),
                emails_count=len(r.emails),
                emails_sent=sent,
                emails_failed=failed,
            )
        )
    return out


# Silence unused-import warning for future Phase 6 reuse.
_ = (func, RfpEmail)
