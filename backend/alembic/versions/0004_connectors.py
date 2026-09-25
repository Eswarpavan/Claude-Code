"""connectors: longer SEC form names, event verification, halts, short interest, macro releases

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-25 06:00:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_ORIGINS = "origin IN ('news', 'filing', 'fda', 'trial', 'earnings')"
NEW_ORIGINS = "origin IN ('news', 'filing', 'fda', 'trial', 'earnings', 'gov')"


def upgrade() -> None:
    op.alter_column('filings', 'form_type', existing_type=sa.String(12), type_=sa.String(24))
    op.add_column('events', sa.Column('verification', sa.String(12), nullable=True))
    op.add_column('events', sa.Column('original_url', sa.Text(), nullable=True))
    op.drop_constraint('ck_events_origin', 'events', type_='check')
    op.create_check_constraint('ck_events_origin', 'events', NEW_ORIGINS)
    op.create_table(
        'trading_halts',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('symbol', sa.String(12), nullable=False, index=True),
        sa.Column('halted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reason_code', sa.String(12), nullable=True),
        sa.Column('market', sa.String(24), nullable=True),
        sa.Column('resumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.UniqueConstraint('symbol', 'halted_at', name='uq_halts_symbol_time'),
    )
    op.create_table(
        'short_interest',
        sa.Column('symbol', sa.String(12), primary_key=True),
        sa.Column('settlement_date', sa.Date(), primary_key=True),
        sa.Column('short_shares', sa.BigInteger(), nullable=False),
        sa.Column('avg_daily_volume', sa.BigInteger(), nullable=True),
        sa.Column('days_to_cover', sa.Float(), nullable=True),
        sa.Column('source', sa.String(24), nullable=False),
    )
    op.create_table(
        'macro_releases',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('source', sa.String(24), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('kind', sa.String(24), nullable=True),
        sa.Column('data', JSONB(), nullable=True),
        sa.UniqueConstraint('source', 'url', name='uq_macro_source_url'),
    )


def downgrade() -> None:
    op.drop_table('macro_releases')
    op.drop_table('short_interest')
    op.drop_table('trading_halts')
    op.execute("DELETE FROM events WHERE origin = 'gov'")
    op.drop_constraint('ck_events_origin', 'events', type_='check')
    op.create_check_constraint('ck_events_origin', 'events', OLD_ORIGINS)
    op.drop_column('events', 'original_url')
    op.drop_column('events', 'verification')
    op.alter_column('filings', 'form_type', existing_type=sa.String(24), type_=sa.String(12))
