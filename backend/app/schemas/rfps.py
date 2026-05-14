from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SendRfpsRequest(BaseModel):
    distributor_limit: int = Field(default=5, ge=1, le=20)
    min_matches: int = Field(default=2, ge=1, le=20)
    deadline_days: int = Field(default=5, ge=1, le=30)


class DistributorOutcomeOut(BaseModel):
    distributor_id: int
    distributor_name: str
    matched_ingredient_count: int
    ingredients_emailed: int
    status: str
    message_id: str | None = None
    resend_id: str | None = None
    recipient_actual: str | None = None
    recipient_nominal: str | None = None
    error: str | None = None


class SendRfpsResponse(BaseModel):
    rfp_request_id: int
    deadline: str
    distributors_targeted: int
    emails_sent: int
    emails_failed: int
    items_count: int
    unassigned_ingredients: list[str] = []
    breakdown: list[DistributorOutcomeOut]


class RfpItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ingredient_id: int
    ingredient_name: str | None = None
    normalized_name: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None


class RfpEmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    distributor_id: int
    distributor_name: str | None = None
    direction: str
    subject: str | None = None
    body: str | None = None
    message_id: str | None = None
    in_reply_to: str | None = None
    status: str
    sent_at: datetime | None = None
    received_at: datetime | None = None
    recipient_actual: str | None = None
    recipient_nominal: str | None = None
    resend_id: str | None = None


class RfpRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    restaurant_id: int
    status: str
    deadline: datetime | None = None
    created_at: datetime | None = None
    items: list[RfpItemOut]
    emails: list[RfpEmailOut]


class RfpRequestSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    restaurant_id: int
    status: str
    deadline: datetime | None = None
    created_at: datetime | None = None
    items_count: int
    emails_count: int
    emails_sent: int
    emails_failed: int


# ---------------------------------------------------------------------------
# Phase 6 schemas
# ---------------------------------------------------------------------------


class PollInboxRequest(BaseModel):
    force_recommendation: bool = Field(
        default=False,
        description=(
            "Compute a recommendation even if not all distributors have "
            "replied and the deadline hasn't passed. Useful for the demo."
        ),
    )


class FollowupOut(BaseModel):
    sent: bool
    skipped_reason: str | None = None
    rfp_email_id: int | None = None
    message_id: str | None = None
    resend_id: str | None = None
    missing_fields_asked: list[str] = []


class ParsedQuotesOut(BaseModel):
    rfp_email_id: int
    rfp_request_id: int | None = None
    distributor_id: int | None = None
    quotes_inserted: int
    off_topic: bool
    overall_parse_confidence: float | None = None
    note: str | None = None
    unmatched_ingredient_names: list[str] = []


class PollInboxResponse(BaseModel):
    rfp_request_id: int
    inbound_count: int
    attributed_count: int
    unattributed_count: int
    duplicate_uids_skipped: int
    persisted_email_ids: list[int]
    poll_error: str | None = None
    parse_results: list[ParsedQuotesOut]
    parse_failed_email_ids: list[int]
    followups: list[FollowupOut]
    recommendation_ready: bool
    recommendation_not_ready_reason: str | None = None
    pick_distributor_id: int | None = None
    pick_score: float | None = None


class ComponentScoreOut(BaseModel):
    name: str
    raw_value: float | None = None
    normalized: float
    null_imputed: bool
    note: str | None = None


class DistributorRecommendationOut(BaseModel):
    distributor_id: int
    distributor_name: str
    score: float
    coverage_pct: Decimal
    quoted_ingredient_count: int
    requested_ingredient_count: int
    incomplete_comparison: bool
    components: list[ComponentScoreOut]
    rationale: str
    excluded_for_cost: list[str] = []


class RecommendationResponse(BaseModel):
    rfp_request_id: int
    ready: bool
    deadline_passed: bool
    all_replied: bool
    pick: DistributorRecommendationOut | None = None
    ranked: list[DistributorRecommendationOut] = []
    not_ready_reason: str | None = None


class QuoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ingredient_id: int
    ingredient_name: str | None = None
    unit_price: Decimal | None = None
    unit: str | None = None
    min_order_qty: Decimal | None = None
    delivery_days: int | None = None
    terms: str | None = None
    parse_confidence: float | None = None
    missing_fields: list[str] = []
    source_email_id: int | None = None


class DistributorQuotesOut(BaseModel):
    distributor_id: int
    distributor_name: str
    quotes: list[QuoteOut]


class QuotesGroupedResponse(BaseModel):
    rfp_request_id: int
    by_distributor: list[DistributorQuotesOut]


class ComparisonCell(BaseModel):
    distributor_id: int | None
    unit_price: Decimal | None
    unit: str | None
    min_order_qty: Decimal | None
    delivery_days: int | None
    missing_fields: list[str] = []


class ComparisonRow(BaseModel):
    ingredient_id: int
    ingredient_name: str
    requested_quantity: Decimal | None = None
    requested_unit: str | None = None
    cells: dict[int, ComparisonCell]


class ComparisonResponse(BaseModel):
    rfp_request_id: int
    distributors: list[dict[str, Any]]
    rows: list[ComparisonRow]
