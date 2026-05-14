"""ingredient_prices: pricing columns + observed_at index

Revision ID: 0002_ingredient_prices_pricing_columns
Revises: 0001_initial
Create Date: 2026-05-14

Hand-cleaned from `_autogen_0002_raw.py.txt`.
Changes vs raw:
  - File renamed to `0002_...py`; revision id set to a stable string.
  - Added partial index `ix_ingredient_prices_observed` on
    (ingredient_id, observed_at DESC) WHERE pricing_unavailable = false —
    supports the trend-read path. Autogen doesn't infer partial indexes,
    so it's added here explicitly.
  - downgrade() drops the partial index before the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_pricing_cols"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingredient_prices",
        sa.Column(
            "pricing_unavailable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "ingredient_prices",
        sa.Column("ams_commodity_code", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "ingredient_prices",
        sa.Column("market_location", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "ingredient_prices",
        sa.Column("price_per_unit", sa.Numeric(precision=12, scale=4), nullable=True),
    )
    op.add_column(
        "ingredient_prices",
        sa.Column("unit_normalized", sa.String(length=40), nullable=True),
    )

    op.execute(
        """
        CREATE INDEX ix_ingredient_prices_observed
        ON ingredient_prices (ingredient_id, observed_at DESC)
        WHERE pricing_unavailable = false
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ingredient_prices_observed")
    op.drop_column("ingredient_prices", "unit_normalized")
    op.drop_column("ingredient_prices", "price_per_unit")
    op.drop_column("ingredient_prices", "market_location")
    op.drop_column("ingredient_prices", "ams_commodity_code")
    op.drop_column("ingredient_prices", "pricing_unavailable")
