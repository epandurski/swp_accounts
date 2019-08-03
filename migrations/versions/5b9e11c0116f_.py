"""empty message

Revision ID: 5b9e11c0116f
Revises: 
Create Date: 2019-08-03 14:34:04.116202

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '5b9e11c0116f'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('account',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('principal', sa.BigInteger(), nullable=False, comment='The total owed amount. Can be negative.'),
    sa.Column('interest_rate', sa.REAL(), nullable=False, comment='Annual rate (in percents) at which interest accumulates on the account.'),
    sa.Column('interest', sa.FLOAT(), nullable=False, comment='The amount of interest accumulated on the account before `last_change_ts`, but not added to the `principal` yet. Can be a negative number. `interest`gets zeroed and added to the principal once in a while (like once per week).'),
    sa.Column('locked_amount', sa.BigInteger(), nullable=False, comment='The total sum of all pending transfer locks for this account.'),
    sa.Column('pending_transfers_count', sa.Integer(), nullable=False, comment='The number of pending transfers for this account.'),
    sa.Column('last_change_seqnum', sa.Integer(), nullable=False, comment='Incremented (with wrapping) on every change in `principal`, `interest_rate`, `interest`, or `status`.'),
    sa.Column('last_change_ts', sa.TIMESTAMP(timezone=True), nullable=False, comment='Updated on every increment of `last_change_seqnum`. Must never decrease.'),
    sa.Column('last_outgoing_transfer_date', sa.DATE(), nullable=True, comment='Updated on each transfer for which this account is the sender. This field is not updated on demurrage payments.'),
    sa.Column('last_transfer_id', sa.BigInteger(), nullable=False, comment='Incremented when a new `prepared_transfer` record is inserted. Must never decrease.'),
    sa.Column('status', sa.SmallInteger(), nullable=False, comment='Additional account status flags.'),
    sa.Column('attributes_last_change_seqnum', sa.Integer(), nullable=True, comment='Updated on each change of account attributes (the `interest_rate` for example), made on a request by the debtor administration subsystem.'),
    sa.Column('attributes_last_change_ts', sa.TIMESTAMP(timezone=True), nullable=True, comment='Updated on each change of account attributes (the `interest_rate` for example), made on a request by the debtor administration subsystem.'),
    sa.CheckConstraint('interest_rate > -100.0 AND interest_rate <= 100.0'),
    sa.CheckConstraint('locked_amount >= 0'),
    sa.CheckConstraint('pending_transfers_count >= 0'),
    sa.CheckConstraint('principal > -9223372036854775808'),
    sa.PrimaryKeyConstraint('debtor_id', 'creditor_id'),
    comment='Tells who owes what to whom.'
    )
    op.create_table('account_change_signal',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('change_seqnum', sa.Integer(), nullable=False),
    sa.Column('change_ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('principal', sa.BigInteger(), nullable=False),
    sa.Column('interest', sa.FLOAT(), nullable=False),
    sa.Column('interest_rate', sa.REAL(), nullable=False),
    sa.Column('last_outgoing_transfer_date', sa.DATE(), nullable=True),
    sa.Column('status', sa.SmallInteger(), nullable=False),
    sa.PrimaryKeyConstraint('debtor_id', 'creditor_id', 'change_seqnum')
    )
    op.create_table('committed_transfer_signal',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('signal_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('coordinator_type', sa.String(length=30), nullable=False),
    sa.Column('sender_creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('recipient_creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('committed_at_ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('committed_amount', sa.BigInteger(), nullable=False),
    sa.Column('transfer_info', postgresql.JSON(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('debtor_id', 'signal_id')
    )
    op.create_table('pending_change',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('change_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('principal_delta', sa.BigInteger(), nullable=False),
    sa.Column('interest_delta', sa.BigInteger(), nullable=False),
    sa.Column('unlocked_amount', sa.BigInteger(), nullable=True, comment='If not NULL, the value must be subtracted from `account.locked_amount`, and `account.pending_transfers_count` must be decremented.'),
    sa.Column('inserted_at_ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint('unlocked_amount >= 0'),
    sa.PrimaryKeyConstraint('debtor_id', 'creditor_id', 'change_id'),
    comment='Changes to account record amounts are queued to this table. This allows multiple updates to one account to coalesce, thus reducing the lock contention.'
    )
    op.create_table('prepared_transfer_signal',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('sender_creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('transfer_id', sa.BigInteger(), nullable=False),
    sa.Column('coordinator_type', sa.String(length=30), nullable=False),
    sa.Column('recipient_creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('sender_locked_amount', sa.BigInteger(), nullable=False),
    sa.Column('prepared_at_ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('coordinator_id', sa.BigInteger(), nullable=False),
    sa.Column('coordinator_request_id', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('debtor_id', 'sender_creditor_id', 'transfer_id')
    )
    op.create_table('rejected_transfer_signal',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('signal_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('coordinator_type', sa.String(length=30), nullable=False),
    sa.Column('coordinator_id', sa.BigInteger(), nullable=False),
    sa.Column('coordinator_request_id', sa.BigInteger(), nullable=False),
    sa.Column('details', postgresql.JSON(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('debtor_id', 'signal_id')
    )
    op.create_table('transfer_request',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('sender_creditor_id', sa.BigInteger(), nullable=False),
    sa.Column('transfer_request_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('coordinator_type', sa.String(length=30), nullable=False),
    sa.Column('coordinator_id', sa.BigInteger(), nullable=False),
    sa.Column('coordinator_request_id', sa.BigInteger(), nullable=False, comment='Along with `coordinator_type` and `coordinator_id` uniquely identifies the initiator of the transfer.'),
    sa.Column('min_amount', sa.BigInteger(), nullable=False, comment='`prepared_transfer.sender_locked_amount` must be no smaller than this value.'),
    sa.Column('max_amount', sa.BigInteger(), nullable=False, comment='`prepared_transfer.sender_locked_amount` must be no bigger than this value.'),
    sa.Column('recipient_creditor_id', sa.BigInteger(), nullable=False),
    sa.CheckConstraint('min_amount <= max_amount'),
    sa.CheckConstraint('min_amount > 0'),
    sa.PrimaryKeyConstraint('debtor_id', 'sender_creditor_id', 'transfer_request_id'),
    comment='Requests to create new `prepared_transfer` records are queued to this table. This allows multiple requests from one sender to be processed at once, reducing the lock contention on `account` records.'
    )
    op.create_table('prepared_transfer',
    sa.Column('debtor_id', sa.BigInteger(), nullable=False),
    sa.Column('sender_creditor_id', sa.BigInteger(), nullable=False, comment='The payer.'),
    sa.Column('transfer_id', sa.BigInteger(), nullable=False, comment='Along with `debtor_id` and `sender_creditor_id` uniquely identifies a transfer'),
    sa.Column('coordinator_type', sa.String(length=30), nullable=False, comment='Indicates which subsystem has initiated the transfer and is responsible for finalizing it. The value must be a valid python identifier, all lowercase, no double underscores. Example: direct, interest, circular.'),
    sa.Column('recipient_creditor_id', sa.BigInteger(), nullable=False, comment='The payee.'),
    sa.Column('sender_locked_amount', sa.BigInteger(), nullable=False, comment="This amount has been added to sender's `account.locked_amount`. The actual transferred (committed) amount may not exceed this number."),
    sa.Column('prepared_at_ts', sa.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint('sender_locked_amount > 0'),
    sa.ForeignKeyConstraint(['debtor_id', 'sender_creditor_id'], ['account.debtor_id', 'account.creditor_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('debtor_id', 'sender_creditor_id', 'transfer_id'),
    comment='A prepared transfer represent a guarantee that a particular transfer of funds will be successful if ordered (committed). A record will remain in this table until the transfer has been commited or dismissed.'
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table('prepared_transfer')
    op.drop_table('transfer_request')
    op.drop_table('rejected_transfer_signal')
    op.drop_table('prepared_transfer_signal')
    op.drop_table('pending_change')
    op.drop_table('committed_transfer_signal')
    op.drop_table('account_change_signal')
    op.drop_table('account')
    # ### end Alembic commands ###