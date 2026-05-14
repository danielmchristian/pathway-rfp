from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class IngredientPrice(Base, TimestampMixin):
    __tablename__ = "ingredient_prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(
        ForeignKey("ingredients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    usda_fdc_id: Mapped[int | None] = mapped_column()
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str | None] = mapped_column(String(120))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)

    # Phase 3 additions — denormalized views of raw_payload for fast filtering.
    pricing_unavailable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    ams_commodity_code: Mapped[str | None] = mapped_column(String(120))
    market_location: Mapped[str | None] = mapped_column(String(120))
    price_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit_normalized: Mapped[str | None] = mapped_column(String(40))
