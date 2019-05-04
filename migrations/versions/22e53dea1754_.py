"""empty message

Revision ID: 22e53dea1754
Revises: 494204926e62
Create Date: 2019-05-04 23:14:42.048838

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '22e53dea1754'
down_revision = '494204926e62'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('account', sa.Column('concession_interest_rate', sa.REAL(), nullable=False))
    op.add_column('account', sa.Column('last_change_ts', sa.TIMESTAMP(timezone=True), nullable=False))
    op.alter_column('account', 'demurrage',
               existing_type=sa.BIGINT(),
               comment='This is the amount of negative interest accumulated on the account. Interest accumulates at an annual rate (in percents) that is equal to the maximum of the following values: `account.concession_interest_rate`, `debtor_policy.interest_rate`, `debtor_policy.interest_rate_floor`.',
               existing_comment='This is the amount of negative interest accumulated on the account. Demurrage accumulates at an annual rate (in percents) that is equal to the minimum of the following values: `account.discount_demurrage_rate`, `debtor_policy.demurrage_rate`, `debtor_policy.demurrage_rate_ceiling`.',
               existing_nullable=False)
    op.drop_column('account', 'last_transfer_ts')
    op.drop_column('account', 'discount_demurrage_rate')
    op.add_column('account_update_signal', sa.Column('concession_interest_rate', sa.REAL(), nullable=False))
    op.drop_column('account_update_signal', 'discount_demurrage_rate')
    op.add_column('debtor_accounts_policy_update_signal', sa.Column('interest_rate', sa.REAL(), nullable=False))
    op.add_column('debtor_accounts_policy_update_signal', sa.Column('interest_rate_floor', sa.REAL(), nullable=False))
    op.drop_column('debtor_accounts_policy_update_signal', 'demurrage_rate')
    op.drop_column('debtor_accounts_policy_update_signal', 'demurrage_rate_ceiling')
    op.add_column('debtor_policy', sa.Column('interest_rate', sa.REAL(), nullable=False))
    op.add_column('debtor_policy', sa.Column('interest_rate_floor', sa.REAL(), nullable=False))
    op.drop_column('debtor_policy', 'demurrage_rate')
    op.drop_column('debtor_policy', 'demurrage_rate_ceiling')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('debtor_policy', sa.Column('demurrage_rate_ceiling', sa.REAL(), autoincrement=False, nullable=False))
    op.add_column('debtor_policy', sa.Column('demurrage_rate', sa.REAL(), autoincrement=False, nullable=False))
    op.drop_column('debtor_policy', 'interest_rate_floor')
    op.drop_column('debtor_policy', 'interest_rate')
    op.add_column('debtor_accounts_policy_update_signal', sa.Column('demurrage_rate_ceiling', sa.REAL(), autoincrement=False, nullable=False))
    op.add_column('debtor_accounts_policy_update_signal', sa.Column('demurrage_rate', sa.REAL(), autoincrement=False, nullable=False))
    op.drop_column('debtor_accounts_policy_update_signal', 'interest_rate_floor')
    op.drop_column('debtor_accounts_policy_update_signal', 'interest_rate')
    op.add_column('account_update_signal', sa.Column('discount_demurrage_rate', sa.REAL(), autoincrement=False, nullable=False))
    op.drop_column('account_update_signal', 'concession_interest_rate')
    op.add_column('account', sa.Column('discount_demurrage_rate', sa.REAL(), autoincrement=False, nullable=False))
    op.add_column('account', sa.Column('last_transfer_ts', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False))
    op.alter_column('account', 'demurrage',
               existing_type=sa.BIGINT(),
               comment='This is the amount of negative interest accumulated on the account. Demurrage accumulates at an annual rate (in percents) that is equal to the minimum of the following values: `account.discount_demurrage_rate`, `debtor_policy.demurrage_rate`, `debtor_policy.demurrage_rate_ceiling`.',
               existing_comment='This is the amount of negative interest accumulated on the account. Interest accumulates at an annual rate (in percents) that is equal to the maximum of the following values: `account.concession_interest_rate`, `debtor_policy.interest_rate`, `debtor_policy.interest_rate_floor`.',
               existing_nullable=False)
    op.drop_column('account', 'last_change_ts')
    op.drop_column('account', 'concession_interest_rate')
    # ### end Alembic commands ###
