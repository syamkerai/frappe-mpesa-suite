"""Payouts (B2C, B2B) and their Safaricom charges as M-Pesa statement lines.

Money in gets one Bank Transaction per receipt (money_in.bank_transaction); money out gets
the same. Each Paid payout gets a withdrawal line on its shortcode's Bank Account as soon
as its result's job has booked the payout, and (Book Safaricom Charges Automatically) a
second line for the Safaricom charge, with a Journal Entry from the shortcode's M-Pesa
Charges Account. Both lines are reconciled with their entries (Reconcile Payout and Charge
Lines Automatically) when exactly one entry matches; a sweep makes what a job missed.

Lines and entries carry the keys Astral's feed scripts use (the receipt on the payout's
line; "CHG-<receipt>" on the charge line and as its Journal Entry's Cheque No), so whichever
side makes one first, the other finds it and never makes a second.

The charge: a B2B result states it (DebitPartyCharges); a till's (Buy Goods) does not, and then
a Transaction Status query states it (money_out.b2b_status); only when no query can is it the
fall in the Working balance since the balance before. A B2C result does not; it carries
the wallet balances left after the payout. Daraja's docs put charges on the Charges Paid
account, but on Astral's shortcode that stays 0 and the charge comes off the Utility
balance, so the two are added. The charge booked is the B2C Charge Bands setting
(Safaricom's tariff); the fall in balance since the payout before checks it (a different
fall: nothing is booked, staff decide), and is booked itself when no bands are set.
"""

import datetime
import json

import frappe
from frappe.utils import add_days, cint, cstr, flt, get_datetime, getdate, nowdate

from ..money_in.bank_transaction import create_statement_line, find_existing
from ..money_in.common import (
    REGISTER,
    as_user,
    enabled,
    erpnext_installed,
    log_match,
    mpesa_bank_accounts,
    named_lock,
    suite,
    suite_int,
)
from ..wallet import accounts

B2C = "B2C Disbursement Request"
B2B = "Mpesa B2B Payment Request"
BT = "Bank Transaction"
JE = "Journal Entry"
CHARGE_PREFIX = "CHG-"
CHARGE_TYPE = "M-Pesa Charges"
LINE_TYPE = {B2C: "M-Pesa B2C", B2B: "M-Pesa B2B"}
KIND = {B2C: "B2C", B2B: "B2B"}
# A fall in balance larger than this is never one payout's charge: a sweep or a reversal
# went out in between.
MAX_SEEN_CHARGE = 100
# How far back the payout before this one may be, and an unsettled one may have been sent.
BALANCE_WINDOW = datetime.timedelta(days=1)
UNSETTLED = ("Not Initiated", "Initiated", "Status Unknown")

COMMON_FIELDS = ("name", "docstatus", "status", "transaction_id", "amount", "mpesa_settings", "creation")
OWN_FIELDS = {
    B2C: (
        "transaction_completed_datetime",
        "posting_date",
        "party_type",
        "party",
        "reference_doctype",
        "reference_name",
        "receiver_public_name",
        "phone_number",
        "utility_acct_avlbl_funds",
        "charges_paid_acct_avlbl_funds",
    ),
    B2B: (
        "trans_completed_time",
        "supplier",
        "purchase_invoice",
        "purchase_order",
        "employee",
        "salary_slip",
        "receiver_shortcode",
        "receiver_public_name",
        "command_id",
        "debit_party_charges",
        "debit_account_balance",
    ),
}
# Custom fields (migrate.create_custom_erpnext_fields): absent until the site is migrated.
LINK_FIELDS = ("bank_transaction", "mpesa_charge", "charge_bank_transaction")


def fed_docstatus(doctype: str) -> tuple:
    """Paid payouts that get lines. A B2B request may be Paid after it was cancelled (a late
    Result on an Unknown request): the money left, so it gets its line and charge too."""
    return (1, 2) if doctype == B2B else (1,)


# ---------------------------------------------------------------- charges (pure)
def parse_charges(value) -> float:
    """B2B DebitPartyCharges: "Business Pay Bill Charge|KES|2.00" (several joined by "&"); "" is none."""
    total = 0.0
    for part in cstr(value).split("&"):
        fields = [f.strip() for f in part.split("|")]
        if len(fields) < 3:
            continue
        try:
            total += float(fields[-1].replace(",", ""))
        except ValueError:
            continue
    return round(total, 2)


def parse_bands(text) -> list:
    """B2C Charge Bands: one "<up to amount> = <charge>" per line, amounts rising. Raises ValueError."""
    bands = []
    for number, raw in enumerate(cstr(text).splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p for p in line.replace(":", "=").replace(",", "").split("=") if p.strip()]
        try:
            up_to, charge = (float(p) for p in parts)
        except ValueError:
            raise ValueError(f"Line {number} ({raw.strip()!r}) must read '<up to amount> = <charge>'") from None
        if charge < 0 or up_to <= 0:
            raise ValueError(f"Line {number}: amounts and charges cannot be negative")
        if bands and up_to <= bands[-1][0]:
            raise ValueError(f"Line {number}: each 'up to' amount must be higher than the one above it")
        bands.append((up_to, charge))
    return bands


def band_charge(bands, amount) -> float | None:
    for up_to, charge in bands:
        if flt(amount) <= up_to + 0.001:
            return charge
    return None


def balance_total(row) -> float:
    """What a B2C result says the payout left: Utility plus Charges Paid (negative, or 0)."""
    return round(flt(row.get("utility_acct_avlbl_funds")) + flt(row.get("charges_paid_acct_avlbl_funds")), 2)


def predecessor(rows, payout):
    """The Paid payout that moved the wallet just before ``payout``, or None when that is unclear.

    Results come in any order, so payouts are put in the order the wallet moved: by
    completion time, then (several in one second) by the balance they left, highest first.
    A payout in between that left no balance hides how far the wallet fell.
    """
    total = balance_total(payout)
    before = [
        r for r in rows
        if r.completed < payout.completed or (r.completed == payout.completed and balance_total(r) > total)
    ]
    if not before:
        return None
    pred = max(before, key=lambda r: (r.completed, -balance_total(r)))
    if not balance_total(pred):
        return None
    return pred


def seen_charge(pred, payout) -> float | None:
    """The charge the balances show (whole shillings, 0 < charge <= MAX_SEEN_CHARGE), else None."""
    charge = round(balance_total(pred) - balance_total(payout) - flt(payout.amount), 2)
    if charge <= 0 or charge > MAX_SEEN_CHARGE or abs(charge - round(charge)) > 0.005:
        return None  # a top-up, a sweep or a reversal moved the wallet in between
    return charge


# ---------------------------------------------------------------- settings
def charge_bands() -> list:
    try:
        return parse_bands(suite().get("b2c_charge_bands"))
    except frappe.DoesNotExistError:
        return []
    except ValueError:
        frappe.log_error(frappe.get_traceback(), "M-Pesa Suite: B2C Charge Bands cannot be read")
        return []


def lines_from():
    """Payouts completed before this date are left alone (Payout Lines From; empty: all)."""
    try:
        value = suite().get("payout_lines_from")
    except frappe.DoesNotExistError:
        return None
    return getdate(value) if value else None


# ---------------------------------------------------------------- payouts
def load(doctype: str, name: str):
    meta = frappe.get_meta(doctype)
    fields = [
        f for f in COMMON_FIELDS + OWN_FIELDS[doctype] + LINK_FIELDS
        if f in ("name", "docstatus", "creation") or meta.has_field(f)
    ]
    p = frappe.db.get_value(doctype, name, fields, as_dict=True)
    if not p:
        return None
    p.doctype = doctype
    p.receipt = cstr(p.transaction_id).strip()
    when = p.get("transaction_completed_datetime") if doctype == B2C else p.get("trans_completed_time")
    p.completed = get_datetime(when) if when else None
    p.date = p.completed.date() if p.completed else getdate(p.get("posting_date") or p.creation or nowdate())
    return p


def description(p) -> str:
    if p.doctype == B2C:
        to = p.receiver_public_name or p.phone_number or "-"
        on = " ".join(filter(None, [p.reference_doctype, p.reference_name])) or "-"
        return f"M-Pesa B2C payout to {to} | {on} | {p.name}"
    to = p.receiver_public_name or p.receiver_shortcode or "-"
    on = p.purchase_invoice or p.get("purchase_order") or p.get("salary_slip") or p.supplier or p.get("employee") or "-"
    return f"M-Pesa B2B {p.command_id or ''} to {to} | {on} | {p.name}"


def party(p) -> tuple:
    if p.doctype == B2C:
        return p.party_type, p.party
    if p.get("employee"):
        return "Employee", p.employee
    return ("Supplier", p.supplier) if p.supplier else (None, None)


def sub_account(p) -> str:
    """The wallet a payout left: B2C from Utility; B2B from the account its result names (Working)."""
    if p.doctype == B2B:
        return accounts.from_balance_text(p.get("debit_account_balance")) or accounts.WORKING
    return accounts.UTILITY


def payout_bank_account(p) -> str | None:
    """That sub-account's Bank Account (the shortcode's own unless the sub-account is mapped)."""
    return accounts.bank_account(p.mpesa_settings, sub_account(p))


def charge_for(p) -> float | None:
    """The payout's Safaricom charge when it is certain, else None (left for staff, or for the
    sweep while a B2C's balances cannot show it yet)."""
    if p.doctype == B2B:
        if cstr(p.debit_party_charges).strip():
            return parse_charges(p.debit_party_charges)  # Safaricom states it (a stated 0 stays 0)
        return unstated_b2b_charge(p)
    bands = charge_bands()
    tariff = band_charge(bands, p.amount) if bands else None
    seen = balance_charge(p)
    if tariff is not None and seen is not None and abs(seen - tariff) > 0.005:
        log_match(
            "Ambiguous", "Payout Charge", once=True, reference_doctype=p.doctype, reference_name=p.name,
            receipt=p.receipt, amount=p.amount,
            details=f"B2C Charge Bands give {tariff} for {flt(p.amount)}, but the M-Pesa balance fell by "
            f"{flt(p.amount) + seen} ({seen} more than the payout). No charge was booked: book it by hand and, "
            "if Safaricom changed its tariff, update B2C Charge Bands on M-Pesa Suite Settings.",
        )
        return None
    return tariff if tariff is not None else seen


def balance_charge(p) -> float | None:
    """The charge the wallet balances show for a B2C payout, or None when they cannot show it."""
    if p.doctype != B2C or not p.completed or not balance_total(p):
        return None
    shortcode = cstr(frappe.db.get_value("Mpesa Settings", p.mpesa_settings, "business_shortcode")).strip()
    wallet = [
        r.name
        for r in frappe.get_all("Mpesa Settings", fields=["name", "business_shortcode"], order_by="creation asc")
        if cstr(r.business_shortcode).strip() == shortcode
    ] or [p.mpesa_settings]
    since = p.completed - BALANCE_WINDOW
    # A payout still in flight may have moved the wallet before this one.
    if frappe.get_all(
        B2C,
        filters=[
            [B2C, "mpesa_settings", "in", wallet],
            [B2C, "docstatus", "=", 1],
            [B2C, "status", "in", UNSETTLED],
            [B2C, "creation", ">=", since],
            [B2C, "creation", "<=", p.completed],
        ],
        pluck="name",
        limit=1,
    ):
        return None
    rows = frappe.get_all(
        B2C,
        filters=[
            [B2C, "mpesa_settings", "in", wallet],
            [B2C, "docstatus", "=", 1],
            [B2C, "status", "=", "Paid"],
            [B2C, "name", "!=", p.name],
            [B2C, "transaction_completed_datetime", ">=", since],
            [B2C, "transaction_completed_datetime", "<=", p.completed],
        ],
        fields=["name", "amount", "transaction_completed_datetime", "utility_acct_avlbl_funds",
                "charges_paid_acct_avlbl_funds"],
        order_by="transaction_completed_datetime asc",
    )
    for r in rows:
        r.completed = get_datetime(r.transaction_completed_datetime)
    pred = predecessor(rows, p)
    if not pred:
        return None
    if any(not balance_total(r) and pred.completed <= r.completed <= p.completed for r in rows):
        return None
    # On a shortcode that also collects, a receipt in between raised the balance.
    if shortcode and frappe.get_all(
        REGISTER,
        filters=[
            [REGISTER, "businessshortcode", "=", shortcode],
            [REGISTER, "transtime", ">=", pred.completed.strftime("%Y%m%d%H%M%S")],
            [REGISTER, "transtime", "<=", p.completed.strftime("%Y%m%d%H%M%S")],
        ],
        pluck="name",
        limit=1,
    ):
        return None
    return seen_charge(pred, p)


def unstated_b2b_charge(p) -> float | None:
    """A B2B whose Result stated no charge (a till payment). None while a status query may still
    state it: the status answer books it (b2b_status.fill), else the sweep looks again. Then what
    the Working balances show, else None, logged once in the Match Log ("charge not stated")."""
    from .b2b_status import still_asking

    if still_asking(p.name):
        return None  # Safaricom's own figure: a portal payment in between would skew the balance's
    seen = b2b_balance_charge(p)
    if seen is not None:
        return seen
    log_match(
        "Ambiguous", "Payout Charge", once=True, reference_doctype=p.doctype, reference_name=p.name,
        receipt=p.receipt, amount=p.amount,
        details=f"Charge not stated: Safaricom did not state the charge on {p.receipt}, its status query did not "
        "give it and the Working balance cannot show it. No charge was booked: take it from the M-Pesa "
        "statement and book it by hand.",
    )
    return None


def working_balance(text) -> float | None:
    """The Working balance a B2B Result left ("Working Account|KES|<current>|..."), or None."""
    from ..wallet.balances import parse_balances

    return parse_balances(text).get(accounts.WORKING)


def b2b_balance_charge(p) -> float | None:
    """The charge the Working balances show for a B2B payout: the balance before it (the Paid B2B
    just before, or a later Account Balance of Working), less the balance its Result left, less
    the amount. None when that is unclear: no balance before it within BALANCE_WINDOW, another
    B2B in flight or one without a balance in between, or a fall that is no payout's charge."""
    this = working_balance(p.get("debit_account_balance"))
    if this is None or not p.completed:
        return None
    shortcode = cstr(frappe.db.get_value("Mpesa Settings", p.mpesa_settings, "business_shortcode")).strip()
    wallet = [
        r.name
        for r in frappe.get_all("Mpesa Settings", fields=["name", "business_shortcode"], order_by="creation asc")
        if cstr(r.business_shortcode).strip() == shortcode
    ] or [p.mpesa_settings]
    since = p.completed - BALANCE_WINDOW
    # A B2B still in flight may have moved the wallet before this one: sent within the window
    # (its ack or its send marker), however long before that it was drafted.
    if frappe.get_all(
        B2B,
        filters=[
            [B2B, "mpesa_settings", "in", wallet],
            [B2B, "docstatus", "=", 1],
            [B2B, "status", "in", ("Initiated", "Unknown")],
            [B2B, "creation", "<=", p.completed],
        ],
        or_filters=[[B2B, "modified", ">=", since], [B2B, "send_started_on", ">=", since]],
        pluck="name",
        limit=1,
    ):
        return None
    rows = frappe.get_all(
        B2B,
        filters=[
            [B2B, "mpesa_settings", "in", wallet],
            [B2B, "docstatus", "in", [1, 2]],
            [B2B, "status", "=", "Paid"],
            [B2B, "name", "!=", p.name],
            [B2B, "trans_completed_time", ">=", since],
            [B2B, "trans_completed_time", "<=", p.completed],
        ],
        fields=["name", "trans_completed_time", "debit_account_balance"],
        order_by="trans_completed_time asc",
    )
    for r in rows:
        r.completed = get_datetime(r.trans_completed_time)
        r.balance = working_balance(r.debit_account_balance)
    # Results come in any order: the wallet moved in completion order, then (several in one
    # second) by the balance they left, highest first.
    def order(r):
        return (r.completed, -r.balance)

    pred = max((r for r in rows if r.balance is not None and order(r) < (p.completed, -this)), key=order, default=None)
    snapshot = None
    if shortcode and frappe.db.table_exists("Mpesa Balance Snapshot"):
        snapshot = frappe.db.get_value(
            "Mpesa Balance Snapshot",
            {"shortcode": shortcode, "sub_account": accounts.WORKING, "source": "Account Balance",
             "taken_at": ["between", [pred.completed if pred else since, p.completed]]},
            ["taken_at", "balance"],
            as_dict=True,
            order_by="taken_at desc",
        )
    taken = get_datetime(snapshot.taken_at) if snapshot else None
    if taken and taken < p.completed and (not pred or taken > pred.completed):
        start, start_balance = taken, flt(snapshot.balance)  # a balance asked for after the B2B before
    elif pred:
        start, start_balance = pred.completed, pred.balance
    else:
        return None  # no balance before it within the window
    if any(r.balance is None and start <= r.completed <= p.completed for r in rows):
        return None  # a B2B in between left no balance: how far the wallet fell is hidden
    charge = round(start_balance - this - flt(p.amount), 2)
    if charge <= 0 or charge > MAX_SEEN_CHARGE or abs(charge - round(charge)) > 0.005:
        return None  # a funding, a transfer or a sweep moved the wallet in between
    return charge


# ---------------------------------------------------------------- statement lines
def after_payout(doctype: str, name: str) -> None:
    """A payout's result was recorded (and its entry booked): its lines, now. Never raises."""
    if not enabled("create_bank_transactions") or not erpnext_installed():
        return
    frappe.db.savepoint("mpesa_payout_bt")
    try:
        feed(doctype, name)
    except Exception:
        frappe.db.rollback(save_point="mpesa_payout_bt")
        frappe.log_error(title=f"M-Pesa payout statement line failed for {name}"[:140], message=frappe.get_traceback())


def feed(doctype: str, name: str) -> str | None:
    """Payout line, charge line and charge entry for one Paid payout, reconciled. Idempotent."""
    p = load(doctype, name)
    if not p or p.docstatus not in fed_docstatus(doctype) or p.status != "Paid" or not p.receipt:
        return None
    start = lines_from()
    if start and p.date < start:
        return None
    bank_account = payout_bank_account(p)
    if not bank_account:
        log_match(
            "Error", "Bank Reconciliation", once=True, reference_doctype=doctype, reference_name=name,
            receipt=p.receipt, amount=p.amount,
            details=f"No Bank Account mapped for Mpesa Settings {p.mpesa_settings}: payout {p.receipt} has no "
            "statement line. Set it on Mpesa Settings → Accounting Mapping.",
        )
        return None

    party_type, party_name = party(p)
    line = statement_line(
        p, bank_account, p.receipt, p.amount, description(p), LINE_TYPE[doctype], party_type, party_name
    )
    if not line:
        return None
    _link(p, "bank_transaction", line)

    charge_line = None
    if enabled("book_payout_charges"):
        key = CHARGE_PREFIX + p.receipt
        charge = charge_for(p)
        if charge:
            charge_line = statement_line(
                p, bank_account, key, charge, f"Safaricom charge on M-Pesa {KIND[doctype]} {p.receipt} | {p.name}",
                CHARGE_TYPE,
            )
        elif charge is None:
            charge_line = find_existing(bank_account, key, withdrawal=True)  # one Astral's feed made
        if charge_line:
            _link(p, "charge_bank_transaction", charge_line)
            booked = flt(frappe.db.get_value(BT, charge_line, "withdrawal"))
            if charge and abs(booked - flt(charge)) > 0.005:
                # a line made earlier (from the balances, or by Astral's feed) says otherwise: the
                # line, its entry and the charge on the payout stay as they are for staff to correct
                log_match(
                    "Ambiguous", "Payout Charge", on_change=True, reference_doctype=p.doctype, reference_name=p.name,
                    receipt=p.receipt, amount=flt(charge),
                    details=f"Charge line {charge_line} says {booked}, but the charge on {p.receipt} is {flt(charge)}. "
                    "Nothing was changed: correct the charge line and its Journal Entry by hand.",
                )
            else:
                _link(p, "mpesa_charge", charge or booked)
                charge_entry(p, bank_account, charge_line, charge)

    if reconciling():
        gl = frappe.db.get_value("Bank Account", bank_account, "account")
        reconcile_withdrawal(line, gl)
        if charge_line:
            reconcile_withdrawal(charge_line, gl)
    return line


def statement_line(p, bank_account, key, amount, text, transaction_type, party_type=None, party_name=None):
    """The withdrawal line ``key`` on ``bank_account``; None when it is already on another M-Pesa account."""
    elsewhere = line_elsewhere(bank_account, key)
    if elsewhere:
        log_match(
            "Ambiguous", "Bank Reconciliation", once=True, reference_doctype=p.doctype, reference_name=p.name,
            receipt=p.receipt, amount=amount,
            details=f"{key} is already on Bank Transaction {elsewhere.name} of {elsewhere.bank_account}, but "
            f"Mpesa Settings {p.mpesa_settings} maps this shortcode to {bank_account}. No second line was made: "
            "correct the mapping or that line.",
        )
        return None
    return create_statement_line(
        bank_account=bank_account,
        receipt=key,
        amount=amount,
        date=p.date,
        description=text,
        transaction_type=transaction_type,
        withdrawal=True,
        party_type=party_type,
        party=party_name,
        sub_account=sub_account(p),
        completed_at=p.completed,
    )


def line_elsewhere(bank_account: str, key: str):
    """A live withdrawal line for ``key`` on another M-Pesa Bank Account (Astral's feed found
    a shortcode's account by its number, the suite by Mpesa Settings)."""
    others = sorted(mpesa_bank_accounts() - {bank_account})
    if not others:
        return None
    for field in ("reference_number", "transaction_id"):
        row = frappe.db.get_value(
            BT,
            {field: key, "bank_account": ["in", others], "withdrawal": [">", 0], "docstatus": ["<", 2]},
            ["name", "bank_account"],
            as_dict=True,
            order_by="creation asc",
        )
        if row:
            return row
    return None


def _link(p, field: str, value) -> None:
    if field in p and p.get(field) != value:
        frappe.db.set_value(p.doctype, p.name, field, value, update_modified=False)
        p[field] = value


def charge_entry(p, bank_account: str, line: str, charge: float | None) -> str | None:
    """Journal Entry for the charge line: Dr the shortcode's M-Pesa Charges Account, Cr its
    bank GL, Cheque No "CHG-<receipt>". An entry Astral's feed made is used as it is."""
    key = CHARGE_PREFIX + p.receipt
    # cheque_no is indexed by ERPNext: the locking read sees an entry another worker
    # committed while we waited for the lock, without locking the table.
    with named_lock(f"je:{key}"):
        existing = frappe.db.get_value(
            JE, {"cheque_no": key, "docstatus": ["<", 2]}, "name", order_by="docstatus desc, creation asc",
            for_update=True,
        )
        if existing:
            return existing
        if charge is None:
            return None  # a line Astral's feed made for a charge the suite cannot confirm
        row = frappe.db.get_value(BT, line, ["withdrawal", "date", "docstatus"], as_dict=True)
        amount = flt(row.withdrawal)
        if abs(amount - flt(charge)) > 0.005:
            log_match(
                "Ambiguous", "Payout Charge", once=True, reference_doctype=p.doctype, reference_name=p.name,
                receipt=p.receipt, amount=amount,
                details=f"Charge line {line} was made before the suite and says {amount}; this payout's charge "
                f"is {flt(charge)}. No entry was booked: correct the line and book its charge.",
            )
            return None
        meta = frappe.get_meta("Mpesa Settings")
        settings = frappe.db.get_value(
            "Mpesa Settings",
            p.mpesa_settings,
            [f for f in ("charges_account", "submit_b2c_accounting_entries") if meta.has_field(f)],
            as_dict=True,
        ) or frappe._dict()
        if not settings.get("charges_account"):
            log_match(
                "Error", "Payout Charge", once=True, reference_doctype=p.doctype, reference_name=p.name,
                receipt=p.receipt, amount=amount,
                details=f"No M-Pesa Charges Account on Mpesa Settings {p.mpesa_settings} (Accounting Mapping): "
                f"charge line {line} waits for its entry.",
            )
            return None
        bank = frappe.db.get_value("Bank Account", bank_account, ["account", "company"], as_dict=True)
        if frappe.db.get_value("Account", settings.charges_account, "company") != bank.company:
            log_match(
                "Error", "Payout Charge", once=True, reference_doctype=p.doctype, reference_name=p.name,
                receipt=p.receipt, amount=amount,
                details=f"M-Pesa Charges Account {settings.charges_account} is not in {bank.company}, the company "
                f"of {bank_account}.",
            )
            return None
        from .common import payout_cost_center

        je = frappe.new_doc(JE)
        je.voucher_type = "Bank Entry"
        je.company = bank.company
        je.posting_date = row.date
        je.cheque_no = key
        je.cheque_date = row.date
        je.user_remark = f"Safaricom charge on M-Pesa {KIND.get(p.doctype, 'payout')} {p.receipt} ({p.name})"
        if frappe.get_meta(JE).has_field("custom_remark"):  # v16 builds its own remark unless told
            je.custom_remark = 1
            je.remark = je.user_remark
        # a charge follows the shortcode, never a document: both rows on one cost center (F5)
        cost_center = payout_cost_center(p.mpesa_settings, bank.company)
        je.append(
            "accounts",
            {"account": settings.charges_account, "debit_in_account_currency": amount, "cost_center": cost_center},
        )
        je.append("accounts", {"account": bank.account, "credit_in_account_currency": amount, "cost_center": cost_center})
        je.flags.ignore_permissions = True
        je.insert()
        # reviewed like the payout's own entry (B2C and B2B both follow this switch)
        if cint(settings.get("submit_b2c_accounting_entries")):
            je.submit()
        return je.name


# ---------------------------------------------------------------- reconciliation
def reconciling() -> bool:
    return enabled("auto_reconcile_bank_transactions") and enabled("reconcile_payout_lines")


def entries_for(key: str, gl: str) -> list:
    """Submitted, uncleared entries carrying ``key`` that pay out of ``gl``: (doctype, name, amount).

    A payout's Payment Entry carries its receipt as Reference No (B2C, B2B; also an Internal
    Transfer staff made between two shortcodes), a salary payout's and a charge's Journal
    Entry as Cheque No.
    """
    found = [
        ("Payment Entry", r.name, flt(r.paid_amount))
        for r in frappe.get_all(
            "Payment Entry",
            filters={"reference_no": key, "docstatus": 1, "paid_from": gl, "clearance_date": ["is", "not set"]},
            fields=["name", "paid_amount"],
            order_by="creation asc",
        )
    ]
    found += [
        (JE, name, flt(amount))
        for name, amount in frappe.db.sql(
            """
            SELECT je.name, SUM(jea.credit_in_account_currency - jea.debit_in_account_currency)
            FROM `tabJournal Entry` je
            JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
            WHERE je.cheque_no = %s AND je.docstatus = 1 AND je.clearance_date IS NULL AND jea.account = %s
            GROUP BY je.name
            HAVING SUM(jea.credit_in_account_currency - jea.debit_in_account_currency) > 0
            ORDER BY je.creation
            """,
            (key, gl),
        )
    ]
    return found


def reconcile_withdrawal(name: str, gl: str) -> str | None:
    """Match one withdrawal line with the one entry carrying its receipt (or "CHG-" key) for
    exactly its amount. No entry, another amount or two candidates: left for staff."""
    from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import reconcile_vouchers

    # row lock: the result's job and the sweep may reach the same line together
    bt = frappe.db.get_value(
        BT, name, ["name", "reference_number", "transaction_id", "unallocated_amount", "docstatus"],
        as_dict=True, for_update=True,
    )
    if not bt or bt.docstatus != 1 or flt(bt.unallocated_amount) <= 0.005:
        return None
    key = cstr(bt.reference_number or bt.transaction_id).strip()
    if not key:
        return None
    found = entries_for(key, gl)
    exact = [e for e in found if abs(e[2] - flt(bt.unallocated_amount)) <= 0.005]
    if len(exact) != 1:
        if found:
            listed = ", ".join(f"{doctype} {entry} ({amount})" for doctype, entry, amount in found)
            log_match(
                "Ambiguous", "Bank Reconciliation", once=True, reference_doctype=BT, reference_name=bt.name,
                receipt=key, payment_entry=found[0][1], amount=bt.unallocated_amount,
                details=f"{key}: statement {flt(bt.unallocated_amount)}; "
                f"{'several entries match' if exact else 'no entry has that amount'}: {listed}. Reconcile it by hand.",
            )
        return None
    doctype, entry, amount = exact[0]
    # reconcile_vouchers checks the session user's write permission; result jobs run as the
    # callback's user
    with as_user("Administrator"):
        reconcile_vouchers(bt.name, json.dumps([{"payment_doctype": doctype, "payment_name": entry, "amount": amount}]))
    return entry


# ---------------------------------------------------------------- scheduler
def sweep_missing(limit: int = 200) -> None:
    """Lines for Paid payouts that have none yet (a job failed, the switch was off, a B2C
    charge the balances could not show yet)."""
    if not enabled("create_bank_transactions") or not erpnext_installed():
        return
    since = add_days(nowdate(), -suite_int("matching_lookback_days", 45))
    for doctype in (B2C, B2B):
        meta = frappe.get_meta(doctype)
        if not all(meta.has_field(f) for f in LINK_FIELDS):
            continue  # not migrated yet
        missing = {"bank_transaction": ["is", "not set"]}
        if enabled("book_payout_charges"):
            missing["charge_bank_transaction"] = ["is", "not set"]
        rows = frappe.get_all(
            doctype,
            filters={"docstatus": ["in", list(fed_docstatus(doctype))], "status": "Paid",
                     "transaction_id": ["is", "set"], "creation": [">=", since]},
            or_filters=missing,
            fields=["name", "bank_transaction", *(("debit_party_charges",) if doctype == B2B else ())],
            order_by="creation desc",
            limit=limit,
        )
        for row in rows:
            if (row.bank_transaction and doctype == B2B and cstr(row.debit_party_charges).strip()
                    and not parse_charges(row.debit_party_charges)):
                continue  # Safaricom says nothing was charged; a charge it did not state is looked for again
            frappe.db.savepoint("mpesa_payout_bt")
            try:
                feed(doctype, row.name)
            except Exception:
                frappe.db.rollback(save_point="mpesa_payout_bt")
                frappe.log_error(
                    title=f"M-Pesa payout statement line failed for {row.name}"[:140], message=frappe.get_traceback()
                )


def reconcile_run(limit: int = 300) -> None:
    """Match every unmatched withdrawal on an M-Pesa account with its entry (the suite's
    lines, and those Astral's feed or a statement import made)."""
    if not reconciling() or not erpnext_installed():
        return
    since = add_days(nowdate(), -suite_int("matching_lookback_days", 45))
    for bank_account in sorted(mpesa_bank_accounts()):
        gl = frappe.db.get_value("Bank Account", bank_account, "account")
        names = frappe.get_all(
            BT,
            filters={
                "bank_account": bank_account,
                "docstatus": 1,
                "withdrawal": [">", 0],
                "unallocated_amount": [">", 0],
                "date": [">=", since],
            },
            pluck="name",
            order_by="date desc, creation desc",
            limit=limit,
        )
        for name in names:
            frappe.db.savepoint("mpesa_payout_recon")
            try:
                reconcile_withdrawal(name, gl)
            except Exception:
                frappe.db.rollback(save_point="mpesa_payout_recon")
                frappe.log_error(title=f"M-Pesa reconciliation failed for {name}"[:140], message=frappe.get_traceback())
