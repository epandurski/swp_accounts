"""empty message

Revision ID: 02002f85eb64
Revises: 
Create Date: 2019-05-06 19:06:19.852374

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '02002f85eb64'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('account_change_signal',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('change_seqnum', sa.BigInteger(), nullable=False),
    sa.Column('change_ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('balance', sa.BigInteger(), nullable=False),
    sa.Column('concession_interest_rate', sa.REAL(), nullable=False),
    sa.Column('standard_interest_rate', sa.REAL(), nullable=False),
    sa.Column('interest', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('debtor_id', 'creditor_id', 'change_seqnum')
    )
    op.create_table('committed_transfer_signal',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('prepared_transfer_seqnum', sa.BigInteger(), nullable=False),
    sa.Column('prepared_at_ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('coordinator_type', sa.String(length=30), nullable=False),
    sa.Column('sender_creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('recipient_creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('sender_locked_amount', sa.BigInteger(), nullable=False),
    sa.Column('committed_at_ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('committed_amount', sa.BigInteger(), nullable=False),
    sa.Column('transfer_info', postgresql.JSON(astext_type=sa.Text()), nullable=False),
    sa.CheckConstraint('committed_amount > 0 AND committed_amount <= amount'),
    sa.PrimaryKeyConstraint('debtor_id', 'prepared_transfer_seqnum')
    )
    op.create_table('debtor_policy',
    sa.Column('debtor_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('interest_rate', sa.REAL(), nullable=False),
    sa.Column('last_interest_rate_change_seqnum', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('debtor_id')
    )
    op.create_table('prepared_transfer_signal',
    sa.Column('coordinator_type', sa.String(length=30), nullable=False),
    sa.Column('coordinator_id', sa.BigInteger(), nullable=False),
    sa.Column('coordinator_request_id', sa.BigInteger(), nullable=False),
    sa.Column('prepared_transfer_seqnum', sa.BigInteger(), nullable=False),
    sa.Column('prepared_at_ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('coordinator_type', 'coordinator_id', 'coordinator_request_id')
    )
    op.create_table('rejected_transfer_signal',
    sa.Column('coordinator_type', sa.String(length=30), nullable=False),
    sa.Column('coordinator_id', sa.BigInteger(), nullable=False),
    sa.Column('coordinator_request_id', sa.BigInteger(), nullable=False),
    sa.Column('details', postgresql.JSON(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('coordinator_type', 'coordinator_id', 'coordinator_request_id')
    )
    op.create_table('account',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('balance', sa.BigInteger(), nullable=False, comment='The total owed amount'),
    sa.Column('concession_interest_rate', sa.REAL(), nullable=False, comment='An interest rate exclusive for this account, presumably more advantageous for the account owner than the standard one. Interest accumulates at an annual rate (in percents) that is equal to the maximum of `concession_interest_rate` and `debtor_policy.interest_rate`.'),
    sa.Column('interest', sa.BigInteger(), nullable=False, comment='The amount of interest accumulated on the account before `last_change_ts`, but not added to the `balance` yet. Can be a negative number. `interest`gets zeroed and added to the ballance one in while (like once per year).'),
    sa.Column('avl_balance', sa.BigInteger(), nullable=False, comment='The `balance` minus pending transfer locks'),
    sa.Column('last_change_seqnum', sa.BigInteger(), nullable=False, comment='Incremented on every change in `balance`, `concession_interest_rate`, or `debtor_policy.interest_rate`.'),
    sa.Column('last_change_ts', sa.TIMESTAMP(timezone=True), nullable=False, comment='Updated on every increment of `last_change_seqnum`.'),
    sa.Column('last_activity_ts', sa.TIMESTAMP(timezone=True), nullable=False, comment='Updated on every account activity. Can be used to remove stale accounts.'),
    sa.ForeignKeyConstraint(['debtor_id'], ['debtor_policy.debtor_id'], ),
    sa.PrimaryKeyConstraint('debtor_id', 'creditor_id')
    )
    op.create_table('prepared_transfer',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('prepared_transfer_seqnum', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('prepared_at_ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('coordinator_type', sa.String(length=30), nullable=False, comment='Indicates which subsystem has initiated the transfer and is responsible for finalizing it. The value must be a valid python identifier, all lowercase, no double underscores. Example: direct, circular.'),
    sa.Column('sender_creditor_id', sa.BigInteger(), nullable=False, comment='The payer'),
    sa.Column('recipient_creditor_id', sa.BigInteger(), nullable=False, comment='The payee'),
    sa.Column('amount', sa.BigInteger(), nullable=False, comment='The actual transferred (committed) amount may not exceed this number.'),
    sa.Column('sender_locked_amount', sa.BigInteger(), nullable=False, comment='This amount has been subtracted from the available account balance.'),
    sa.CheckConstraint('amount >= 0'),
    sa.CheckConstraint('sender_locked_amount >= 0'),
    sa.ForeignKeyConstraint(['debtor_id', 'sender_creditor_id'], ['account.debtor_id', 'account.creditor_id'], ),
    sa.PrimaryKeyConstraint('debtor_id', 'prepared_transfer_seqnum')
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('prepared_transfer')
    op.drop_table('account')
    op.drop_table('rejected_transfer_signal')
    op.drop_table('prepared_transfer_signal')
    op.drop_table('debtor_policy')
    op.drop_table('committed_transfer_signal')
    op.drop_table('account_change_signal')
    # ### end Alembic commands ###