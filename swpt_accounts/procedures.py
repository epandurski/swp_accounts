import math
from datetime import datetime, timezone, timedelta
from typing import TypeVar, Iterable, List, Tuple, Union, Optional, Callable, Set
from decimal import Decimal
from flask import current_app
from sqlalchemy.sql.expression import tuple_, and_
from sqlalchemy.exc import IntegrityError
from swpt_lib.utils import Seqnum, increment_seqnum, u64_to_i64, i64_to_u64
from .extensions import db
from .models import (
    Account, TransferRequest, PreparedTransfer, PendingAccountChange, RejectedConfigSignal,
    RejectedTransferSignal, PreparedTransferSignal, FinalizedTransferSignal,
    AccountUpdateSignal, AccountTransferSignal, AccountMaintenanceSignal, FinalizationRequest,
    ROOT_CREDITOR_ID, INTEREST_RATE_FLOOR, INTEREST_RATE_CEIL, TRANSFER_NOTE_MAX_BYTES,
    MIN_INT32, MAX_INT32, MIN_INT64, MAX_INT64, BEGINNING_OF_TIME, SECONDS_IN_DAY,
    CT_INTEREST, CT_DELETE, CT_DIRECT,
    SC_OK, SC_RECIPIENT_IS_UNREACHABLE, SC_INSUFFICIENT_AVAILABLE_AMOUNT,
    SC_RECIPIENT_SAME_AS_SENDER, SC_TOO_MANY_TRANSFERS, SC_TOO_LOW_INTEREST_RATE,
)

T = TypeVar('T')
atomic: Callable[[T], T] = db.atomic

ACCOUNT_PK = tuple_(Account.debtor_id, Account.creditor_id)
RC_INVALID_CONFIGURATION = 'INVALID_CONFIGURATION'
PREPARED_TRANSFER_JOIN_CLAUSE = and_(
    FinalizationRequest.debtor_id == PreparedTransfer.debtor_id,
    FinalizationRequest.sender_creditor_id == PreparedTransfer.sender_creditor_id,
    FinalizationRequest.transfer_id == PreparedTransfer.transfer_id,
    FinalizationRequest.coordinator_type == PreparedTransfer.coordinator_type,
    FinalizationRequest.coordinator_id == PreparedTransfer.coordinator_id,
    FinalizationRequest.coordinator_request_id == PreparedTransfer.coordinator_request_id,
)


@atomic
def configure_account(
        debtor_id: int,
        creditor_id: int,
        ts: datetime,
        seqnum: int,
        negligible_amount: float = 0.0,
        config_flags: int = 0,
        config: str = '') -> None:

    # TODO: Consider using a `ConfigureRequest` buffer table, to
    #       reduce lock contention on `account` table rows. This might
    #       be beneficial when there are lots of `ConfigureAccount`
    #       messages for one account, in a short period of time
    #       (probably not a typical load).

    assert MIN_INT64 <= debtor_id <= MAX_INT64
    assert MIN_INT64 <= creditor_id <= MAX_INT64
    assert ts > BEGINNING_OF_TIME
    assert MIN_INT32 <= seqnum <= MAX_INT32
    assert MIN_INT32 <= config_flags <= MAX_INT32

    current_ts = datetime.now(tz=timezone.utc)

    def update_account_status_flags(account):
        if account.status_flags & Account.STATUS_DELETED_FLAG:
            account.status_flags &= ~Account.STATUS_DELETED_FLAG
            account.status_flags &= ~Account.STATUS_ESTABLISHED_INTEREST_RATE_FLAG
        if config_flags & Account.CONFIG_SCHEDULED_FOR_DELETION_FLAG:
            account.status_flags |= Account.STATUS_UNREACHABLE_FLAG
        else:
            account.status_flags &= ~Account.STATUS_UNREACHABLE_FLAG

    def is_valid_config():
        return negligible_amount >= 0.0 and config == ''

    def try_to_configure(account):
        if is_valid_config():
            if account is None:
                account = _create_account(debtor_id, creditor_id, current_ts)
            update_account_status_flags(account)
            account.config_flags = config_flags
            account.negligible_amount = negligible_amount
            account.last_config_ts = ts
            account.last_config_seqnum = seqnum
            _apply_account_change(account, 0, 0, current_ts)
        else:
            db.session.add(RejectedConfigSignal(
                debtor_id=debtor_id,
                creditor_id=creditor_id,
                config_ts=ts,
                config_seqnum=seqnum,
                config_flags=config_flags,
                negligible_amount=negligible_amount,
                config=config,
                rejection_code=RC_INVALID_CONFIGURATION,
            ))

    account = _get_account_instance(debtor_id, creditor_id, lock=True)
    if account:
        this_event = (ts, Seqnum(seqnum))
        last_event = (account.last_config_ts, Seqnum(account.last_config_seqnum))
        if this_event > last_event:
            try_to_configure(account)
    else:
        signalbus_max_delay_seconds = current_app.config['APP_SIGNALBUS_MAX_DELAY_DAYS'] * SECONDS_IN_DAY
        signal_age_seconds = (current_ts - ts).total_seconds()
        if signal_age_seconds <= signalbus_max_delay_seconds:
            try_to_configure(account)


@atomic
def prepare_transfer(
        coordinator_type: str,
        coordinator_id: int,
        coordinator_request_id: int,
        min_locked_amount: int,
        max_locked_amount: int,
        debtor_id: int,
        creditor_id: int,
        recipient: str,
        ts: datetime,
        max_commit_delay: int = MAX_INT32,
        min_account_balance: int = 0,
        min_interest_rate: float = -100.0) -> None:

    assert len(coordinator_type) <= 30 and coordinator_type.encode('ascii')
    assert MIN_INT64 <= coordinator_id <= MAX_INT64
    assert MIN_INT64 <= coordinator_request_id <= MAX_INT64
    assert 0 <= min_locked_amount <= max_locked_amount <= MAX_INT64
    assert MIN_INT64 <= debtor_id <= MAX_INT64
    assert MIN_INT64 <= creditor_id <= MAX_INT64
    assert ts > BEGINNING_OF_TIME
    assert 0 <= max_commit_delay <= MAX_INT32
    assert MIN_INT64 <= min_account_balance <= MAX_INT64

    if creditor_id != ROOT_CREDITOR_ID:
        # NOTE: Only the debtor's account is allowed to go
        # deliberately negative. This is because only the debtor's
        # account is allowed to issue money.
        min_account_balance = max(0, min_account_balance)

    try:
        recipient_creditor_id = u64_to_i64(int(recipient))
    except ValueError:
        db.session.add(RejectedTransferSignal(
            debtor_id=debtor_id,
            coordinator_type=coordinator_type,
            coordinator_id=coordinator_id,
            coordinator_request_id=coordinator_request_id,
            status_code=SC_RECIPIENT_IS_UNREACHABLE,
            total_locked_amount=0,
            sender_creditor_id=creditor_id,
            recipient=recipient,
        ))
    else:
        db.session.add(TransferRequest(
            debtor_id=debtor_id,
            coordinator_type=coordinator_type,
            coordinator_id=coordinator_id,
            coordinator_request_id=coordinator_request_id,
            min_locked_amount=min_locked_amount,
            max_locked_amount=max_locked_amount,
            sender_creditor_id=creditor_id,
            recipient_creditor_id=recipient_creditor_id,
            deadline=ts + timedelta(seconds=max_commit_delay),
            min_account_balance=min_account_balance,
            min_interest_rate=min_interest_rate,
        ))


@atomic
def finalize_transfer(
        debtor_id: int,
        creditor_id: int,
        transfer_id: int,
        coordinator_type: str,
        coordinator_id: int,
        coordinator_request_id: int,
        committed_amount: int,
        finalization_flags: int = 0,
        transfer_note: str = '',
        ts: datetime = None) -> None:

    assert MIN_INT64 <= debtor_id <= MAX_INT64
    assert MIN_INT64 <= creditor_id <= MAX_INT64
    assert MIN_INT64 <= transfer_id <= MAX_INT64
    assert len(coordinator_type) <= 30 and coordinator_type.encode('ascii')
    assert MIN_INT64 <= coordinator_id <= MAX_INT64
    assert MIN_INT64 <= coordinator_request_id <= MAX_INT64
    assert 0 <= committed_amount <= MAX_INT64
    assert MIN_INT32 <= finalization_flags <= MAX_INT32
    assert len(transfer_note) <= TRANSFER_NOTE_MAX_BYTES
    assert len(transfer_note.encode('utf8')) <= TRANSFER_NOTE_MAX_BYTES
    assert ts is None or ts > BEGINNING_OF_TIME

    db.session.add(FinalizationRequest(
        debtor_id=debtor_id,
        sender_creditor_id=creditor_id,
        transfer_id=transfer_id,
        coordinator_type=coordinator_type,
        coordinator_id=coordinator_id,
        coordinator_request_id=coordinator_request_id,
        committed_amount=committed_amount,
        finalization_flags=finalization_flags,
        transfer_note=transfer_note,
        ts=ts or datetime.now(tz=timezone.utc),
    ))

    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()


@atomic
def try_to_change_interest_rate(debtor_id: int, creditor_id: int, interest_rate: float, request_ts: datetime) -> None:
    assert MIN_INT64 <= debtor_id <= MAX_INT64
    assert MIN_INT64 <= creditor_id <= MAX_INT64
    assert not math.isnan(interest_rate)

    current_ts = datetime.now(tz=timezone.utc)
    signalbus_max_delay_seconds = current_app.config['APP_SIGNALBUS_MAX_DELAY_DAYS'] * SECONDS_IN_DAY
    is_valid_request = (current_ts - request_ts).total_seconds() <= signalbus_max_delay_seconds
    account = get_account(debtor_id, creditor_id, lock=True) if is_valid_request else None
    if account:
        # NOTE: Too big positive interest rates can cause account
        # balance overflows. To prevent this, the interest rates
        # should be kept within reasonable limits, and the accumulated
        # interest should be capitalized every once in a while (like
        # once a month).
        if interest_rate > INTEREST_RATE_CEIL:
            interest_rate = INTEREST_RATE_CEIL

        # NOTE: Too big negative interest rates are dangerous
        # too. Chances are that they have been entered either
        # maliciously or by mistake. It is a good precaution to not
        # allow them at all.
        if interest_rate < INTEREST_RATE_FLOOR:
            interest_rate = INTEREST_RATE_FLOOR

        has_established_interest_rate = account.status_flags & Account.STATUS_ESTABLISHED_INTEREST_RATE_FLAG
        has_incorrect_interest_rate = not has_established_interest_rate or account.interest_rate != interest_rate
        seconds_since_last_change = (current_ts - account.last_interest_rate_change_ts).total_seconds()
        if seconds_since_last_change > signalbus_max_delay_seconds + SECONDS_IN_DAY and has_incorrect_interest_rate:
            assert current_ts >= account.last_interest_rate_change_ts
            account.interest = float(_calc_account_accumulated_interest(account, current_ts))
            account.previous_interest_rate = account.interest_rate
            account.interest_rate = interest_rate
            account.last_interest_rate_change_ts = current_ts
            account.status_flags |= Account.STATUS_ESTABLISHED_INTEREST_RATE_FLAG
            _insert_account_update_signal(account, current_ts)

    _insert_account_maintenance_signal(debtor_id, creditor_id, request_ts, current_ts)


@atomic
def capitalize_interest(
        debtor_id: int,
        creditor_id: int,
        accumulated_interest_threshold: int,
        request_ts: datetime) -> None:

    assert MIN_INT64 <= debtor_id <= MAX_INT64
    assert MIN_INT64 <= creditor_id <= MAX_INT64
    assert MIN_INT64 <= accumulated_interest_threshold <= MAX_INT64

    current_ts = datetime.now(tz=timezone.utc)
    account = get_account(debtor_id, creditor_id, lock=True)
    if account:
        positive_threshold = max(1, abs(accumulated_interest_threshold))
        accumulated_interest = math.floor(_calc_account_accumulated_interest(account, current_ts))
        accumulated_interest = _contain_principal_overflow(accumulated_interest)
        if abs(accumulated_interest) >= positive_threshold:
            _make_debtor_payment(CT_INTEREST, account, accumulated_interest, current_ts)

    _insert_account_maintenance_signal(debtor_id, creditor_id, request_ts, current_ts)


@atomic
def try_to_delete_account(debtor_id: int, creditor_id: int, request_ts: datetime) -> None:
    assert MIN_INT64 <= debtor_id <= MAX_INT64
    assert MIN_INT64 <= creditor_id <= MAX_INT64

    current_ts = datetime.now(tz=timezone.utc)
    account = get_account(debtor_id, creditor_id, lock=True)
    if account and account.pending_transfers_count == 0:
        if creditor_id == ROOT_CREDITOR_ID:
            can_be_deleted = account.principal == 0
        else:
            has_negligible_balance = account.calc_current_balance(current_ts) <= max(2.0, account.negligible_amount)
            is_scheduled_for_deletion = account.config_flags & Account.CONFIG_SCHEDULED_FOR_DELETION_FLAG
            can_be_deleted = has_negligible_balance and is_scheduled_for_deletion

        if can_be_deleted:
            if account.principal != 0:
                _make_debtor_payment(CT_DELETE, account, -account.principal, current_ts)
            _mark_account_as_deleted(account, current_ts)

    _insert_account_maintenance_signal(debtor_id, creditor_id, request_ts, current_ts)


@atomic
def get_accounts_with_transfer_requests() -> Iterable[Tuple[int, int]]:
    return set(db.session.query(TransferRequest.debtor_id, TransferRequest.sender_creditor_id).all())


@atomic
def process_transfer_requests(debtor_id: int, creditor_id: int) -> None:
    current_ts = datetime.now(tz=timezone.utc)
    transfer_requests = TransferRequest.query.\
        filter_by(debtor_id=debtor_id, sender_creditor_id=creditor_id).\
        with_for_update(skip_locked=True).\
        all()

    if transfer_requests:
        sender_account = get_account(debtor_id, creditor_id, lock=True)
        reachable_recipient_account_pks = _get_reachable_recipient_account_pks(transfer_requests)
        rejected_transfer_signals = []
        prepared_transfer_signals = []

        for tr in transfer_requests:
            is_recipient_reachable = (debtor_id, tr.recipient_creditor_id) in reachable_recipient_account_pks
            signal = _process_transfer_request(tr, sender_account, is_recipient_reachable, current_ts)
            if isinstance(signal, RejectedTransferSignal):
                rejected_transfer_signals.append(signal)
            else:
                assert isinstance(signal, PreparedTransferSignal)
                prepared_transfer_signals.append(signal)

        # TODO: Use bulk-inserts when we decide to disable
        #       auto-flushing. This will be faster, because the useless
        #       auto-generated `signal_id`s would not be fetched separately
        #       for each inserted row.
        db.session.add_all(rejected_transfer_signals)
        db.session.add_all(prepared_transfer_signals)


@atomic
def get_accounts_with_finalization_requests() -> Iterable[Tuple[int, int]]:
    return set(db.session.query(FinalizationRequest.debtor_id, FinalizationRequest.sender_creditor_id).all())


@atomic
def process_finalization_requests(debtor_id: int, sender_creditor_id: int) -> None:
    current_ts = datetime.now(tz=timezone.utc)
    requests = db.session.query(FinalizationRequest, PreparedTransfer).\
        outerjoin(PreparedTransfer, PREPARED_TRANSFER_JOIN_CLAUSE).\
        filter(
            FinalizationRequest.debtor_id == debtor_id,
            FinalizationRequest.sender_creditor_id == sender_creditor_id).\
        with_for_update(skip_locked=True, of=FinalizationRequest).\
        all()

    # TODO: Use bulk-inserts when we decide to disable
    #       auto-flushing. This will be faster, because the useless
    #       auto-generated `change_id`s would not be fetched
    #       separately for each inserted `PendingAccountChange` row.
    if requests:
        sender_account = get_account(debtor_id, sender_creditor_id, lock=True)
        starting_balance = math.floor(sender_account.calc_current_balance(current_ts)) if sender_account else 0
        principal_delta = 0
        for fr, pt in requests:
            if pt and sender_account:
                expendable_amount = (
                    + starting_balance
                    + principal_delta
                    - sender_account.total_locked_amount
                    - pt.min_account_balance
                )
                committed_amount = _finalize_prepared_transfer(pt, fr, sender_account, expendable_amount, current_ts)
                assert committed_amount >= 0
                principal_delta -= committed_amount
                db.session.delete(pt)

            db.session.delete(fr)

        if principal_delta != 0:
            assert sender_account
            _apply_account_change(sender_account, principal_delta, 0, current_ts)


@atomic
def get_accounts_with_pending_changes() -> Iterable[Tuple[int, int]]:
    return set(db.session.query(PendingAccountChange.debtor_id, PendingAccountChange.creditor_id).all())


@atomic
def process_pending_account_changes(debtor_id: int, creditor_id: int) -> None:
    current_ts = datetime.now(tz=timezone.utc)
    changes = PendingAccountChange.query.\
        filter_by(debtor_id=debtor_id, creditor_id=creditor_id).\
        with_for_update(skip_locked=True).\
        all()

    if changes:
        principal_delta = 0
        interest_delta = 0.0
        account = _lock_or_create_account(debtor_id, creditor_id, current_ts)
        for change in changes:
            principal_delta += change.principal_delta
            interest_delta += change.interest_delta

            # NOTE: We should compensate for the fact that the
            # transfer was committed at `change.inserted_at_ts`, but
            # the transferred amount is being added to the account's
            # principal just now (`current_ts`).
            interest_delta += account.calc_due_interest(change.principal_delta, change.inserted_at_ts, current_ts)

            _insert_account_transfer_signal(
                account=account,
                coordinator_type=change.coordinator_type,
                other_creditor_id=change.other_creditor_id,
                committed_at_ts=change.inserted_at_ts,
                acquired_amount=change.principal_delta,
                transfer_note=change.transfer_note,
                principal=_contain_principal_overflow(account.principal + principal_delta),
            )
            db.session.delete(change)

        _apply_account_change(account, principal_delta, interest_delta, current_ts)


@atomic
def get_account(debtor_id: int, creditor_id: int, lock: bool = False) -> Optional[Account]:
    account = _get_account_instance(debtor_id, creditor_id, lock=lock)
    if account and not account.status_flags & Account.STATUS_DELETED_FLAG:
        return account
    return None


@atomic
def get_available_amount(debtor_id: int, creditor_id: int) -> Optional[int]:
    current_ts = datetime.now(tz=timezone.utc)
    account = get_account(debtor_id, creditor_id)
    if account:
        return _get_available_amount(account, current_ts)
    return None


@atomic
def make_debtor_payment(
        coordinator_type: str,
        debtor_id: int,
        creditor_id: int,
        amount: int,
        transfer_note: str = '') -> None:

    current_ts = datetime.now(tz=timezone.utc)
    account = _lock_or_create_account(debtor_id, creditor_id, current_ts)
    _make_debtor_payment(coordinator_type, account, amount, current_ts, transfer_note)


def _contain_principal_overflow(value: int) -> int:
    if value <= MIN_INT64:
        return -MAX_INT64
    if value > MAX_INT64:
        return MAX_INT64
    return value


def _insert_account_update_signal(account: Account, current_ts: datetime) -> None:
    # NOTE: Callers of this function should be very careful, because
    # it updates `account.last_change_ts` without updating
    # `account.interest`. This will result in an incorrect value for
    # the interest, unless the current balance is zero, or
    # `account.interest` is updated "manually" before this function is
    # called.

    account.last_change_seqnum = increment_seqnum(account.last_change_seqnum)
    account.last_change_ts = max(account.last_change_ts, current_ts)
    db.session.add(AccountUpdateSignal(
        debtor_id=account.debtor_id,
        creditor_id=account.creditor_id,
        last_change_seqnum=account.last_change_seqnum,
        last_change_ts=account.last_change_ts,
        principal=account.principal,
        interest=account.interest,
        interest_rate=account.interest_rate,
        last_interest_rate_change_ts=account.last_interest_rate_change_ts,
        last_transfer_number=account.last_transfer_number,
        last_transfer_committed_at_ts=account.last_transfer_committed_at_ts,
        last_config_ts=account.last_config_ts,
        last_config_seqnum=account.last_config_seqnum,
        creation_date=account.creation_date,
        negligible_amount=account.negligible_amount,
        config_flags=account.config_flags,
        status_flags=account.status_flags,
        inserted_at_ts=account.last_change_ts,
    ))


def _create_account(debtor_id: int, creditor_id: int, current_ts: datetime) -> Account:
    account = Account(
        debtor_id=debtor_id,
        creditor_id=creditor_id,
        creation_date=current_ts.date(),
    )
    with db.retry_on_integrity_error():
        db.session.add(account)
    return account


def _get_account_instance(debtor_id: int, creditor_id: int, lock: bool = False) -> Optional[Account]:
    if lock:
        account = Account.lock_instance((debtor_id, creditor_id))
    else:
        account = Account.get_instance((debtor_id, creditor_id))
    return account


def _lock_or_create_account(debtor_id: int, creditor_id: int, current_ts: datetime) -> Account:
    account = _get_account_instance(debtor_id, creditor_id, lock=True)
    if account is None:
        account = _create_account(debtor_id, creditor_id, current_ts)
        _insert_account_update_signal(account, current_ts)

    if account.status_flags & Account.STATUS_DELETED_FLAG:
        account.status_flags &= ~Account.STATUS_DELETED_FLAG
        account.status_flags &= ~Account.STATUS_ESTABLISHED_INTEREST_RATE_FLAG
        _insert_account_update_signal(account, current_ts)

    return account


def _get_available_amount(account: Account, current_ts: datetime) -> int:
    current_balance = math.floor(account.calc_current_balance(current_ts))
    return _contain_principal_overflow(current_balance - account.total_locked_amount)


def _calc_account_accumulated_interest(account: Account, current_ts: datetime) -> Decimal:
    return account.calc_current_balance(current_ts) - account.principal


def _insert_pending_account_change(
        debtor_id: int,
        creditor_id: int,
        coordinator_type: str,
        other_creditor_id: int,
        inserted_at_ts: datetime,
        transfer_note: str = None,
        principal_delta: int = 0,
        interest_delta: int = 0) -> None:

    # TODO: To achieve better scalability, consider emitting a
    #       `PendingAccountChangeSignal` instead (with a globally unique
    #       ID), then implement an actor that reads those signals and
    #       inserts `PendingAccountChange` records for them (correctly
    #       handling possible multiple deliveries).

    db.session.add(PendingAccountChange(
        debtor_id=debtor_id,
        creditor_id=creditor_id,
        coordinator_type=coordinator_type,
        other_creditor_id=other_creditor_id,
        inserted_at_ts=inserted_at_ts,
        transfer_note=transfer_note,
        principal_delta=principal_delta,
        interest_delta=interest_delta,
    ))


def _insert_account_transfer_signal(
        account: Account,
        coordinator_type: str,
        other_creditor_id: int,
        committed_at_ts: datetime,
        acquired_amount: int,
        transfer_note: str,
        principal: int) -> None:

    assert acquired_amount != 0
    is_negligible = 0 < acquired_amount <= account.negligible_amount

    # NOTE: We do not send notifications for transfers from/to the
    # debtor's account, because the debtor's account does not have a
    # real owning creditor.
    if not is_negligible and account.creditor_id != ROOT_CREDITOR_ID:
        previous_transfer_number = account.last_transfer_number
        account.last_transfer_number += 1
        account.last_transfer_committed_at_ts = committed_at_ts
        db.session.add(AccountTransferSignal(
            debtor_id=account.debtor_id,
            creditor_id=account.creditor_id,
            transfer_number=account.last_transfer_number,
            coordinator_type=coordinator_type,
            other_creditor_id=other_creditor_id,
            committed_at_ts=committed_at_ts,
            acquired_amount=acquired_amount,
            transfer_note=transfer_note,
            creation_date=account.creation_date,
            principal=principal,
            previous_transfer_number=previous_transfer_number,
        ))


def _mark_account_as_deleted(account: Account, current_ts: datetime):
    account.principal = 0
    account.interest = 0.0
    account.total_locked_amount = 0
    account.status_flags |= Account.STATUS_DELETED_FLAG
    _insert_account_update_signal(account, current_ts)


def _apply_account_change(account: Account, principal_delta: int, interest_delta: float, current_ts: datetime) -> None:
    account.interest = float(_calc_account_accumulated_interest(account, current_ts)) + interest_delta
    principal_possibly_overflown = account.principal + principal_delta
    principal = _contain_principal_overflow(principal_possibly_overflown)
    if principal != principal_possibly_overflown:
        account.status_flags |= Account.STATUS_OVERFLOWN_FLAG
    account.principal = principal
    _insert_account_update_signal(account, current_ts)


def _make_debtor_payment(
        coordinator_type: str,
        account: Account,
        amount: int,
        current_ts: datetime,
        transfer_note: str = '') -> None:

    assert coordinator_type != CT_DIRECT
    assert -MAX_INT64 <= amount <= MAX_INT64

    if amount != 0 and account.creditor_id != ROOT_CREDITOR_ID:
        _insert_pending_account_change(
            debtor_id=account.debtor_id,
            creditor_id=ROOT_CREDITOR_ID,
            coordinator_type=coordinator_type,
            other_creditor_id=account.creditor_id,
            inserted_at_ts=current_ts,
            transfer_note=transfer_note,
            principal_delta=-amount,
        )
        _insert_account_transfer_signal(
            account=account,
            coordinator_type=coordinator_type,
            other_creditor_id=ROOT_CREDITOR_ID,
            committed_at_ts=current_ts,
            acquired_amount=amount,
            transfer_note=transfer_note,
            principal=_contain_principal_overflow(account.principal + amount),
        )

        # NOTE: We do not need to update the principal and the
        # interest when the account is getting deleted, because they
        # will be consequently zeroed out anyway.
        if coordinator_type != CT_DELETE:
            principal_delta = amount
            interest_delta = -amount if coordinator_type == CT_INTEREST else 0
            _apply_account_change(account, principal_delta, interest_delta, current_ts)


def _process_transfer_request(
        tr: TransferRequest,
        sender_account: Optional[Account],
        is_recipient_reachable: bool,
        current_ts: datetime) -> Union[RejectedTransferSignal, PreparedTransferSignal]:

    def reject(status_code: str, total_locked_amount: int) -> RejectedTransferSignal:
        assert total_locked_amount >= 0
        return RejectedTransferSignal(
            debtor_id=tr.debtor_id,
            coordinator_type=tr.coordinator_type,
            coordinator_id=tr.coordinator_id,
            coordinator_request_id=tr.coordinator_request_id,
            status_code=status_code,
            total_locked_amount=total_locked_amount,
            sender_creditor_id=tr.sender_creditor_id,
            recipient=str(i64_to_u64(tr.recipient_creditor_id)),
        )

    def prepare(amount: int) -> PreparedTransferSignal:
        assert sender_account is not None
        sender_account.total_locked_amount = min(sender_account.total_locked_amount + amount, MAX_INT64)
        sender_account.pending_transfers_count += 1
        sender_account.last_transfer_id += 1
        demurrage_rate = INTEREST_RATE_FLOOR
        commit_period = AccountUpdateSignal.get_commit_period()
        deadline = min(current_ts + timedelta(seconds=commit_period), tr.deadline)
        db.session.add(PreparedTransfer(
            debtor_id=tr.debtor_id,
            sender_creditor_id=tr.sender_creditor_id,
            transfer_id=sender_account.last_transfer_id,
            coordinator_type=tr.coordinator_type,
            coordinator_id=tr.coordinator_id,
            coordinator_request_id=tr.coordinator_request_id,
            locked_amount=amount,
            recipient_creditor_id=tr.recipient_creditor_id,
            min_account_balance=tr.min_account_balance,
            min_interest_rate=tr.min_interest_rate,
            demurrage_rate=demurrage_rate,
            deadline=deadline,
            prepared_at_ts=current_ts,
        ))
        return PreparedTransferSignal(
            debtor_id=tr.debtor_id,
            sender_creditor_id=tr.sender_creditor_id,
            transfer_id=sender_account.last_transfer_id,
            coordinator_type=tr.coordinator_type,
            coordinator_id=tr.coordinator_id,
            coordinator_request_id=tr.coordinator_request_id,
            locked_amount=amount,
            recipient_creditor_id=tr.recipient_creditor_id,
            prepared_at_ts=current_ts,
            demurrage_rate=demurrage_rate,
            deadline=deadline,
            inserted_at_ts=current_ts,
        )

    db.session.delete(tr)

    if sender_account is None:
        return reject(SC_INSUFFICIENT_AVAILABLE_AMOUNT, 0)

    assert sender_account.debtor_id == tr.debtor_id
    assert sender_account.creditor_id == tr.sender_creditor_id

    if sender_account.pending_transfers_count >= MAX_INT32:
        return reject(SC_TOO_MANY_TRANSFERS, sender_account.total_locked_amount)

    if tr.sender_creditor_id == tr.recipient_creditor_id:
        return reject(SC_RECIPIENT_SAME_AS_SENDER, sender_account.total_locked_amount)

    # NOTE: Transfers to the debtor's account must be allowed even
    # when the debtor's account does not exist. In this case, it will
    # be created when the transfer is committed.
    if tr.recipient_creditor_id != ROOT_CREDITOR_ID and not is_recipient_reachable:
        return reject(SC_RECIPIENT_IS_UNREACHABLE, sender_account.total_locked_amount)

    if sender_account.interest_rate < tr.min_interest_rate:
        return reject(SC_TOO_LOW_INTEREST_RATE, sender_account.total_locked_amount)

    # NOTE: The available amount should be checked last, because if
    # the transfer request is rejected due to insufficient available
    # amount, and the same transfer request is made again, but for
    # small enough amount, we want it to succeed, and not fail for
    # some of the other possible reasons.
    available_amount = _get_available_amount(sender_account, current_ts)
    expendable_amount = available_amount - tr.min_account_balance
    expendable_amount = min(expendable_amount, tr.max_locked_amount)
    expendable_amount = max(0, expendable_amount)
    if expendable_amount < tr.min_locked_amount:
        return reject(SC_INSUFFICIENT_AVAILABLE_AMOUNT, sender_account.total_locked_amount)

    return prepare(expendable_amount)


def _finalize_prepared_transfer(
        pt: PreparedTransfer,
        fr: FinalizationRequest,
        sender_account: Account,
        expendable_amount: int,
        current_ts: datetime) -> int:

    sender_account.total_locked_amount = max(0, sender_account.total_locked_amount - pt.locked_amount)
    sender_account.pending_transfers_count = max(0, sender_account.pending_transfers_count - 1)
    interest_rate = sender_account.interest_rate
    status_code = pt.calc_status_code(fr.committed_amount, expendable_amount, interest_rate, current_ts)
    committed_amount = fr.committed_amount if status_code == SC_OK else 0
    if committed_amount > 0:
        _insert_account_transfer_signal(
            account=sender_account,
            coordinator_type=pt.coordinator_type,
            other_creditor_id=pt.recipient_creditor_id,
            committed_at_ts=current_ts,
            acquired_amount=-committed_amount,
            transfer_note=fr.transfer_note,
            principal=_contain_principal_overflow(sender_account.principal - committed_amount),
        )
        _insert_pending_account_change(
            debtor_id=pt.debtor_id,
            creditor_id=pt.recipient_creditor_id,
            coordinator_type=pt.coordinator_type,
            other_creditor_id=pt.sender_creditor_id,
            inserted_at_ts=current_ts,
            transfer_note=fr.transfer_note,
            principal_delta=committed_amount,
        )

    db.session.add(FinalizedTransferSignal(
        debtor_id=pt.debtor_id,
        sender_creditor_id=pt.sender_creditor_id,
        transfer_id=pt.transfer_id,
        coordinator_type=pt.coordinator_type,
        coordinator_id=pt.coordinator_id,
        coordinator_request_id=pt.coordinator_request_id,
        recipient_creditor_id=pt.recipient_creditor_id,
        prepared_at_ts=pt.prepared_at_ts,
        finalized_at_ts=current_ts,
        committed_amount=committed_amount,
        total_locked_amount=sender_account.total_locked_amount,
        status_code=status_code,
    ))
    return committed_amount


def _insert_account_maintenance_signal(
        debtor_id: int,
        creditor_id: int,
        request_ts: datetime,
        current_ts: datetime) -> None:

    db.session.add(AccountMaintenanceSignal(
        debtor_id=debtor_id,
        creditor_id=creditor_id,
        request_ts=request_ts,
        inserted_at_ts=current_ts,
    ))


def _get_reachable_recipient_account_pks(transfer_requests: List[TransferRequest]) -> Set[Tuple[int, int]]:
    # TODO: To achieve better scalability, consider using some fast
    #       distributed key-store (Redis?) containing the (debtor_id,
    #       creditor_id) tuples for all accessible accounts.

    account_pks = [(tr.debtor_id, tr.recipient_creditor_id) for tr in transfer_requests]
    account_pks = db.session.\
        query(Account.debtor_id, Account.creditor_id).\
        filter(ACCOUNT_PK.in_(account_pks)).\
        filter(Account.status_flags.op('&')(Account.STATUS_DELETED_FLAG) == 0).\
        filter(Account.status_flags.op('&')(Account.STATUS_UNREACHABLE_FLAG) == 0).\
        all()
    return set(account_pks)
