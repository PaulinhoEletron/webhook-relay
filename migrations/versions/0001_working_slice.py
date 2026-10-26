"""Persist events, deliveries and attempts for the first working slice."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("outbound_body", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("scope_id", "idempotency_key", name="uq_event_key"),
    )
    op.create_table(
        "deliveries",
        sa.Column(
            "event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts_used", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','retry_wait','delivered','failed')",
            name="ck_delivery_status",
        ),
        sa.CheckConstraint("attempts_used BETWEEN 0 AND 5", name="ck_attempt_budget"),
    )
    op.create_index("ix_delivery_due", "deliveries", ["status", "next_attempt_at"])
    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "event_id", sa.Uuid(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.Uuid(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.UniqueConstraint("event_id", "attempt_number", name="uq_event_attempt"),
        sa.CheckConstraint("attempt_number BETWEEN 1 AND 5", name="ck_attempt_number"),
        sa.CheckConstraint(
            "outcome IN ('in_progress','delivered','failed','unknown')", name="ck_attempt_outcome"
        ),
    )


def downgrade():
    op.drop_table("delivery_attempts")
    op.drop_index("ix_delivery_due", table_name="deliveries")
    op.drop_table("deliveries")
    op.drop_table("events")
