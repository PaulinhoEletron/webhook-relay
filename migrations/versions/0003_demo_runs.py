"""Isolated demo runs and worker heartbeat."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "demo_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "event_id",
            sa.Uuid(),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("scenario", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipts", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "scenario IN ('success','temporary_failure','permanent_failure')",
            name="ck_demo_scenario",
        ),
    )
    op.create_table(
        "worker_heartbeat",
        sa.Column("worker_id", sa.String(64), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
    )


def downgrade():
    op.drop_table("worker_heartbeat")
    op.drop_table("demo_runs")
