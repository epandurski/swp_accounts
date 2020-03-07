from typing import TypeVar, Callable
from datetime import datetime, timedelta, timezone
from swpt_lib.scan_table import TableScanner
from sqlalchemy.sql.expression import tuple_
from flask import current_app
from .extensions import db
from .models import Account, AccountChangeSignal, AccountPurgeSignal, PreparedTransfer, PreparedTransferSignal

T = TypeVar('T')
atomic: Callable[[T], T] = db.atomic
SECONDS_IN_YEAR = 365.25 * 24 * 60 * 60

# TODO: Consider using bulk-inserts for `AccountChangeSignal`s and
#       `PreparedTransferSignal`s when we decide to disable
#       auto-flushing. This would probably be slightly faster.

# TODO: Make `TableScanner.blocks_per_query` and
#       `TableScanner.target_beat_duration` configurable.


class AccountScanner(TableScanner):
    """Sends account heartbeat signals, purge deleted accounts."""

    table = Account.__table__
    pk = tuple_(Account.debtor_id, Account.creditor_id)

    def __init__(self):
        super().__init__()
        self.few_days_interval = timedelta(days=2)
        self.signalbus_max_delay = timedelta(days=current_app.config['APP_SIGNALBUS_MAX_DELAY_DAYS'])
        self.pending_transfers_max_delay = timedelta(days=current_app.config['APP_PENDING_TRANSFERS_MAX_DELAY_DAYS'])
        self.account_purge_delay = 2 * self.signalbus_max_delay + self.pending_transfers_max_delay

        # To prevent clogging the signal bus with heartbeat signals,
        # we make sure that the heartbeat interval is not shorter that
        # the maximum possible delay in the signal bus.
        self.account_heartbeat_interval = max(
            timedelta(days=current_app.config['APP_ACCOUNT_HEARTBEAT_DAYS']),
            self.signalbus_max_delay,
        )

    def _insert_heartbeat_signal(self, row, current_ts):
        """Resend the last sent `AccountChangeSignal` for a given row.

        NOTE: We do not update `change_ts` and `change_seqnum`,
              because there is no meaningful change in the account.

        """

        c = self.table.c
        db.session.add(AccountChangeSignal(
            debtor_id=row[c.debtor_id],
            creditor_id=row[c.creditor_id],
            change_ts=row[c.last_change_ts],
            change_seqnum=row[c.last_change_seqnum],
            principal=row[c.principal],
            interest=row[c.interest],
            interest_rate=row[c.interest_rate],
            last_transfer_seqnum=row[c.last_transfer_seqnum],
            last_outgoing_transfer_date=row[c.last_outgoing_transfer_date],
            last_config_signal_ts=row[c.last_config_signal_ts],
            last_config_signal_seqnum=row[c.last_config_signal_seqnum],
            creation_date=row[c.creation_date],
            negligible_amount=row[c.negligible_amount],
            status=row[c.status],
            inserted_at_ts=current_ts,
        ))

    @atomic
    def process_rows(self, rows):
        c = self.table.c
        deleted_flag = Account.STATUS_DELETED_FLAG
        current_ts = datetime.now(tz=timezone.utc)
        date_few_days_ago = (current_ts - self.few_days_interval).date()
        purge_cutoff_ts = current_ts - self.account_purge_delay
        heartbeat_cutoff_ts = current_ts - self.account_heartbeat_interval
        pks_to_delete = []
        reminded_pks = []

        for row in rows:
            if row[c.status] & deleted_flag:
                # If an account is created, deleted, purged, and
                # re-created in a single day, the `creation_date` of
                # the re-created account will be the same as the
                # `creation_date` of the purged account. We need to
                # make sure that this never happens.
                creation_date_is_ok = row[c.creation_date] < date_few_days_ago

                if row[c.last_change_ts] < purge_cutoff_ts and creation_date_is_ok:
                    pks_to_delete.append((row[c.debtor_id], row[c.creditor_id]))
            else:
                # A heartbeat signal (an `AccountChangeSignal`) is
                # sent either when there is a meaningful change in the
                # account, or to remind that the account still exists.
                last_heartbeat_ts = max(row[c.last_change_ts], row[c.last_reminder_ts])

                if last_heartbeat_ts < heartbeat_cutoff_ts:
                    self._insert_heartbeat_signal(row, current_ts)
                    reminded_pks.append((row[c.debtor_id], row[c.creditor_id]))

        if pks_to_delete:
            to_delete = db.session.query(Account.debtor_id, Account.creditor_id, Account.creation_date).\
                filter(self.pk.in_(pks_to_delete)).\
                filter(Account.status.op('&')(deleted_flag) == deleted_flag).\
                filter(Account.last_change_ts < purge_cutoff_ts).\
                filter(Account.creation_date < date_few_days_ago).\
                with_for_update().\
                all()
            if to_delete:
                pks_to_delete = [(debtor_id, creditor_id) for debtor_id, creditor_id, _ in to_delete]
                Account.query.filter(self.pk.in_(pks_to_delete)).delete(synchronize_session=False)
                db.session.add_all([
                    AccountPurgeSignal(
                        debtor_id=debtor_id,
                        creditor_id=creditor_id,
                        creation_date=creation_date,
                    )
                    for debtor_id, creditor_id, creation_date in to_delete
                ])

        if reminded_pks:
            Account.query.\
                filter(self.pk.in_(reminded_pks)).\
                update({Account.last_reminder_ts: current_ts}, synchronize_session=False)


class PreparedTransferScanner(TableScanner):
    """Attempts to finalize staled prepared transfers."""

    table = PreparedTransfer.__table__
    pk = tuple_(PreparedTransfer.debtor_id, PreparedTransfer.sender_creditor_id, PreparedTransfer.transfer_id)

    def __init__(self):
        super().__init__()
        self.signalbus_max_delay = timedelta(days=current_app.config['APP_SIGNALBUS_MAX_DELAY_DAYS'])
        self.pending_transfers_max_delay = timedelta(days=current_app.config['APP_PENDING_TRANSFERS_MAX_DELAY_DAYS'])
        self.critical_delay = 2 * self.signalbus_max_delay + self.pending_transfers_max_delay

    @atomic
    def process_rows(self, rows):
        c = self.table.c
        current_ts = datetime.now(tz=timezone.utc)
        critical_delay_cutoff_ts = current_ts - self.critical_delay
        recent_reminder_cutoff_ts = current_ts - max(self.signalbus_max_delay, self.pending_transfers_max_delay)
        reminded_pks = []

        for row in rows:
            last_reminder_ts = row[c.last_reminder_ts]
            has_critical_delay = row[c.prepared_at_ts] < critical_delay_cutoff_ts
            has_recent_reminder = last_reminder_ts is not None and last_reminder_ts >= recent_reminder_cutoff_ts
            if has_critical_delay and not has_recent_reminder:
                db.session.add(PreparedTransferSignal(
                    debtor_id=row[c.debtor_id],
                    sender_creditor_id=row[c.sender_creditor_id],
                    transfer_id=row[c.transfer_id],
                    coordinator_type=row[c.coordinator_type],
                    coordinator_id=row[c.coordinator_id],
                    coordinator_request_id=row[c.coordinator_request_id],
                    sender_locked_amount=row[c.sender_locked_amount],
                    recipient_creditor_id=row[c.recipient_creditor_id],
                    prepared_at_ts=row[c.prepared_at_ts],
                ))
                reminded_pks.append((row[c.debtor_id], row[c.sender_creditor_id], row[c.transfer_id]))

        if reminded_pks:
            PreparedTransfer.query.filter(self.pk.in_(reminded_pks)).update({
                PreparedTransfer.last_reminder_ts: current_ts,
            }, synchronize_session=False)
