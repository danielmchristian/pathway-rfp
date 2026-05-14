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
from app.models.quote import Quote
from app.models.restaurant import Restaurant
from app.models.rfp import EmailStatus, RfpEmail, RfpRequest, RfpRequestItem
from app.schemas.rfps import (
    ComparisonCell,
    ComparisonResponse,
    ComparisonRow,
    ComponentScoreOut,
    DistributorOutcomeOut,
    DistributorQuotesOut,
    DistributorRecommendationOut,
    FollowupOut,
    ParsedQuotesOut,
    PollInboxRequest,
    PollInboxResponse,
    QuoteOut,
    QuotesGroupedResponse,
    RecommendationResponse,
    RfpEmailOut,
    RfpItemOut,
    RfpRequestOut,
    RfpRequestSummaryOut,
    SendRfpsRequest,
    SendRfpsResponse,
)
from app.services.quote_pipeline import poll_and_process
from app.services.recommender import compute_for_rfp
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


# ---------------------------------------------------------------------------
# Phase 6 — inbox poll, quotes, comparison, recommendation
# ---------------------------------------------------------------------------


@view_router.post("/{rfp_request_id}/poll_inbox", response_model=PollInboxResponse)
async def poll_inbox_endpoint(
    rfp_request_id: int,
    body: PollInboxRequest,
    session: SessionDep,
) -> PollInboxResponse:
    rfp = await session.get(RfpRequest, rfp_request_id)
    if rfp is None:
        raise HTTPException(status_code=404, detail=f"rfp_request {rfp_request_id} not found")
    try:
        result = await poll_and_process(
            restaurant_id=rfp.restaurant_id,
            rfp_request_id=rfp_request_id,
            force_recommendation=body.force_recommendation,
        )
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rec = result.recommendation
    return PollInboxResponse(
        rfp_request_id=rfp_request_id,
        inbound_count=result.poll.inbound_count,
        attributed_count=result.poll.attributed_count,
        unattributed_count=result.poll.unattributed_count,
        duplicate_uids_skipped=result.poll.duplicate_uids_skipped,
        persisted_email_ids=result.poll.persisted_email_ids,
        poll_error=result.poll.error,
        parse_results=[ParsedQuotesOut(**p.to_dict()) for p in result.parse_results],
        parse_failed_email_ids=result.parse_failed_email_ids,
        followups=[FollowupOut(**f.to_dict()) for f in result.followups],
        recommendation_ready=bool(rec and rec.ready),
        recommendation_not_ready_reason=(rec.not_ready_reason if rec else None),
        pick_distributor_id=(rec.pick.distributor_id if rec and rec.pick else None),
        pick_score=(rec.pick.score if rec and rec.pick else None),
    )


@view_router.post("/{rfp_request_id}/finalize", response_model=RecommendationResponse)
async def finalize_recommendation(
    rfp_request_id: int, session: SessionDep
) -> RecommendationResponse:
    """Force-compute the recommendation regardless of deadline / replies."""
    rfp = await session.get(RfpRequest, rfp_request_id)
    if rfp is None:
        raise HTTPException(status_code=404, detail=f"rfp_request {rfp_request_id} not found")
    rec = await compute_for_rfp(rfp_request_id, force=True)
    return _recommendation_to_response(rec)


@view_router.get(
    "/{rfp_request_id}/recommendation", response_model=RecommendationResponse
)
async def get_recommendation(
    rfp_request_id: int, session: SessionDep
) -> RecommendationResponse:
    rfp = await session.get(RfpRequest, rfp_request_id)
    if rfp is None:
        raise HTTPException(status_code=404, detail=f"rfp_request {rfp_request_id} not found")
    rec = await compute_for_rfp(rfp_request_id, force=False)
    return _recommendation_to_response(rec)


@view_router.get("/{rfp_request_id}/quotes", response_model=QuotesGroupedResponse)
async def list_quotes(rfp_request_id: int, session: SessionDep) -> QuotesGroupedResponse:
    rfp = await session.get(RfpRequest, rfp_request_id)
    if rfp is None:
        raise HTTPException(status_code=404, detail=f"rfp_request {rfp_request_id} not found")
    quotes = (
        (
            await session.execute(
                select(Quote).where(Quote.rfp_request_id == rfp_request_id)
            )
        )
        .scalars()
        .all()
    )
    if not quotes:
        return QuotesGroupedResponse(rfp_request_id=rfp_request_id, by_distributor=[])

    ingredient_ids = {q.ingredient_id for q in quotes}
    distributor_ids = {q.distributor_id for q in quotes}
    ings = {
        i.id: i.name
        for i in (
            await session.execute(
                select(Ingredient).where(Ingredient.id.in_(ingredient_ids))
            )
        ).scalars()
    }
    dists = {
        d.id: d.name
        for d in (
            await session.execute(
                select(Distributor).where(Distributor.id.in_(distributor_ids))
            )
        ).scalars()
    }
    by_dist: dict[int, list[Quote]] = {}
    for q in quotes:
        by_dist.setdefault(q.distributor_id, []).append(q)
    out: list[DistributorQuotesOut] = []
    for did, qs in by_dist.items():
        out.append(
            DistributorQuotesOut(
                distributor_id=did,
                distributor_name=dists.get(did, f"distributor {did}"),
                quotes=[
                    QuoteOut(
                        id=q.id,
                        ingredient_id=q.ingredient_id,
                        ingredient_name=ings.get(q.ingredient_id),
                        unit_price=q.unit_price,
                        unit=q.unit,
                        min_order_qty=q.min_order_qty,
                        delivery_days=q.delivery_days,
                        terms=q.terms,
                        parse_confidence=q.parse_confidence,
                        missing_fields=list(q.missing_fields or []),
                        source_email_id=q.source_email_id,
                    )
                    for q in qs
                ],
            )
        )
    return QuotesGroupedResponse(rfp_request_id=rfp_request_id, by_distributor=out)


@view_router.get("/{rfp_request_id}/comparison", response_model=ComparisonResponse)
async def get_comparison(
    rfp_request_id: int, session: SessionDep
) -> ComparisonResponse:
    rfp = await session.get(RfpRequest, rfp_request_id)
    if rfp is None:
        raise HTTPException(status_code=404, detail=f"rfp_request {rfp_request_id} not found")

    items = (
        await session.execute(
            select(RfpRequestItem).where(RfpRequestItem.rfp_request_id == rfp_request_id)
        )
    ).scalars().all()
    items_by_ing = {i.ingredient_id: i for i in items}
    ing_ids = list(items_by_ing.keys())
    ings: dict[int, Ingredient] = {
        i.id: i
        for i in (
            await session.execute(
                select(Ingredient).where(Ingredient.id.in_(ing_ids))
            )
        ).scalars()
    }

    quotes = (
        (
            await session.execute(
                select(Quote).where(Quote.rfp_request_id == rfp_request_id)
            )
        )
        .scalars()
        .all()
    )
    distributor_ids: set[int] = set()
    by_pair: dict[tuple[int, int], Quote] = {}
    for q in quotes:
        distributor_ids.add(q.distributor_id)
        by_pair[(q.ingredient_id, q.distributor_id)] = q

    dists = {
        d.id: d
        for d in (
            await session.execute(
                select(Distributor).where(Distributor.id.in_(distributor_ids))
            )
        ).scalars()
    }
    distributors_out = [{"id": d.id, "name": d.name} for d in dists.values()]

    rows: list[ComparisonRow] = []
    for ing_id in ing_ids:
        ing = ings.get(ing_id)
        item = items_by_ing[ing_id]
        cells: dict[int, ComparisonCell] = {}
        for did in dists:
            q = by_pair.get((ing_id, did))
            if q is None:
                cells[did] = ComparisonCell(
                    distributor_id=did,
                    unit_price=None,
                    unit=None,
                    min_order_qty=None,
                    delivery_days=None,
                    missing_fields=["no_quote"],
                )
                continue
            cells[did] = ComparisonCell(
                distributor_id=did,
                unit_price=q.unit_price,
                unit=q.unit,
                min_order_qty=q.min_order_qty,
                delivery_days=q.delivery_days,
                missing_fields=list(q.missing_fields or []),
            )
        rows.append(
            ComparisonRow(
                ingredient_id=ing_id,
                ingredient_name=ing.name if ing else f"ingredient {ing_id}",
                requested_quantity=item.quantity,
                requested_unit=item.unit,
                cells=cells,
            )
        )

    return ComparisonResponse(
        rfp_request_id=rfp_request_id,
        distributors=distributors_out,
        rows=rows,
    )


def _recommendation_to_response(rec) -> RecommendationResponse:  # type: ignore[no-untyped-def]
    def _conv(d) -> DistributorRecommendationOut:
        return DistributorRecommendationOut(
            distributor_id=d.distributor_id,
            distributor_name=d.distributor_name,
            score=d.score,
            coverage_pct=d.coverage_pct,
            quoted_ingredient_count=d.quoted_ingredient_count,
            requested_ingredient_count=d.requested_ingredient_count,
            incomplete_comparison=d.incomplete_comparison,
            components=[ComponentScoreOut(**c.to_dict()) for c in d.components],
            rationale=d.rationale,
            excluded_for_cost=list(d.excluded_for_cost),
        )

    return RecommendationResponse(
        rfp_request_id=rec.rfp_request_id,
        ready=rec.ready,
        deadline_passed=rec.deadline_passed,
        all_replied=rec.all_replied,
        pick=_conv(rec.pick) if rec.pick else None,
        ranked=[_conv(r) for r in rec.ranked],
        not_ready_reason=rec.not_ready_reason,
    )


# Silence unused-import warning for future Phase 6 reuse.
_ = (func, RfpEmail)
