"""Add administrator approval state to self-service registrations."""

from alembic import op
import sqlalchemy as sa

revision = "k1b2c3d4e5f6"
down_revision = "j2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    # Existing accounts retain their login eligibility, including disabled ones.
    op.add_column("users", sa.Column("registration_status", sa.String(16), nullable=False, server_default="approved"))
    op.add_column("users", sa.Column("registration_reviewed_by", sa.String(36), nullable=True))
    op.add_column("users", sa.Column("registration_reviewed_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("registration_review_note", sa.String(500), nullable=False, server_default=""))


def downgrade():
    op.drop_column("users", "registration_review_note")
    op.drop_column("users", "registration_reviewed_at")
    op.drop_column("users", "registration_reviewed_by")
    op.drop_column("users", "registration_status")
