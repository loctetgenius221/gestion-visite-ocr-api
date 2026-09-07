"""clôture automatique, pièces oubliées et branding

Revision ID: d2f4a8b91c77
Revises: f8a1c9e30b12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d2f4a8b91c77"
down_revision: str | Sequence[str] | None = "f8a1c9e30b12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    checkout_origin_enum = postgresql.ENUM(
        "MANUAL",
        "AUTOMATIC",
        name="checkout_origin",
        create_type=False,
    )
    forgotten_document_status_enum = postgresql.ENUM(
        "PENDING",
        "RESOLVED",
        "SMS_SENT",
        "SMS_FAILED",
        "NO_PHONE",
        name="forgotten_document_status",
        create_type=False,
    )
    checkout_origin_enum.create(bind, checkfirst=True)
    forgotten_document_status_enum.create(bind, checkfirst=True)

    op.add_column("organizations", sa.Column("logo_url", sa.String(length=500), nullable=True))
    op.add_column("organizations", sa.Column("primary_color", sa.String(length=20), nullable=True))
    op.add_column("organizations", sa.Column("secondary_color", sa.String(length=20), nullable=True))
    op.add_column("organizations", sa.Column("accent_color", sa.String(length=20), nullable=True))
    op.add_column("organizations", sa.Column("theme_config", sa.JSON(), nullable=True))

    op.add_column(
        "visits",
        sa.Column(
            "checkout_origin",
            checkout_origin_enum,
            nullable=True,
        ),
    )

    op.create_table(
        "forgotten_document_cases",
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("visit_id", sa.Uuid(), nullable=False),
        sa.Column("visitor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            forgotten_document_status_enum,
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("sms_status", sa.String(length=50), nullable=True),
        sa.Column("sms_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["visit_id"], ["visits.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["visitor_id"], ["visitors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forgotten_document_cases")),
    )
    op.create_index("ix_forgotten_document_cases_organization_id", "forgotten_document_cases", ["organization_id"])
    op.create_index("ix_forgotten_document_cases_visit_id", "forgotten_document_cases", ["visit_id"], unique=True)
    op.create_index("ix_forgotten_document_cases_visitor_id", "forgotten_document_cases", ["visitor_id"])
    op.create_index("ix_forgotten_document_cases_status", "forgotten_document_cases", ["status"])
    op.create_index("ix_forgotten_doc_org_status", "forgotten_document_cases", ["organization_id", "status"])

    op.create_table(
        "automatic_closure_runs",
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("closed_visits_count", sa.Integer(), nullable=False),
        sa.Column("cases_created_count", sa.Integer(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_automatic_closure_runs")),
        sa.UniqueConstraint("organization_id", "run_date", name="uq_auto_closure_org_date"),
    )
    op.create_index("ix_automatic_closure_runs_organization_id", "automatic_closure_runs", ["organization_id"])
    op.create_index("ix_automatic_closure_runs_run_date", "automatic_closure_runs", ["run_date"])


def downgrade() -> None:
    op.drop_index("ix_automatic_closure_runs_run_date", table_name="automatic_closure_runs")
    op.drop_index("ix_automatic_closure_runs_organization_id", table_name="automatic_closure_runs")
    op.drop_table("automatic_closure_runs")

    op.drop_index("ix_forgotten_doc_org_status", table_name="forgotten_document_cases")
    op.drop_index("ix_forgotten_document_cases_status", table_name="forgotten_document_cases")
    op.drop_index("ix_forgotten_document_cases_visitor_id", table_name="forgotten_document_cases")
    op.drop_index("ix_forgotten_document_cases_visit_id", table_name="forgotten_document_cases")
    op.drop_index("ix_forgotten_document_cases_organization_id", table_name="forgotten_document_cases")
    op.drop_table("forgotten_document_cases")

    with op.batch_alter_table("visits") as batch_op:
        batch_op.drop_column("checkout_origin")
    for enum_name in ("checkout_origin", "forgotten_document_status"):
        enum = sa.Enum(name=enum_name)
        enum.drop(op.get_bind(), checkfirst=True)

    for column in ("theme_config", "accent_color", "secondary_color", "primary_color", "logo_url"):
        op.drop_column("organizations", column)
