"""Persist client admission identity and bounded polling windows."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "demo_runs", sa.Column("client_key", sa.String(64), nullable=False, server_default="legacy")
    )
    op.create_table(
        "demo_poll_buckets",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("demo_poll_buckets")
    op.drop_column("demo_runs", "client_key")
