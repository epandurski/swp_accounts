"""empty message

Revision ID: b866c83f8c83
Revises: 985bd683dfb7
Create Date: 2019-05-04 23:57:16.376156

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'b866c83f8c83'
down_revision = '985bd683dfb7'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('account', sa.Column('interest', sa.BigInteger(), nullable=False, comment='The amount of interest accumulated on the account. Can be negative. Interest accumulates at an annual rate (in percents) that is equal to the maximum of the following values: `account.concession_interest_rate`, `debtor_policy.interest_rate`, `debtor_policy.interest_rate_floor`.'))
    op.alter_column('account', 'avl_balance',
               existing_type=sa.BIGINT(),
               comment='The `balance`, plus `interest`, minus pending transfer locks',
               existing_comment='The total owed amount, minus demurrage, minus pending transfer locks',
               existing_nullable=False)
    op.alter_column('account', 'last_change_ts',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment='This is updated on every change.',
               existing_nullable=False)
    op.drop_column('account', 'demurrage')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('account', sa.Column('demurrage', sa.BIGINT(), autoincrement=False, nullable=False, comment='This is the amount of negative interest accumulated on the account. Interest accumulates at an annual rate (in percents) that is equal to the maximum of the following values: `account.concession_interest_rate`, `debtor_policy.interest_rate`, `debtor_policy.interest_rate_floor`.'))
    op.alter_column('account', 'last_change_ts',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               comment=None,
               existing_comment='This is updated on every change.',
               existing_nullable=False)
    op.alter_column('account', 'avl_balance',
               existing_type=sa.BIGINT(),
               comment='The total owed amount, minus demurrage, minus pending transfer locks',
               existing_comment='The `balance`, plus `interest`, minus pending transfer locks',
               existing_nullable=False)
    op.drop_column('account', 'interest')
    # ### end Alembic commands ###