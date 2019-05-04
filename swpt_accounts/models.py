import os
import struct
import datetime
import math
import dramatiq
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.dialects import postgresql as pg
from flask import current_app
from .extensions import db
from . import actors

BEGINNING_OF_TIME = datetime.datetime(datetime.MINYEAR, 1, 1, tzinfo=datetime.timezone.utc)


def get_now_utc():
    return datetime.datetime.now(tz=datetime.timezone.utc)


class Debtor(db.Model):
    debtor_id = db.Column(db.BigInteger, primary_key=True, autoincrement=False)
    demurrage_rate = db.Column(db.REAL, nullable=False, default=0.0)
    demurrage_rate_ceiling = db.Column(db.REAL, nullable=False, default=0.0)
    __table_args__ = (
        db.CheckConstraint(demurrage_rate >= 0),
        db.CheckConstraint(demurrage_rate_ceiling >= 0),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'debtor_id' not in kwargs:
            modulo = 1 << 63
            self.debtor_id = struct.unpack('>q', os.urandom(8))[0] % modulo or 1
            assert 0 < self.debtor_id < modulo


class DebtorModel(db.Model):
    __abstract__ = True

    @declared_attr
    def debtor(cls):
        return db.relationship(
            Debtor,
            primaryjoin=Debtor.debtor_id == db.foreign(cls.debtor_id),
            backref=db.backref(cls.__tablename__ + '_list'),
        )


class SignalModel(db.Model):
    __abstract__ = True

    queue_name = None

    def send_signalbus_message(self):
        model = type(self)
        if model.queue_name is None:
            assert not hasattr(model, 'actor_name'), \
                'SignalModel.queue_name is not set, but SignalModel.actor_model is set'
            exchange_name = current_app.config.get('RABBITMQ_EVENT_EXCHANGE', '')
            actor_prefix = f'on_{exchange_name}_' if exchange_name else 'on_'
            actor_name = actor_prefix + model.__tablename__
        else:
            exchange_name = ''
            actor_name = model.actor_name
        data = model.__marshmallow_schema__.dump(self)
        message = dramatiq.Message(
            queue_name=model.queue_name,
            actor_name=actor_name,
            args=(),
            kwargs=data,
            options={},
        )
        actors.broker.publish_message(message, exchange=exchange_name)


class Account(DebtorModel):
    debtor_id = db.Column(db.BigInteger, db.ForeignKey('debtor.debtor_id'), primary_key=True)
    creditor_id = db.Column(db.BigInteger, primary_key=True)
    discount_demurrage_rate = db.Column(db.REAL, nullable=False, default=math.inf)
    balance = db.Column(
        db.BigInteger,
        nullable=False,
        default=0,
        comment='The total owed amount',
    )
    demurrage = db.Column(
        db.BigInteger,
        nullable=False,
        default=0,
        comment='This is the amount of negative interest accumulated on the account. '
                'Demurrage accumulates at an annual rate (in percents) that is equal to '
                'the minimum of the following values: `account.discount_demurrage_rate`, '
                '`debtor.demurrage_rate`, `debtor.demurrage_rate_ceiling`.',
    )
    avl_balance = db.Column(
        db.BigInteger,
        nullable=False,
        default=0,
        comment='The total owed amount, minus demurrage, minus pending transfer locks',
    )
    last_transfer_ts = db.Column(db.TIMESTAMP(timezone=True), nullable=False, default=BEGINNING_OF_TIME)
    last_change_seqnum = db.Column(
        db.BigInteger,
        nullable=False,
        default=1,
        comment='This is incremented on every change. Zero indicates a deactivated account.',
    )
    __table_args__ = (
        db.CheckConstraint(demurrage >= 0),
        db.CheckConstraint(discount_demurrage_rate >= 0),
    )


class PreparedTransfer(DebtorModel):
    TYPE_CIRCULAR = 1
    TYPE_DIRECT = 2
    TYPE_THIRD_PARTY = 3

    debtor_id = db.Column(db.BigInteger, primary_key=True)
    prepared_transfer_seqnum = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    sender_creditor_id = db.Column(db.BigInteger, nullable=False)
    recipient_creditor_id = db.Column(db.BigInteger, nullable=False)
    transfer_type = db.Column(
        db.SmallInteger,
        nullable=False,
        comment=(
            f'{TYPE_CIRCULAR} -- circular transfer, '
            f'{TYPE_DIRECT} -- direct transfer, '
            f'{TYPE_THIRD_PARTY} -- third party transfer '
        ),
    )
    transfer_info = db.Column(pg.JSONB, nullable=False, default={})
    amount = db.Column(db.BigInteger, nullable=False)
    sender_locked_amount = db.Column(
        db.BigInteger,
        nullable=False,
        default=lambda context: context.get_current_parameters()['amount'],
    )
    prepared_at_ts = db.Column(db.TIMESTAMP(timezone=True), nullable=False, default=get_now_utc)
    __table_args__ = (
        db.ForeignKeyConstraint(
            ['debtor_id', 'sender_creditor_id'],
            ['account.debtor_id', 'account.creditor_id'],
        ),
        db.Index(
            'idx_prepared_transfer_sender_creditor_id',
            debtor_id,
            sender_creditor_id,
        ),
        db.CheckConstraint(amount >= 0),
        db.CheckConstraint(sender_locked_amount >= 0),
    )

    sender_account = db.relationship(
        'Account',
        backref=db.backref('prepared_transfer_list'),
    )


class TransactionSignal(SignalModel):
    debtor_id = db.Column(db.BigInteger, primary_key=True)
    prepared_transfer_seqnum = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    sender_creditor_id = db.Column(db.BigInteger, nullable=False)
    recipient_creditor_id = db.Column(db.BigInteger, nullable=False)
    amount = db.Column(db.BigInteger, nullable=False)
    transaction_info = db.Column(pg.JSONB, nullable=False, default={})
    committed_at_ts = db.Column(db.TIMESTAMP(timezone=True), nullable=False, default=get_now_utc)
