"""empty message

Revision ID: 2cb1d9a090f2
Revises: 146814e225dd
Create Date: 2020-06-30 13:24:25.473497

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2cb1d9a090f2'
down_revision = '146814e225dd'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('finalization_request',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('sender_creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('transfer_id', sa.BigInteger(), nullable=False),
    sa.Column('coordinator_type', sa.String(length=30), nullable=False),
    sa.Column('coordinator_id', sa.BigInteger(), nullable=False),
    sa.Column('coordinator_request_id', sa.BigInteger(), nullable=False),
    sa.Column('committed_amount', sa.BigInteger(), nullable=False),
    sa.Column('transfer_note', sa.TEXT(), nullable=False),
    sa.Column('finalization_flags', sa.Integer(), nullable=False),
    sa.Column('min_interest_rate', sa.REAL(), nullable=False),
    sa.Column('ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint('committed_amount >= 0'),
    sa.CheckConstraint('min_interest_rate >= -100.0'),
    sa.PrimaryKeyConstraint('debtor_id', 'sender_creditor_id', 'transfer_id'),
    comment='Represents a request to finalize a prepared transfer. Requests are queued to the `finalization_request` table, before being processed, because this allows many requests from one sender to be processed at once, reducing the lock contention on `account` table rows.'
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('finalization_request')
    # ### end Alembic commands ###
