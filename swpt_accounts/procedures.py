import math
from datetime import datetime, timezone, timedelta
from typing import TypeVar, Iterable, List, Tuple, Union, Optional, Callable
from decimal import Decimal
from .extensions import db
from .models import Account, PreparedTransfer, RejectedTransferSignal, PreparedTransferSignal, \
    AccountChangeSignal, CommittedTransferSignal, PendingChange, TransferRequest, increment_seqnum, \
    MIN_INT64, MAX_INT64

T = TypeVar('T')
atomic: Callable[[T], T] = db.atomic
AccountId = Union[Account, Tuple[int, int]]

TINY_POSITIVE_AMOUNT = 3  # should be at least `2`
MAX_PENDING_TRANSFERS_COUNT = 1000

TD_ZERO = timedelta(seconds=0)
TD_SECOND = timedelta(seconds=1)
TD_MINUS_SECOND = -TD_SECOND
SECONDS_IN_YEAR = 365.25 * 24 * 60 * 60

# The account `(debtor_id, ROOT_CREDITOR_ID)` is special. This is the
# debtor's account. It issuers all the money. Also, all interest and
# demurrage payments come from/to this account.
ROOT_CREDITOR_ID = MIN_INT64


@atomic
def get_debtor_account_list(debtor_id: int, start_after: int = None, limit: bool = None) -> List[Account]:
    query = Account.query.filter_by(debtor_id=debtor_id).order_by(Account.creditor_id)
    if start_after is not None:
        query = query.filter(Account.creditor_id > start_after)
    if limit is not None:
        if limit < 1:
            return []
        query = query.limit(limit)
    return query.all()


@atomic
def get_account(debtor_id: int, creditor_id: int) -> Optional[Account]:
    return _get_account((debtor_id, creditor_id))


@atomic
def get_or_create_account(debtor_id: int, creditor_id: int) -> Account:
    assert MIN_INT64 <= debtor_id <= MAX_INT64
    assert MIN_INT64 <= creditor_id <= MAX_INT64
    return _get_or_create_account((debtor_id, creditor_id))


@atomic
def get_available_balance(debtor_id: int, creditor_id: int, ignore_interest: bool) -> int:
    return _get_available_balance((debtor_id, creditor_id), ignore_interest)


@atomic
def prepare_transfer(coordinator_type: str,
                     coordinator_id: int,
                     coordinator_request_id: int,
                     min_amount: int,
                     max_amount: int,
                     debtor_id: int,
                     sender_creditor_id: int,
                     recipient_creditor_id: int,
                     ignore_interest: bool) -> None:
    assert MIN_INT64 <= coordinator_id <= MAX_INT64
    assert MIN_INT64 <= coordinator_request_id <= MAX_INT64
    assert 0 < min_amount <= max_amount <= MAX_INT64
    assert MIN_INT64 <= debtor_id <= MAX_INT64
    assert MIN_INT64 <= sender_creditor_id <= MAX_INT64
    assert MIN_INT64 <= recipient_creditor_id <= MAX_INT64

    db.session.add(TransferRequest(
        debtor_id=debtor_id,
        coordinator_type=coordinator_type,
        coordinator_id=coordinator_id,
        coordinator_request_id=coordinator_request_id,
        min_amount=min_amount,
        max_amount=max_amount,
        sender_creditor_id=sender_creditor_id,
        recipient_creditor_id=recipient_creditor_id,
        ignore_interest=ignore_interest,
    ))


@atomic
def finalize_prepared_transfer(debtor_id: int,
                               sender_creditor_id: int,
                               transfer_id: int,
                               committed_amount: int,
                               transfer_info: dict = {}) -> None:
    assert committed_amount >= 0
    pt = PreparedTransfer.lock_instance((debtor_id, sender_creditor_id, transfer_id))
    if pt:
        if committed_amount == 0:
            _delete_prepared_transfer(pt)
        else:
            _commit_prepared_transfer(pt, committed_amount, transfer_info)


@atomic
def set_interest_rate(debtor_id: int,
                      creditor_id: int,
                      interest_rate: float,
                      change_seqnum: int,
                      change_ts: datetime) -> None:
    # Too big interest rates can cause account balance overflows. To
    # prevent this, the interest rates should be kept within
    # reasonable limits, and the accumulated interest should be
    # capitalized every once in a while (like once a month).
    assert -100.0 < interest_rate <= 100.0

    account = _get_account((debtor_id, creditor_id))
    if account:
        this_event = (change_seqnum, change_ts)
        prev_event = (account.interest_rate_last_change_seqnum, account.interest_rate_last_change_ts)
        if _is_later_event(this_event, prev_event):
            _change_interest_rate(account, interest_rate, change_seqnum, change_ts)
            if creditor_id == ROOT_CREDITOR_ID:
                # It is a nonsense to accumulate interest on debtor's
                # own account. Therefore, we only pretend that the
                # interest rate has been set, while leaving it zero.
                account.interest_rate = 0.0


@atomic
def capitalize_interest(debtor_id: int,
                        creditor_id: int,
                        accumulated_interest_threshold: int = 0,
                        current_ts: datetime = None) -> None:
    account = _get_account((debtor_id, creditor_id))
    if account:
        positive_threshold = max(1, abs(accumulated_interest_threshold))
        current_ts = current_ts or datetime.now(tz=timezone.utc)
        amount = math.floor(_calc_account_accumulated_interest(account, current_ts))

        # When the new account principal is positive and very close to
        # zero, we make it a zero. This behavior allows us to reliably
        # zero out the principal before deleting the account.
        if creditor_id != ROOT_CREDITOR_ID and 0 < account.principal + amount <= TINY_POSITIVE_AMOUNT:
            amount = -account.principal

        # Make sure `amount` and `-amount` are within INT64 limits.
        if amount > MAX_INT64:  # pragma: no cover
            amount = MAX_INT64
        if amount < -MAX_INT64:  # pragma: no cover
            amount = -MAX_INT64

        if abs(amount) >= positive_threshold:
            make_debtor_payment('interest', debtor_id, creditor_id, amount)


@atomic
def make_debtor_payment(
        coordinator_type: str,
        debtor_id: int,
        creditor_id: int,
        amount: int,
        transfer_info: dict = {}) -> None:
    assert MIN_INT64 <= debtor_id <= MAX_INT64
    assert MIN_INT64 <= creditor_id <= MAX_INT64
    assert -MAX_INT64 <= amount <= MAX_INT64

    # It could happen that the debtor must pay himself, for example,
    # when `capitalize_interest` is called for the debtor's
    # account. In this case we will simply discard the interest
    # accumulated on the account.
    is_self_payment = creditor_id == ROOT_CREDITOR_ID

    if amount > 0:
        # The debtor pays the creditor.
        _force_transfer(
            coordinator_type=coordinator_type,
            debtor_id=debtor_id,
            sender_creditor_id=ROOT_CREDITOR_ID,
            recipient_creditor_id=creditor_id,
            committed_at_ts=datetime.now(tz=timezone.utc),
            committed_amount=0 if is_self_payment else amount,
            transfer_info=transfer_info,
            recipient_interest_delta=0 if coordinator_type != 'interest' else -amount,
        )
    elif amount < 0:
        # The creditor pays the debtor.
        _force_transfer(
            coordinator_type=coordinator_type,
            debtor_id=debtor_id,
            sender_creditor_id=creditor_id,
            recipient_creditor_id=ROOT_CREDITOR_ID,
            committed_at_ts=datetime.now(tz=timezone.utc),
            committed_amount=0 if is_self_payment else -amount,
            transfer_info=transfer_info,
            sender_interest_delta=0 if coordinator_type != 'interest' else -amount,
        )


@atomic
def delete_account_if_zeroed(debtor_id: int, creditor_id: int, ignore_after_ts: datetime = None) -> None:
    current_ts = datetime.now(tz=timezone.utc)
    if ignore_after_ts and current_ts > ignore_after_ts:
        return
    account = _get_account((debtor_id, creditor_id))
    if (account
            and account.pending_transfers_count == 0
            and account.locked_amount == 0
            and 0 <= _calc_account_current_balance(account, current_ts) <= TINY_POSITIVE_AMOUNT):
        if account.principal != 0:
            capitalize_interest(debtor_id, creditor_id, 0, current_ts)
            process_pending_changes(debtor_id, creditor_id)
        if account.principal == 0:
            account.interest = 0.0
            account.status |= Account.STATUS_DELETED_FLAG
            _insert_account_change_signal(account, current_ts)


@atomic
def purge_deleted_account(debtor_id: int, creditor_id: int, if_deleted_before: datetime) -> None:
    Account.query.\
        filter_by(debtor_id=debtor_id, creditor_id=creditor_id).\
        filter(Account.status.op('&')(Account.STATUS_DELETED_FLAG) == Account.STATUS_DELETED_FLAG).\
        filter(Account.last_change_ts < if_deleted_before).\
        delete(synchronize_session=False)


@atomic
def get_accounts_with_transfer_requests() -> Iterable[Tuple[int, int]]:
    return set(db.session.query(TransferRequest.debtor_id, TransferRequest.sender_creditor_id).all())


@atomic
def get_accounts_with_pending_changes() -> Iterable[Tuple[int, int]]:
    return set(db.session.query(PendingChange.debtor_id, PendingChange.creditor_id).all())


@atomic
def process_transfer_requests(debtor_id: int, creditor_id: int) -> None:
    requests = TransferRequest.query.\
        filter_by(debtor_id=debtor_id, sender_creditor_id=creditor_id).\
        with_for_update(skip_locked=True).\
        all()
    if requests:
        sender_account = _get_account((debtor_id, creditor_id), lock=True)
        new_objects = []
        for request in requests:
            new_objects.extend(_process_transfer_request(request, sender_account))
            db.session.delete(request)
        db.session.bulk_save_objects(new_objects)


@atomic
def process_pending_changes(debtor_id: int, creditor_id: int) -> None:
    changes = PendingChange.query.\
        filter_by(debtor_id=debtor_id, creditor_id=creditor_id).\
        with_for_update(skip_locked=True).\
        all()
    if changes:
        account = _get_or_create_account((debtor_id, creditor_id), lock=True)
        current_ts = datetime.now(tz=timezone.utc)
        current_date = current_ts.date()
        principal_delta = interest_delta = 0
        nonzero_deltas = False
        for change in changes:
            if change.principal_delta != 0 or change.interest_delta != 0:
                nonzero_deltas = True
                principal_delta += change.principal_delta
                interest_delta += change.interest_delta
            if change.unlocked_amount is not None:
                account.locked_amount = max(0, account.locked_amount - change.unlocked_amount)
                account.pending_transfers_count = max(0, account.pending_transfers_count - 1)
                if change.principal_delta < 0:
                    account.last_outgoing_transfer_date = current_date
            db.session.delete(change)
        if nonzero_deltas:
            _apply_account_change(
                account=account,
                principal_delta=principal_delta,
                interest_delta=interest_delta,
                current_ts=current_ts,
            )


@atomic
def get_dead_transfers(if_prepared_before: datetime = None) -> List[PreparedTransfer]:
    if_prepared_before = if_prepared_before or datetime.now(tz=timezone.utc) - timedelta(days=7)
    return PreparedTransfer.query.\
        filter(PreparedTransfer.prepared_at_ts < if_prepared_before).\
        all()


def _is_later_event(event: Tuple[int, datetime], other_event: Tuple[Optional[int], Optional[datetime]]) -> bool:
    seqnum, ts = event
    other_seqnum, other_ts = other_event
    if other_ts:
        advance = ts - other_ts
    else:
        advance = TD_ZERO
    return advance >= TD_MINUS_SECOND and (
        advance > TD_SECOND
        or other_seqnum is None
        or 0 < (seqnum - other_seqnum) % 0x100000000 < 0x80000000
    )


def _insert_account_change_signal(account: Account, current_ts: datetime = None) -> None:
    current_ts = current_ts or datetime.now(tz=timezone.utc)
    account.last_change_seqnum = increment_seqnum(account.last_change_seqnum)
    account.last_change_ts = max(account.last_change_ts, current_ts)
    db.session.add(AccountChangeSignal(
        debtor_id=account.debtor_id,
        creditor_id=account.creditor_id,
        change_seqnum=account.last_change_seqnum,
        change_ts=account.last_change_ts,
        principal=account.principal,
        interest=account.interest,
        interest_rate=account.interest_rate,
        last_outgoing_transfer_date=account.last_outgoing_transfer_date,
        status=account.status,
    ))


def _create_account(debtor_id: int, creditor_id: int) -> Account:
    account = Account(debtor_id=debtor_id, creditor_id=creditor_id)
    with db.retry_on_integrity_error():
        db.session.add(account)
    _insert_account_change_signal(account)
    return account


def _get_account(account_or_pk: AccountId, lock: bool = False) -> Optional[Account]:
    if lock:
        account = Account.lock_instance(account_or_pk)
    else:
        account = Account.get_instance(account_or_pk)
    if account and not account.status & Account.STATUS_DELETED_FLAG:
        return account
    return None


def _get_or_create_account(account_or_pk: AccountId, lock: bool = False) -> Account:
    if lock:
        account = Account.lock_instance(account_or_pk)
    else:
        account = Account.get_instance(account_or_pk)
    if account is None:
        debtor_id, creditor_id = Account.get_pk_values(account_or_pk)
        account = _create_account(debtor_id, creditor_id)
    _resurrect_account_if_deleted(account)
    return account


def _resurrect_account_if_deleted(account: Account) -> None:
    if account.status & Account.STATUS_DELETED_FLAG:
        account.principal = 0
        account.pending_transfers_count = 0
        account.locked_amount = 0
        account.status = 0
        account.interest = 0.0
        account.interest_rate = 0.0
        account.interest_rate_last_change_seqnum = None
        account.interest_rate_last_change_ts = None
        account.last_outgoing_transfer_date = None
        _insert_account_change_signal(account)


def _calc_account_current_balance(account: Account, current_ts: datetime = None) -> Decimal:
    current_ts = current_ts or datetime.now(tz=timezone.utc)
    current_balance = account.principal + Decimal.from_float(account.interest)
    if current_balance > 0:
        k = math.log(1.0 + account.interest_rate / 100.0) / SECONDS_IN_YEAR
        passed_seconds = max(0.0, (current_ts - account.last_change_ts).total_seconds())
        current_balance *= Decimal.from_float(math.exp(k * passed_seconds))
    return current_balance


def _get_available_balance(account_or_pk: AccountId, ignore_interest: bool) -> int:
    avl_balance = 0
    account = _get_account(account_or_pk)
    if account:
        if ignore_interest:
            avl_balance = account.principal
        else:
            avl_balance = math.floor(_calc_account_current_balance(account))
        avl_balance -= account.locked_amount
    return avl_balance


def _calc_account_accumulated_interest(account: Account, current_ts: datetime) -> Decimal:
    return _calc_account_current_balance(account, current_ts) - account.principal


def _change_interest_rate(account: Account, interest_rate: float, change_seqnum: int, change_ts: datetime) -> None:
    current_ts = datetime.now(tz=timezone.utc)
    account.interest = float(_calc_account_accumulated_interest(account, current_ts))
    account.interest_rate = interest_rate
    account.interest_rate_last_change_seqnum = change_seqnum
    account.interest_rate_last_change_ts = change_ts
    account.status |= Account.STATUS_ESTABLISHED_INTEREST_RATE_FLAG
    _insert_account_change_signal(account, current_ts)


def _delete_prepared_transfer(pt: PreparedTransfer) -> None:
    _insert_pending_change(
        debtor_id=pt.debtor_id,
        creditor_id=pt.sender_creditor_id,
        unlocked_amount=pt.sender_locked_amount,
    )
    db.session.delete(pt)


def _commit_prepared_transfer(pt: PreparedTransfer, committed_amount: int, transfer_info: dict) -> None:
    assert committed_amount > 0
    if committed_amount > pt.sender_locked_amount:  # pragma: no cover
        committed_amount = pt.sender_locked_amount
    current_ts = datetime.now(tz=timezone.utc)
    _insert_pending_change(
        debtor_id=pt.debtor_id,
        creditor_id=pt.sender_creditor_id,
        principal_delta=-committed_amount,
        unlocked_amount=pt.sender_locked_amount,
    )
    _insert_pending_change(
        debtor_id=pt.debtor_id,
        creditor_id=pt.recipient_creditor_id,
        principal_delta=committed_amount,
    )
    db.session.add(CommittedTransferSignal(
        debtor_id=pt.debtor_id,
        coordinator_type=pt.coordinator_type,
        sender_creditor_id=pt.sender_creditor_id,
        recipient_creditor_id=pt.recipient_creditor_id,
        committed_at_ts=current_ts,
        committed_amount=committed_amount,
        committed_transfer_id=pt.transfer_id,
        transfer_info=transfer_info,
    ))
    db.session.delete(pt)


def _force_transfer(coordinator_type: str,
                    debtor_id: int,
                    sender_creditor_id: int,
                    recipient_creditor_id,
                    committed_at_ts: datetime,
                    committed_amount: int,
                    transfer_info: dict = {},
                    sender_interest_delta: int = 0,
                    recipient_interest_delta: int = 0) -> None:
    assert committed_amount >= 0
    if committed_amount != 0 and sender_creditor_id != recipient_creditor_id:
        db.session.add(CommittedTransferSignal(
            debtor_id=debtor_id,
            coordinator_type=coordinator_type,
            sender_creditor_id=sender_creditor_id,
            recipient_creditor_id=recipient_creditor_id,
            committed_at_ts=committed_at_ts,
            committed_amount=committed_amount,
            transfer_info=transfer_info,
        ))
    _insert_pending_change(
        debtor_id=debtor_id,
        creditor_id=sender_creditor_id,
        principal_delta=-committed_amount,
        interest_delta=sender_interest_delta,
    )
    _insert_pending_change(
        debtor_id=debtor_id,
        creditor_id=recipient_creditor_id,
        principal_delta=committed_amount,
        interest_delta=recipient_interest_delta,
    )


def _insert_pending_change(debtor_id: int,
                           creditor_id: int,
                           principal_delta: int = 0,
                           interest_delta: int = 0,
                           unlocked_amount: int = None) -> None:
    if principal_delta != 0 or interest_delta != 0 or unlocked_amount is not None:
        db.session.add(PendingChange(
            debtor_id=debtor_id,
            creditor_id=creditor_id,
            principal_delta=principal_delta,
            interest_delta=interest_delta,
            unlocked_amount=unlocked_amount,
        ))


def _apply_account_change(account: Account, principal_delta: int, interest_delta: int, current_ts: datetime) -> None:
    account.interest = float(_calc_account_accumulated_interest(account, current_ts) + interest_delta)
    new_principal = account.principal + principal_delta
    if new_principal < MIN_INT64:
        account.principal = MIN_INT64
        account.status |= Account.STATUS_OVERFLOWN_FLAG
    elif new_principal > MAX_INT64:
        account.principal = MAX_INT64
        account.status |= Account.STATUS_OVERFLOWN_FLAG
    else:
        account.principal = new_principal
    _insert_account_change_signal(account, current_ts)


def _process_transfer_request(tr: TransferRequest, sender_account: Optional[Account]) -> list:

    def reject(**kw) -> List[RejectedTransferSignal]:
        return [RejectedTransferSignal(
            debtor_id=tr.debtor_id,
            coordinator_type=tr.coordinator_type,
            coordinator_id=tr.coordinator_id,
            coordinator_request_id=tr.coordinator_request_id,
            details=kw,
        )]

    def accept(amount: int) -> List[Union[PreparedTransfer, PreparedTransferSignal]]:
        assert sender_account is not None
        current_ts = datetime.now(tz=timezone.utc)
        sender_account.locked_amount += amount
        sender_account.pending_transfers_count += 1
        if sender_account.last_transfer_id < MAX_INT64:
            sender_account.last_transfer_id += 1
        else:  # pragma: no cover
            sender_account.last_transfer_id = MIN_INT64
        return [
            PreparedTransfer(
                debtor_id=tr.debtor_id,
                sender_creditor_id=tr.sender_creditor_id,
                transfer_id=sender_account.last_transfer_id,
                coordinator_type=tr.coordinator_type,
                recipient_creditor_id=tr.recipient_creditor_id,
                sender_locked_amount=amount,
                prepared_at_ts=current_ts,
            ),
            PreparedTransferSignal(
                debtor_id=tr.debtor_id,
                sender_creditor_id=tr.sender_creditor_id,
                transfer_id=sender_account.last_transfer_id,
                coordinator_type=tr.coordinator_type,
                recipient_creditor_id=tr.recipient_creditor_id,
                sender_locked_amount=amount,
                prepared_at_ts=current_ts,
                coordinator_id=tr.coordinator_id,
                coordinator_request_id=tr.coordinator_request_id,
            ),
        ]

    if sender_account is None:
        return reject(
            error_code='ACC001',
            message='The sender account does not exist.',
        )
    assert sender_account.debtor_id == tr.debtor_id
    assert sender_account.creditor_id == tr.sender_creditor_id
    amount = min(_get_available_balance(sender_account, tr.ignore_interest), tr.max_amount)

    if amount < tr.min_amount:
        return reject(
            error_code='ACC002',
            message='The available balance is insufficient.',
            avl_balance=amount,
        )
    if tr.sender_creditor_id == ROOT_CREDITOR_ID:  # pragma: no cover
        return reject(
            error_code='ACC003',
            message="The sender account can not be the debtor's account.",
        )
    if tr.sender_creditor_id == tr.recipient_creditor_id:  # pragma: no cover
        return reject(
            error_code='ACC004',
            message='Recipient and sender accounts are the same.',
        )
    if sender_account.locked_amount + amount > MAX_INT64:  # pragma: no cover
        return reject(
            error_code='ACC005',
            message='The locked amount is too big.',
            locked_amount=sender_account.locked_amount + amount,
        )
    if sender_account.pending_transfers_count >= MAX_PENDING_TRANSFERS_COUNT:  # pragma: no cover
        return reject(
            error_code='ACC006',
            message='There are too many pending transfers.',
            pending_transfers_count=sender_account.pending_transfers_count,
        )
    if tr.recipient_creditor_id != ROOT_CREDITOR_ID and _get_account((tr.debtor_id, tr.recipient_creditor_id)) is None:
        return reject(
            error_code='ACC007',
            message='The recipient account does not exist.',
        )

    return accept(amount)
