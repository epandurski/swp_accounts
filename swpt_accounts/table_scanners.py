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


class AccountScanner(TableScanner):
    """Sends account heartbeat signals, purge deleted accounts."""

    table = Account.__table__
    pk = tuple_(Account.debtor_id, Account.creditor_id)

    def __init__(self):
        super().__init__()
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

    def _insert_heartbeat_signal(self, row):
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
            last_config_change_ts=row[c.last_config_change_ts],
            last_config_change_seqnum=row[c.last_config_change_seqnum],
            creation_date=row[c.creation_date],
            negligible_amount=row[c.negligible_amount],
            status=row[c.status],
        ))

    def _insert_account_purge_signal(self, row):
        c = self.table.c
        db.session.add(AccountPurgeSignal(
            debtor_id=row[c.debtor_id],
            creditor_id=row[c.creditor_id],
            creation_date=row[c.creation_date],
        ))

    @atomic
    def process_rows(self, rows):
        c = self.table.c
        pks_to_delete = []
        pks_to_update = []
        deleted_flag = Account.STATUS_DELETED_FLAG
        current_ts = datetime.now(tz=timezone.utc)
        yesterday = current_ts.date() - timedelta(days=1)
        purge_cutoff_ts = current_ts - self.account_purge_delay
        heartbeat_cutoff_ts = current_ts - self.account_heartbeat_interval
        for row in rows:
            if row[c.status] & deleted_flag:
                if row[c.creation_date] >= yesterday:  # pragma: no cover
                    # When one account is created, deleted, purged, and re-created
                    # in a single day, the `creation_date` of the re-created
                    # account will be the same as the `creation_date` of the
                    # purged account. This must be avoided, because we use the
                    # creation date to differentiate `AccountCommitSignal`s from
                    # different "epochs" (the `account_creation_date` column).
                    continue
                elif row[c.last_change_ts] < purge_cutoff_ts:
                    self._insert_account_purge_signal(row)
                    pks_to_delete.append((row[c.debtor_id], row[c.creditor_id]))
            else:
                last_heartbeat_ts = max(row[c.last_change_ts], row[c.last_remainder_ts])
                if last_heartbeat_ts < heartbeat_cutoff_ts:
                    self._insert_heartbeat_signal(row)
                    pks_to_update.append((row[c.debtor_id], row[c.creditor_id]))
        if pks_to_delete:
            Account.query.\
                filter(self.pk.in_(pks_to_delete)).\
                filter(Account.status.op('&')(deleted_flag) == deleted_flag).\
                filter(Account.last_change_ts < purge_cutoff_ts).\
                delete(synchronize_session=False)
        if pks_to_update:
            Account.query.\
                filter(self.pk.in_(pks_to_update)).\
                update({Account.last_remainder_ts: current_ts}, synchronize_session=False)


class PreparedTransferScanner(TableScanner):
    """Attempts to finalize staled prepared transfers."""

    table = PreparedTransfer.__table__
    pk = tuple_(table.c.debtor_id, table.c.sender_creditor_id, table.c.transfer_id)

    def __init__(self):
        super().__init__()
        self.signalbus_max_delay = timedelta(days=current_app.config['APP_SIGNALBUS_MAX_DELAY_DAYS'])
        self.pending_transfers_max_delay = timedelta(days=current_app.config['APP_PENDING_TRANSFERS_MAX_DELAY_DAYS'])
        self.critical_delay = 2 * self.signalbus_max_delay + self.pending_transfers_max_delay

    @atomic
    def process_rows(self, rows):
        c = self.table.c
        pks_to_update = []
        current_ts = datetime.now(tz=timezone.utc)
        critical_delay_cutoff_ts = current_ts - self.critical_delay
        recent_remainder_cutoff_ts = current_ts - max(self.signalbus_max_delay, self.pending_transfers_max_delay)
        for row in rows:
            last_remainder_ts = row[c.last_remainder_ts]
            has_critical_delay = row[c.prepared_at_ts] < critical_delay_cutoff_ts
            has_recent_remainder = last_remainder_ts is not None and last_remainder_ts >= recent_remainder_cutoff_ts
            if has_critical_delay and not has_recent_remainder:
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
                pks_to_update.append((row[c.debtor_id], row[c.sender_creditor_id], row[c.transfer_id]))
        if pks_to_update:
            PreparedTransfer.query.filter(self.pk.in_(pks_to_update)).update({
                PreparedTransfer.last_remainder_ts: current_ts,
            }, synchronize_session=False)
