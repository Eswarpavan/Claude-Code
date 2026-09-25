"""allow 'test' notifications (deployment check)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25 09:00:00

"""
from typing import Sequence, Union

from alembic import op

revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('ck_notifications_kind', 'notifications', type_='check')
    op.create_check_constraint('ck_notifications_kind', 'notifications',
                               "kind IN ('paper_buy', 'high_confidence', 'digest', 'test')")


def downgrade() -> None:
    op.execute("DELETE FROM notifications WHERE kind = 'test'")
    op.drop_constraint('ck_notifications_kind', 'notifications', type_='check')
    op.create_check_constraint('ck_notifications_kind', 'notifications',
                               "kind IN ('paper_buy', 'high_confidence', 'digest')")
