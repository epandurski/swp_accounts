import datetime
import dramatiq
from marshmallow import Schema, fields
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.sql.expression import null, or_
from .extensions import db, broker, MAIN_EXCHANGE_NAME

MIN_INT32 = -1 << 31
MAX_INT32 = (1 << 31) - 1
MIN_INT64 = -1 << 63
MAX_INT64 = (1 << 63) - 1
INTEREST_RATE_FLOOR = -50.0
INTEREST_RATE_CEIL = 100.0


def get_now_utc():
    return datetime.datetime.now(tz=datetime.timezone.utc)


def increment_seqnum(n):
    return MIN_INT32 if n == MAX_INT32 else n + 1


class Signal(db.Model):
    __abstract__ = True

    # TODO: Define `send_signalbus_messages` class method, set
    #      `ModelClass.signalbus_autoflush = False` and
    #      `ModelClass.signalbus_burst_count = N` in models.

    queue_name = None

    @property
    def event_name(self):  # pragma: no cover
        model = type(self)
        return f'on_{model.__tablename__}'

    def send_signalbus_message(self):  # pragma: no cover
        model = type(self)
        if model.queue_name is None:
            assert not hasattr(model, 'actor_name'), \
                'SignalModel.actor_name is set, but SignalModel.queue_name is not'
            actor_name = self.event_name
            routing_key = f'events.{actor_name}'
        else:
            actor_name = model.actor_name
            routing_key = model.queue_name
        data = model.__marshmallow_schema__.dump(self)
        message = dramatiq.Message(
            queue_name=model.queue_name,
            actor_name=actor_name,
            args=(),
            kwargs=data,
            options={},
        )
        broker.publish_message(message, exchange=MAIN_EXCHANGE_NAME, routing_key=routing_key)


class Account(db.Model):
    STATUS_DELETED_FLAG = 1
    STATUS_ESTABLISHED_INTEREST_RATE_FLAG = 2
    STATUS_OVERFLOWN_FLAG = 4
    STATUS_SCHEDULED_FOR_DELETION_FLAG = 8

    debtor_id = db.Column(db.BigInteger, primary_key=True)
    creditor_id = db.Column(db.BigInteger, primary_key=True)
    principal = db.Column(
        db.BigInteger,
        nullable=False,
        default=0,
        comment='The total owed amount. Can be negative.',
    )
    interest_rate = db.Column(
        db.REAL,
        nullable=False,
        default=0.0,
        comment='Annual rate (in percents) at which interest accumulates on the account.',
    )
    interest = db.Column(
        db.FLOAT,
        nullable=False,
        default=0.0,
        comment='The amount of interest accumulated on the account before `last_change_ts`, '
                'but not added to the `principal` yet. Can be a negative number. `interest`'
                'gets zeroed and added to the principal once in a while (like once per week).',
    )
    locked_amount = db.Column(
        db.BigInteger,
        nullable=False,
        default=0,
        comment='The total sum of all pending transfer locks for this account.',
    )
    pending_transfers_count = db.Column(
        db.Integer,
        nullable=False,
        default=0,
        comment='The number of pending transfers for this account.',
    )
    last_change_seqnum = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        comment='Incremented (with wrapping) on every change in `principal`, `interest_rate`, '
                '`interest`, or `status`.',
    )
    last_change_ts = db.Column(
        db.TIMESTAMP(timezone=True),
        nullable=False,
        default=get_now_utc,
        comment='Updated on every increment of `last_change_seqnum`. Must never decrease.',
    )
    last_outgoing_transfer_date = db.Column(
        db.DATE,
        comment='Updated on each transfer for which this account is the sender. This field is '
                'not updated on demurrage payments.',
    )
    last_transfer_id = db.Column(
        db.BigInteger,
        nullable=False,
        default=0,
        comment='Incremented when a new `prepared_transfer` record is inserted. '
                'Must never decrease.',
    )
    status = db.Column(
        db.SmallInteger,
        nullable=False,
        comment='Additional account status flags.',
    )
    interest_rate_last_change_seqnum = db.Column(
        db.Integer,
        comment='Updated on each change of the `interest_rate`.',
    )
    interest_rate_last_change_ts = db.Column(
        db.TIMESTAMP(timezone=True),
        comment='Updated on each change of the `interest_rate`.',
    )
    __table_args__ = (
        db.CheckConstraint((interest_rate >= INTEREST_RATE_FLOOR) & (interest_rate <= INTEREST_RATE_CEIL)),
        db.CheckConstraint(locked_amount >= 0),
        db.CheckConstraint(pending_transfers_count >= 0),
        db.CheckConstraint(principal > MIN_INT64),
        {
            'comment': 'Tells who owes what to whom.',
        }
    )


class TransferRequest(db.Model):
    debtor_id = db.Column(db.BigInteger, primary_key=True)
    sender_creditor_id = db.Column(db.BigInteger, primary_key=True)
    transfer_request_id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    coordinator_type = db.Column(db.String(30), nullable=False)
    coordinator_id = db.Column(db.BigInteger, nullable=False)
    coordinator_request_id = db.Column(
        db.BigInteger,
        nullable=False,
        comment='Along with `coordinator_type` and `coordinator_id` uniquely identifies the '
                'initiator of the transfer.',
    )
    min_amount = db.Column(
        db.BigInteger,
        nullable=False,
        comment='`prepared_transfer.sender_locked_amount` must be no smaller than this value.',
    )
    max_amount = db.Column(
        db.BigInteger,
        nullable=False,
        comment='`prepared_transfer.sender_locked_amount` must be no bigger than this value.',
    )
    recipient_creditor_id = db.Column(db.BigInteger, nullable=False)
    minimum_account_balance = db.Column(
        db.BigInteger,
        nullable=False,
        comment="Determines the amount that must remain available on sender's account after "
                "the requested amount has been secured.",
    )

    __table_args__ = (
        db.CheckConstraint(min_amount > 0),
        db.CheckConstraint(min_amount <= max_amount),
        {
            'comment': 'Requests to create new `prepared_transfer` records are queued to this '
                       'table. This allows multiple requests from one sender to be processed at '
                       'once, reducing the lock contention on `account` records.',
        }
    )


class PreparedTransfer(db.Model):
    debtor_id = db.Column(db.BigInteger, primary_key=True)
    sender_creditor_id = db.Column(
        db.BigInteger,
        primary_key=True,
        comment='The payer.',
    )
    transfer_id = db.Column(
        db.BigInteger,
        primary_key=True,
        comment='Along with `debtor_id` and `sender_creditor_id` uniquely identifies a transfer',
    )
    coordinator_type = db.Column(
        db.String(30),
        nullable=False,
        comment='Indicates which subsystem has initiated the transfer and is responsible for '
                'finalizing it. The value must be a valid python identifier, all lowercase, '
                'no double underscores. Example: direct, interest, circular.',
    )
    recipient_creditor_id = db.Column(
        db.BigInteger,
        nullable=False,
        comment='The payee.',
    )
    sender_locked_amount = db.Column(
        db.BigInteger,
        nullable=False,
        comment="This amount has been added to sender's `account.locked_amount`. "
                "The actual transferred (committed) amount may not exceed this number.",
    )
    prepared_at_ts = db.Column(
        db.TIMESTAMP(timezone=True),
        nullable=False,
        default=get_now_utc,
    )
    __table_args__ = (
        db.ForeignKeyConstraint(
            ['debtor_id', 'sender_creditor_id'],
            ['account.debtor_id', 'account.creditor_id'],
            ondelete='CASCADE',
        ),
        db.CheckConstraint(sender_locked_amount > 0),
        {
            'comment': 'A prepared transfer represent a guarantee that a particular transfer of '
                       'funds will be successful if ordered (committed). A record will remain in '
                       'this table until the transfer has been commited or dismissed.',
        }
    )

    sender_account = db.relationship(
        'Account',
        backref=db.backref('prepared_transfers'),
    )


class PendingChange(db.Model):
    debtor_id = db.Column(db.BigInteger, primary_key=True)
    creditor_id = db.Column(db.BigInteger, primary_key=True)
    change_id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    coordinator_type = db.Column(db.String(30), nullable=False)
    other_creditor_id = db.Column(
        db.BigInteger,
        nullable=False,
        comment='The other party in the transfer. When `principal_delta` is positive, '
                'this is the sender. When `principal_delta` is negative, this is the '
                'recipient. When `principal_delta` is zero, this is irrelevant.',
    )
    committed_at_ts = db.Column(
        db.TIMESTAMP(timezone=True),
        nullable=False,
        comment='The moment at which the transfer was committed.',
    )
    transfer_info = db.Column(
        pg.JSON,
        comment='Notes from the sender. Can be any object that the sender wants the recipient '
                'to see. Can be NULL only if `principal_delta` is zero.',
    )
    principal_delta = db.Column(
        db.BigInteger,
        nullable=False,
        comment='The change in `account.principal`.',
    )
    interest_delta = db.Column(
        db.BigInteger,
        nullable=False,
        comment='The change in `account.interest`.',
    )
    unlocked_amount = db.Column(
        db.BigInteger,
        comment='If not NULL, the value must be subtracted from `account.locked_amount`, and '
                '`account.pending_transfers_count` must be decremented.',
    )

    __table_args__ = (
        db.CheckConstraint(or_(principal_delta == 0, transfer_info != null())),
        db.CheckConstraint(unlocked_amount >= 0),
        {
            'comment': 'Changes to account record amounts are queued to this table. This '
                       'allows multiple updates to one account to coalesce, thus reducing the '
                       'lock contention.',
        }
    )


class PreparedTransferSignal(Signal):
    # These fields are taken from `PreparedTransfer`.
    debtor_id = db.Column(db.BigInteger, primary_key=True)
    sender_creditor_id = db.Column(db.BigInteger, primary_key=True)
    transfer_id = db.Column(db.BigInteger, primary_key=True)
    coordinator_type = db.Column(db.String(30), nullable=False)
    recipient_creditor_id = db.Column(db.BigInteger, nullable=False)
    sender_locked_amount = db.Column(db.BigInteger, nullable=False)
    prepared_at_ts = db.Column(db.TIMESTAMP(timezone=True), nullable=False)

    coordinator_id = db.Column(db.BigInteger, nullable=False)
    coordinator_request_id = db.Column(db.BigInteger, nullable=False)

    @property
    def event_name(self):  # pragma: no cover
        return f'on_prepared_{self.coordinator_type}_transfer_signal'


class RejectedTransferSignal(Signal):
    class __marshmallow__(Schema):
        coordinator_type = fields.String()
        coordinator_id = fields.Integer()
        coordinator_request_id = fields.Integer()
        details = fields.Raw()

    debtor_id = db.Column(db.BigInteger, primary_key=True)
    signal_id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    coordinator_type = db.Column(db.String(30), nullable=False)
    coordinator_id = db.Column(db.BigInteger, nullable=False)
    coordinator_request_id = db.Column(db.BigInteger, nullable=False)
    details = db.Column(pg.JSON, nullable=False, default={})

    @property
    def event_name(self):  # pragma: no cover
        return f'on_rejected_{self.coordinator_type}_transfer_signal'


class AccountChangeSignal(Signal):
    debtor_id = db.Column(db.BigInteger, primary_key=True)
    creditor_id = db.Column(db.BigInteger, primary_key=True)
    change_seqnum = db.Column(db.Integer, primary_key=True)
    change_ts = db.Column(db.TIMESTAMP(timezone=True), primary_key=True)
    principal = db.Column(db.BigInteger, nullable=False)
    interest = db.Column(db.FLOAT, nullable=False)
    interest_rate = db.Column(db.REAL, nullable=False)
    last_outgoing_transfer_date = db.Column(db.DATE)
    status = db.Column(db.SmallInteger, nullable=False)


class CommittedTransferSignal(Signal):
    debtor_id = db.Column(db.BigInteger, primary_key=True)
    signal_id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    coordinator_type = db.Column(db.String(30), nullable=False)
    creditor_id = db.Column(db.BigInteger, nullable=False)
    other_creditor_id = db.Column(db.BigInteger, nullable=False)
    committed_at_ts = db.Column(db.TIMESTAMP(timezone=True), nullable=False)
    committed_amount = db.Column(db.BigInteger, nullable=False)
    transfer_info = db.Column(pg.JSON, nullable=False, default={})

    __table_args__ = (
        db.CheckConstraint(committed_amount != 0),
    )
