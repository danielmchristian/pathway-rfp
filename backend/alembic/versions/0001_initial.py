"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-13

Hand-cleaned from the raw autogen output in `_autogen_raw.py.txt`.
Changes vs raw:
  - File renamed to `0001_initial.py`; revision id set to `0001_initial`.
  - `email_direction` enum values fixed: `'out', 'in'` (raw emitted `'in_'`,
    leaking the Python member-name workaround into Postgres). Model now uses
    `values_callable` so future autogens stay consistent.
  - Explicit `DROP TYPE` for the three native PG enums in `downgrade()` so
    re-running migrations leaves no orphan types.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "distributors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("phone", sa.String(length=60), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("website", sa.String(length=1000), nullable=True),
        sa.Column("latitude", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("longitude", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("source", sa.String(length=60), nullable=True),
        sa.Column("specialties", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_distributors_email", "distributors", ["email"])

    op.create_table(
        "ingredients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("usda_fdc_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ingredients_normalized_name", "ingredients", ["normalized_name"], unique=True
    )

    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=60), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_usage_stage", "llm_usage", ["stage"])

    op.create_table(
        "restaurants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=80), nullable=True),
        sa.Column("zip", sa.String(length=20), nullable=True),
        sa.Column("latitude", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("longitude", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("menu_source_url", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "dishes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("parse_confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["restaurant_id"], ["restaurants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dishes_restaurant_id", "dishes", ["restaurant_id"])

    op.create_table(
        "ingredient_prices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.Integer(), nullable=False),
        sa.Column("usda_fdc_id", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("unit", sa.String(length=40), nullable=True),
        sa.Column("source", sa.String(length=120), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["ingredient_id"], ["ingredients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingredient_prices_ingredient_id", "ingredient_prices", ["ingredient_id"])

    op.create_table(
        "rfp_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("restaurant_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "sent", "partial", "closed", name="rfp_request_status"),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["restaurant_id"], ["restaurants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rfp_requests_restaurant_id", "rfp_requests", ["restaurant_id"])

    op.create_table(
        "dish_ingredients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dish_id", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("unit", sa.String(length=40), nullable=True),
        sa.Column("estimation_confidence", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["dish_id"], ["dishes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ingredient_id"], ["ingredients.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dish_ingredients_dish_id", "dish_ingredients", ["dish_id"])
    op.create_index("ix_dish_ingredients_ingredient_id", "dish_ingredients", ["ingredient_id"])

    op.create_table(
        "recommendations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rfp_request_id", sa.Integer(), nullable=False),
        sa.Column("distributor_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["distributor_id"], ["distributors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rfp_request_id"], ["rfp_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recommendations_distributor_id", "recommendations", ["distributor_id"])
    op.create_index("ix_recommendations_rfp_request_id", "recommendations", ["rfp_request_id"])

    op.create_table(
        "rfp_emails",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rfp_request_id", sa.Integer(), nullable=False),
        sa.Column("distributor_id", sa.Integer(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("out", "in", name="email_direction"),
            nullable=False,
        ),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("message_id", sa.String(length=500), nullable=True),
        sa.Column("in_reply_to", sa.String(length=500), nullable=True),
        sa.Column(
            "status",
            sa.Enum("queued", "sent", "failed", "received", name="email_status"),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["distributor_id"], ["distributors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rfp_request_id"], ["rfp_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rfp_emails_distributor_id", "rfp_emails", ["distributor_id"])
    op.create_index("ix_rfp_emails_in_reply_to", "rfp_emails", ["in_reply_to"])
    op.create_index("ix_rfp_emails_message_id", "rfp_emails", ["message_id"])
    op.create_index("ix_rfp_emails_rfp_request_id", "rfp_emails", ["rfp_request_id"])

    op.create_table(
        "rfp_request_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rfp_request_id", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("unit", sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(["ingredient_id"], ["ingredients.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rfp_request_id"], ["rfp_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rfp_request_items_ingredient_id", "rfp_request_items", ["ingredient_id"])
    op.create_index("ix_rfp_request_items_rfp_request_id", "rfp_request_items", ["rfp_request_id"])

    op.create_table(
        "quotes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rfp_request_id", sa.Integer(), nullable=False),
        sa.Column("distributor_id", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("unit", sa.String(length=40), nullable=True),
        sa.Column("min_order_qty", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("delivery_days", sa.Integer(), nullable=True),
        sa.Column("terms", sa.Text(), nullable=True),
        sa.Column("source_email_id", sa.Integer(), nullable=True),
        sa.Column("parse_confidence", sa.Float(), nullable=True),
        sa.Column("missing_fields", postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["distributor_id"], ["distributors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ingredient_id"], ["ingredients.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rfp_request_id"], ["rfp_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_email_id"], ["rfp_emails.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quotes_distributor_id", "quotes", ["distributor_id"])
    op.create_index("ix_quotes_ingredient_id", "quotes", ["ingredient_id"])
    op.create_index("ix_quotes_rfp_request_id", "quotes", ["rfp_request_id"])
    op.create_index("ix_quotes_source_email_id", "quotes", ["source_email_id"])


def downgrade() -> None:
    op.drop_index("ix_quotes_source_email_id", table_name="quotes")
    op.drop_index("ix_quotes_rfp_request_id", table_name="quotes")
    op.drop_index("ix_quotes_ingredient_id", table_name="quotes")
    op.drop_index("ix_quotes_distributor_id", table_name="quotes")
    op.drop_table("quotes")

    op.drop_index("ix_rfp_request_items_rfp_request_id", table_name="rfp_request_items")
    op.drop_index("ix_rfp_request_items_ingredient_id", table_name="rfp_request_items")
    op.drop_table("rfp_request_items")

    op.drop_index("ix_rfp_emails_rfp_request_id", table_name="rfp_emails")
    op.drop_index("ix_rfp_emails_message_id", table_name="rfp_emails")
    op.drop_index("ix_rfp_emails_in_reply_to", table_name="rfp_emails")
    op.drop_index("ix_rfp_emails_distributor_id", table_name="rfp_emails")
    op.drop_table("rfp_emails")

    op.drop_index("ix_recommendations_rfp_request_id", table_name="recommendations")
    op.drop_index("ix_recommendations_distributor_id", table_name="recommendations")
    op.drop_table("recommendations")

    op.drop_index("ix_dish_ingredients_ingredient_id", table_name="dish_ingredients")
    op.drop_index("ix_dish_ingredients_dish_id", table_name="dish_ingredients")
    op.drop_table("dish_ingredients")

    op.drop_index("ix_rfp_requests_restaurant_id", table_name="rfp_requests")
    op.drop_table("rfp_requests")

    op.drop_index("ix_ingredient_prices_ingredient_id", table_name="ingredient_prices")
    op.drop_table("ingredient_prices")

    op.drop_index("ix_dishes_restaurant_id", table_name="dishes")
    op.drop_table("dishes")

    op.drop_table("restaurants")

    op.drop_index("ix_llm_usage_stage", table_name="llm_usage")
    op.drop_table("llm_usage")

    op.drop_index("ix_ingredients_normalized_name", table_name="ingredients")
    op.drop_table("ingredients")

    op.drop_index("ix_distributors_email", table_name="distributors")
    op.drop_table("distributors")

    # Drop the three native PG enum types; autogen leaves them behind.
    op.execute("DROP TYPE IF EXISTS email_status")
    op.execute("DROP TYPE IF EXISTS email_direction")
    op.execute("DROP TYPE IF EXISTS rfp_request_status")
