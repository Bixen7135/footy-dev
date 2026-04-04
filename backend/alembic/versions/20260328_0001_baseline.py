"""baseline schema

Revision ID: 20260328_0001
Revises:
Create Date: 2026-03-28
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260328_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Baseline is managed by SQLModel metadata and startup bootstrap.
    # This revision anchors future Alembic migrations.
    pass


def downgrade() -> None:
    pass
