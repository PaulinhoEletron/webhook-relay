"""Persist the receiver's simulated fulfillment effect with event-ID deduplication."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "fulfillment_effects",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("outbound_body", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade():
    op.drop_table("fulfillment_effects")
