from datetime import datetime
from decimal import Decimal

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
