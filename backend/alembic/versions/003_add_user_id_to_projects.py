"""add user_id to projects

Revision ID: 003_add_user_id_to_projects
Revises: 002_add_users
Create Date: 2026-04-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_add_user_id_to_projects"
down_revision: Union[str, None] = "002_add_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("user_id", sa.String(30), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_user_id",
        "projects",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_projects_user_id", "projects", ["user_id"])

    # Backfill: if there are existing projects with no user, they will have
    # NULL user_id. After confirming the backfill (or deleting orphan rows),
    # run a separate migration to make the column NOT NULL.


def downgrade() -> None:
    op.drop_index("ix_projects_user_id", table_name="projects")
    op.drop_constraint("fk_projects_user_id", "projects", type_="foreignkey")
    op.drop_column("projects", "user_id")
