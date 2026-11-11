"""Merchants and casual payees paid straight from an Expense Claim ("petty cash without cash").

Staff often pay a claim's expenses from their own phones or petty cash (a till, a county paybill,
parking, fuel, casual workers) and are reimbursed later. An Expense Claim may instead list who is
to be paid (its M-Pesa Payees table, M-Pesa Claim Payee rows) and pay them directly: a till by Buy
Goods, a paybill with its account number, a bank account through the bank's paybill (B2B), or a
phone (B2C). The money goes to the merchant; the books stay a reimbursement of the claim: each
paid payout is booked as a Payment Entry to the claimant against the claim
(money_out.employee_documents), whoever received the money. Whatever the lines leave of what the
claim owes is reimbursed to the employee as usual.

Switched on by M-Pesa Suite Settings (allow_claim_payees) and per Expense Claim Type
(allow_mpesa_payees): every payee line names the expense line it pays, and that line's type must
allow payees (the claim's other lines may be of any type). Who does what:
- the claimant (or HR or Accounts) enters the lines while the claim is a draft;
- the claim's expense approver approves them with the claim (HRMS's approval and submit); the
  lines are frozen from then on;
- someone in PREPARE_ROLES presses Pay merchants and payees: one draft B2B request per merchant
  line and one draft B2C Payment Disbursement for all the phone lines (pay_payees, retry_payees,
  pay_claims for casual payees of several claims);
- an M-Pesa Payer sends each with Send via M-Pesa (with maker-checker on, one who did not prepare
  them).

These destinations are not approved M-Pesa accounts, so:
- never an employee's: the claimant's own or any employee's phone, M-Pesa account or bank account
  number (the Employee's, its Bank Accounts' and its M-Pesa accounts') is refused (an employee is
  paid by reimbursing the claim), and so is a site user's phone (staff with a login, an Employee or
  not; the sender's own: "your own phone");
- never staff under another number (round 4.1): when the name M-Pesa paid for a line, or paid
  before for its number, is the claimant's, its payout's preparer's or sender's, or (M-Pesa Suite
  Settings) any Employee's, the line is flagged a staff or self payment (staff_check Blocked), the
  M-Pesa Managers but those involved are alerted, and its destination is refused for claim payees
  until an M-Pesa Manager who was not involved clears it (clear_staff_flag);
- a per-payment and a per-day (per company) cap from M-Pesa Suite Settings, when above 0, except for
  a destination that is a supplier's approved Supplier M-Pesa Account (noted on the line);
- a payout of a line must be the approved line's: the same destination, its whole amount, the line
  not paid or on its way already, the claim still payable (check_b2b, check_b2c_row: on save, at
  the send and right before the money leaves);
- the name M-Pesa paid is compared with the name the claimant gave; a mismatch is commented on the
  claim and alerted to the Notify Role. A bank account is Not Verified at best: M-Pesa shows the
  bank's name (checked against M-Pesa Bank Paybill), never who holds the account. Phones show in
  full in those comments, alerts and logs, or masked (2547****0111) when M-Pesa Suite Settings says
  so (utils.privacy, round 4.3).

A till or paybill paid suggest_supplier_after times from claims is suggested as a Supplier M-Pesa
Account, approved once and then paid like any supplier.

Each line's status follows its payouts (sync_lines): Draft (no payout), Queued (a payout not sent
yet), In Flight, Paid, Failed (paid again with Retry failed payees), Cancelled (the claim was
rejected or cancelled, or an M-Pesa Manager settled a Draft or Failed line outside M-Pesa: settle_line).
"""

import contextlib
import csv
import inspect
import re

import frappe
from frappe.utils import cint, cstr, flt, now_datetime, nowdate

from ..daraja import apis
from ..daraja import money_out as daraja
from ..daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT
from ..permissions import ADMIN_ROLES, PREPARE_ROLES, require, require_named_user
from ..utils import privacy
from ..utils.phone import FORMATS, is_safaricom, network_of, normalize_msisdn, payable_msisdn, to_local
from . import owed
from . import party_accounts as pa

SUITE = "M-Pesa Suite Settings"
CLAIM = "Expense Claim"
CLAIM_TYPE = "Expense Claim Type"
LINE = "M-Pesa Claim Payee"
TABLE = "mpesa_payees"
TYPE_FLAG = "allow_mpesa_payees"
B2B = "Mpesa B2B Payment Request"
B2C = "B2C Payment Disbursement"
B2C_ROW = "B2C Payment Disbursement Reference"
B2C_REQUEST = "B2C Disbursement Request"
SUPPLIER_ACCOUNT = "Supplier M-Pesa Account"
EMPLOYEE_ACCOUNT = "Employee M-Pesa Account"
BANK_PAYBILL = "M-Pesa Bank Paybill"

# What a line pays: the payee account types' labels (party_accounts after round 4's payee accounts)
TILL = "Till (Buy Goods)"
PAYBILL = "Paybill"
BANK = "Bank Account (via Bank Paybill)"
PHONE = "Phone (B2C)"
OLD_BANK = "Bank Paybill (B2B)"  # an Employee M-Pesa Account's bank type before round 4
TYPES = (TILL, PAYBILL, BANK, PHONE)
B2B_TYPES = (TILL, PAYBILL, BANK)
COMMAND_FOR = {TILL: daraja.B2B_BUY_GOODS, PAYBILL: daraja.B2B_PAYBILL, BANK: daraja.B2B_PAYBILL}
WHAT = {PHONE: "phone", TILL: "till", PAYBILL: "paybill and account number", BANK: "bank account"}

# A line's status
DRAFT, QUEUED, IN_FLIGHT, PAID, FAILED, CANCELLED = "Draft", "Queued", "In Flight", "Paid", "Failed", "Cancelled"
# Which payout decides a line when it has several: the one furthest on its way
RANK = {PAID: 4, IN_FLIGHT: 3, QUEUED: 2, FAILED: 1}
# Written by the server from the line's payouts; a save after submit keeps what the database holds
RESULT_FIELDS = ("status", "payout_doctype", "payout_name", "receipt", "paid_on", "sent_on", "error_message",
                 "receiver_name", "name_matches", "name_note", "supplier_account", "name_verdict", "sent_by",
                 "staff_check", "staff_note", "staff_involved")
# The name check's verdict (name_verdict): a bank account is never verified, M-Pesa shows the bank's name
MATCHES, NO_MATCH, NOT_VERIFIED = "Matches", "Does Not Match", "Not Verified"
VERDICT = {True: MATCHES, False: NO_MATCH, None: NOT_VERIFIED}
# A destination M-Pesa paid under a staff member's name (staff_check): refused until an M-Pesa Manager clears it
BLOCKED, CLEARED = "Blocked", "Cleared"
# Kept from the claim's readers (level 2 on the line, which no role reads): the logins a staff flag is
# about name a colleague
PRIVATE_FIELDS = ("staff_involved",)
LINE_FIELDS = ("name", "idx", "parent", "parenttype", "payee_type", "payee_name", "destination", "expense_detail",
               "expense_type", "bank_paybill", "shortcode", "account_number", "phone", "amount",
               "destination_key", *RESULT_FIELDS)
# Pasted or uploaded casual payees: at most this many rows at a time
PASTE_LIMIT = 1000
# The number M-Pesa puts before a name: "522522 - KCB Paybill AC", "2547****0111 - JANE" (also after
# an en-dash or em-dash); party_accounts.number_prefix after round 4's payee accounts
_NUMBER_PREFIX = re.compile(r"^\s*([0-9][0-9*xX]*)\s*[-–—]\s*")


def settings() -> frappe._dict:
    """M-Pesa Suite Settings for claim payees: the switch, the two caps (0 = no cap) and when to
    suggest a supplier account (0 = never)."""
    def get(field):
        return frappe.db.get_single_value(SUITE, field)

    after = get("suggest_supplier_after")
    employee_names = get("claim_payee_employee_names")
    return frappe._dict(
        allow=bool(cint(get("allow_claim_payees"))),
        max_amount=flt(get("claim_payee_max_amount")),
        daily_cap=flt(get("claim_payee_daily_cap")),
        suggest_after=3 if after in (None, "") else cint(after),
        # on unless switched off (a site not yet migrated reads it as on)
        employee_names=True if employee_names in (None, "") else bool(cint(employee_names)),
    )


def money(value) -> str:
    return owed.money(value)


# ---------------------------------------------------------------- where a line pays
def _paybill_number(value) -> str:
    return re.sub(r"[\s-]", "", cstr(value)).upper()


def bank_number(value) -> str:
    """A bank account number as the payee accounts keep it: letters and digits, upper-cased."""
    return re.sub(r"[^A-Za-z0-9]", "", cstr(value)).upper()


def destination_key(payee_type, shortcode=None, account_number=None, phone=None) -> str | None:
    """Where a line (or a payee account) pays, in the payee accounts' format
    (party_accounts.destination_key after round 4's payee accounts): 'B2C:2547...', 'TILL:835811',
    'PB:522522:0123456789'. A paybill's account number is upper-cased without spaces and dashes, a
    bank's letters and digits only, so the same destination typed two ways is one key."""
    if payee_type == PHONE:
        msisdn = normalize_msisdn(phone)
        return f"B2C:{msisdn}" if msisdn else None
    code = cstr(shortcode).strip()
    if not code:
        return None
    if payee_type == TILL:
        return f"TILL:{code}"
    if payee_type == PAYBILL:
        return f"PB:{code}:{_paybill_number(account_number)}"
    if payee_type in (BANK, OLD_BANK):
        return f"PB:{code}:{bank_number(account_number)}"
    return None


def line_key(line) -> str | None:
    return destination_key(line.get("payee_type"), line.get("shortcode"), line.get("account_number"),
                           line.get("phone"))


def account_key(acc) -> str | None:
    """A Supplier or Employee M-Pesa Account's key: its own destination_key once it has one."""
    if cstr(acc.get("destination_key")).strip():
        return acc.destination_key
    return destination_key(acc.get("account_type"), acc.get("shortcode"), acc.get("account_number"), acc.get("phone"))


def describe(line, masked: bool = False) -> str:
    """How a line's destination reads: '835811', '522522 / 0123456789', '0722 000 111' (``masked``:
    the phone as the send summary shows it, for comments, alerts and logs: 254722000111, or
    2547****0111 when M-Pesa Suite Settings masks numbers, utils.privacy)."""
    if line.get("payee_type") == PHONE:
        if masked:
            return privacy.phone(line.get("phone"))
        local = to_local(line.get("phone"))
        return f"{local[:4]} {local[4:7]} {local[7:]}" if local else cstr(line.get("phone"))
    code = cstr(line.get("shortcode")).strip()
    account = cstr(line.get("account_number")).strip()
    return f"{code} / {account}" if account and line.get("payee_type") != TILL else code


# ---------------------------------------------------------------- the claim's lines (validate)
def validate_claim(doc, method=None) -> None:
    """Expense Claim validate (hooks.py doc_events), on every save of a draft and at submit, which
    is the approval: the payee lines cleaned and checked (see the module docstring). A rejected
    claim pays no payee: its lines are left as they are and never paid."""
    rows = doc.get(TABLE) or []
    if not rows:
        return
    at_submit = cint(doc.docstatus) == 1
    if not at_submit or getattr(doc, "_action", None) == "submit":
        clear_results(rows)  # a draft and its approval paid nothing: results an amend copied (or typed) go
    if doc.get("approval_status") == "Rejected":
        if at_submit:
            for row in rows:
                row.status = CANCELLED
        return
    if at_submit and getattr(doc, "_action", None) == "submit":
        # the claim's approval approves its payees' unverified tills, paybills, bank accounts and
        # phones: a named approver, never the shared Administrator login (decision 1)
        require_named_user()
    s = settings()
    if not s.allow:
        frappe.throw("Paying merchants and payees from an Expense Claim is switched off (M-Pesa Suite Settings): "
                     "remove the payee lines, or ask an M-Pesa Manager to switch it on.")
    if cint(doc.get("is_paid")):
        frappe.throw("This claim is marked Is Paid (paid at once from its Mode of Payment), so it pays no payee by "
                     "M-Pesa: untick Is Paid or remove the payee lines.")
    currency = claim_currency(doc)
    if currency != "KES":
        frappe.throw(f"This claim is in {currency}. M-Pesa pays KES only: remove the payee lines.")
    expenses = {r.name: r for r in doc.get("expenses") or []}
    allowed = types_allowing_payees({r.expense_type for r in expenses.values()})
    found = employee_destinations()
    own = own_numbers()
    warnings, per_expense, seen = [], {}, {}
    capped_total = capped_phones = 0.0
    names = mpesa_names(payable_msisdn(r.get("phone")) for r in rows if r.get("payee_type") == PHONE)
    for row in rows:
        clean_line(row, own, warnings, names)
        expense = _expense_line(row, expenses, at_submit, warnings, doc.get("amended_from"))
        if expense:
            row.expense_type = expense.expense_type
            if expense.expense_type not in allowed:
                frappe.throw(f"Row #{row.idx}: {expense.expense_type} is not paid to merchants or payees. Tick Paid "
                             "to Merchants or Casual Payees by M-Pesa on the Expense Claim Type, or pay this expense "
                             "to the employee.")
            per_expense[expense.name] = per_expense.get(expense.name, 0.0) + flt(row.amount)
        problem = employee_problem(row, doc.employee, found)
        if problem:
            frappe.throw(problem)
        row.supplier_account = approved_supplier_account(row)
        if not row.supplier_account:
            cap_problem = amount_cap_problem(row, s)
            if cap_problem:
                frappe.throw(cap_problem)
            capped_total += flt(row.amount)
            if row.payee_type == PHONE:
                capped_phones += flt(row.amount)
        seen.setdefault(row.destination_key, []).append(row.idx)
        if not row.get("status"):
            row.status = DRAFT
    for name, total in per_expense.items():
        expense = expenses[name]
        limit = flt(expense.sanctioned_amount) if at_submit else flt(expense.sanctioned_amount) or flt(expense.amount)
        if total > limit + 0.001:
            frappe.throw(f"The payee lines for expense line #{expense.idx} ({expense.expense_type}) add up to KES "
                         f"{money(total)}, more than the line's KES {money(limit)}.")
    total, left = sum(flt(r.amount) for r in rows), claim_owed(doc, at_submit)
    if total > left + 0.001:
        frappe.throw(f"The payee lines add up to KES {money(total)}, more than the claim owes (KES {money(left)}). "
                     "Lower them: whatever they leave is reimbursed to the employee as usual.")
    if s.daily_cap and capped_phones > s.daily_cap:
        frappe.throw(f"The phone payees add up to KES {money(capped_phones)}, more than may be paid in one day to "
                     f"payees that are not approved accounts (KES {money(s.daily_cap)}, M-Pesa Suite Settings), "
                     "and a claim's phone payees are paid in one batch: split them over two claims.")
    if s.daily_cap and capped_total > s.daily_cap:
        warnings.append(f"The payee lines add up to KES {money(capped_total)}, more than may be paid in one day to "
                        f"payees that are not approved accounts (KES {money(s.daily_cap)}): some will be paid on a "
                        "later day.")
    for key, rows_of_key in seen.items():
        if key and len(rows_of_key) > 1:
            warnings.append(f"Rows {', '.join(f'#{i}' for i in rows_of_key)} pay the same destination: check that "
                            "this is not one payment entered twice.")
    if warnings:
        frappe.msgprint("<br>".join(warnings), title="Check the M-Pesa payees", indicator="orange")


def clear_results(rows) -> None:
    """A claim not submitted yet pays no line: each line starts as a Draft with no payout or
    result. An amend copies every field of the cancelled claim's lines (Cancelled, its payouts and
    receipts), and the API could set them on a draft."""
    for row in rows:
        for field in RESULT_FIELDS:
            setattr(row, field, None)
        row.status = DRAFT
        row.name_matches = 0


def clean_line(row, own=None, warnings=None, names=None) -> None:
    """One line's details for its type: a phone kept as 2547..., a till or paybill as 5 to 7
    digits (a paybill with its account number), a bank account at an enabled M-Pesa Bank Paybill
    (its paybill copied from the bank, never typed), and never one of this company's own numbers.
    A phone M-Pesa paid before takes the name M-Pesa paid it under, never a typed one (round 4.3,
    mpesa_names; ``names``: those of the claim's phones, read once)."""
    row.payee_name = cstr(row.get("payee_name")).strip()
    row.mpesa_name = row.mpesa_name_from = None
    if row.get("payee_type") not in TYPES:
        frappe.throw(f"Row #{row.idx}: Pay To must be {', '.join(TYPES[:-1])} or {TYPES[-1]}.")
    if row.payee_type == PHONE:
        # the B2C row's rule (utils.phone.payable_msisdn): a claim never approves a number its payout refuses
        msisdn = payable_msisdn(row.get("phone"))
        if not msisdn:
            frappe.throw(f"Row #{row.idx}: {cstr(row.get('phone')) or 'no phone'} is not a Kenyan mobile number "
                         f"M-Pesa can pay ({FORMATS}).")
        row.phone = msisdn
        row.shortcode = row.account_number = row.bank_paybill = None
        known = (names if names is not None else mpesa_names([msisdn])).get(msisdn)
        if known:
            row.payee_name = row.mpesa_name = known.name
            row.mpesa_name_from = name_from(known)
        if not is_safaricom(msisdn) and warnings is not None:
            # not refused: a number moved from another network keeps its old prefix
            warnings.append(f"Row #{row.idx}: {to_local(msisdn)} does not start like a Safaricom number, and B2C "
                            "pays M-Pesa lines only.")
    elif row.payee_type == BANK:
        _clean_bank(row)
    else:
        row.shortcode = cstr(row.get("shortcode")).strip()
        row.phone = row.bank_paybill = None
        if not row.shortcode.isdigit() or not 5 <= len(row.shortcode) <= 7:
            frappe.throw(f"Row #{row.idx}: the Till / Paybill Number must be 5 to 7 digits.")
        if row.payee_type == TILL:
            row.account_number = None
        else:
            row.account_number = cstr(row.get("account_number")).strip() or None
            if not row.account_number:
                frappe.throw(f"Row #{row.idx}: a paybill needs the Account Number the merchant gave.")
            bank = _bank_with_paybill(row.shortcode)
            if bank and not cint(bank.enabled):
                # a bank paybill nobody has checked yet: not paid as a merchant's paybill either
                frappe.throw(f"Row #{row.idx}: {row.shortcode} is {bank.institution_name}'s bank paybill, switched off "
                             "in M-Pesa Bank Paybills: an M-Pesa Manager checks its paybill and enables it, then pick "
                             f"Pay To Bank Account (via Bank Paybill) and {bank.institution_name}.")
            if bank:
                # the bank's account number rule and Safaricom's name for the bank apply to it
                frappe.throw(f"Row #{row.idx}: {row.shortcode} is {bank.institution_name}'s bank paybill: pick Pay "
                             f"To Bank Account (via Bank Paybill) and {bank.institution_name}.")
        if len(cstr(row.account_number)) > 20:
            frappe.throw(f"Row #{row.idx}: the Account Number is longer than M-Pesa allows (20 characters).")
    if not row.payee_name:
        frappe.throw(f"Row #{row.idx}: enter the Payee Name, as the claimant gives it.")
    if row.payee_type != PHONE and row.shortcode in (own if own is not None else own_numbers()):
        frappe.throw(f"Row #{row.idx}: {row.shortcode} is one of this company's own M-Pesa numbers.")
    try:
        daraja.whole_shillings(row.get("amount"))
    except ValueError as e:
        frappe.throw(f"Row #{row.idx}: {e}")
    if row.payee_type == PHONE and not B2C_MIN_AMOUNT <= flt(row.amount) <= B2C_MAX_AMOUNT:
        frappe.throw(f"Row #{row.idx}: M-Pesa B2C pays KES {B2C_MIN_AMOUNT} to {B2C_MAX_AMOUNT:,} to a phone.")
    row.destination = describe(row)
    row.destination_key = line_key(row)


def _clean_bank(row) -> None:
    if not cstr(row.get("bank_paybill")).strip():
        frappe.throw(f"Row #{row.idx}: pick the Bank the account is at.")
    bank = frappe.db.get_value(BANK_PAYBILL, row.bank_paybill,
                               ["name", "institution_name", "paybill", "enabled", "account_number_pattern",
                                "account_number_rule"], as_dict=True)
    if not bank:
        frappe.throw(f"Row #{row.idx}: M-Pesa Bank Paybill {row.bank_paybill} not found.")
    if not cint(bank.enabled):
        frappe.throw(f"Row #{row.idx}: {bank.institution_name} is switched off in M-Pesa Bank Paybills: an M-Pesa "
                     "Manager checks its paybill and enables it first.")
    row.shortcode = cstr(bank.paybill).strip()
    row.phone = None
    number = re.sub(r"[\s-]", "", cstr(row.get("account_number"))).upper()
    if not number:
        frappe.throw(f"Row #{row.idx}: enter the account number at {bank.institution_name}.")
    if not number.isalnum():
        frappe.throw(f"Row #{row.idx}: a bank account number has letters and digits only.")
    pattern = cstr(bank.get("account_number_pattern")).strip()
    if pattern and not _fits(pattern, number):
        rule = cstr(bank.get("account_number_rule")).strip()
        frappe.throw(f"Row #{row.idx}: {number} does not look like a {bank.institution_name} account number"
                     + (f" ({rule})" if rule else "") + ".")
    row.account_number = number


def _fits(pattern, value) -> bool:
    try:
        return bool(re.fullmatch(pattern, value))
    except re.error:
        return True  # a broken pattern refuses nothing


def _bank_with_paybill(shortcode):
    """The M-Pesa Bank Paybill row with this paybill, enabled or not (a shipped row is disabled
    until an M-Pesa Manager checks it), if the list exists on the site."""
    if not frappe.db.exists("DocType", BANK_PAYBILL):
        return None
    return frappe.db.get_value(BANK_PAYBILL, {"paybill": shortcode}, ["name", "institution_name", "enabled"],
                               as_dict=True)


def _expense_line(row, expenses: dict, at_submit: bool, warnings: list, amended_from=None):
    """The claim's expense line the payee line pays: the claim's only one when none is picked. An
    amended claim's line names the cancelled claim's expense line: the same line of this claim
    (its row number and type) is taken; one that is gone is picked again before the approval."""
    picked = cstr(row.get("expense_detail")).strip()
    if picked and picked not in expenses:
        row.expense_detail = picked = _amended_expense(picked, expenses, amended_from)
        if not picked and len(expenses) != 1:
            gone = f"Row #{row.idx}: its expense line is no longer on the claim: pick its Expense Line again"
            if at_submit:
                frappe.throw(f"{gone}.")
            warnings.append(f"{gone} before the claim is approved.")
            return None
    if picked:
        return expenses[picked]
    if len(expenses) == 1:
        row.expense_detail = next(iter(expenses))
        return expenses[row.expense_detail]
    if at_submit:
        frappe.throw(f"Row #{row.idx}: pick the Expense Line this payee is paid for.")
    warnings.append(f"Row #{row.idx}: pick the Expense Line this payee is paid for before the claim is approved.")
    return None


def _amended_expense(picked: str, expenses: dict, amended_from) -> str | None:
    """This claim's expense line that is the one ``picked`` of the claim it amends (same row number
    and type), or None."""
    if not cstr(amended_from).strip():
        return None
    old = frappe.db.get_value("Expense Claim Detail", {"name": picked, "parent": amended_from},
                              ["idx", "expense_type"], as_dict=True)
    if not old:
        return None
    same = [r.name for r in expenses.values() if cint(r.idx) == cint(old.idx) and r.expense_type == old.expense_type]
    return same[0] if len(same) == 1 else None


def claim_currency(doc) -> str:
    """The claim's currency: v16's own field, else its company's (a v15 claim has none), as
    money_out.employee_documents reads it."""
    return (doc.get("currency")
            or (doc.get("company") and frappe.db.get_value("Company", doc.get("company"), "default_currency"))
            or "KES")


def types_allowing_payees(types) -> set:
    """Of these Expense Claim Types, the ones whose lines may be paid to merchants or payees."""
    types = sorted({t for t in types if t})
    if not types or not frappe.db.has_column(CLAIM_TYPE, TYPE_FLAG):
        return set()
    return set(frappe.get_all(CLAIM_TYPE, filters={"name": ["in", types], TYPE_FLAG: 1}, pluck="name"))


def claim_owed(doc, at_submit: bool = True) -> float:
    """What the claim owes: HRMS's grand total (sanctioned, plus taxes, less advances) less what was
    reimbursed. A draft's lines may not be sanctioned yet: their claimed amounts count then."""
    total = flt(doc.get("grand_total"))
    if not at_submit and not total:
        lines = doc.get("expenses") or []
        sanctioned = sum(flt(r.get("sanctioned_amount")) or flt(r.get("amount")) for r in lines)
        total = sanctioned + flt(doc.get("total_taxes_and_charges")) - flt(doc.get("total_advance_amount"))
    return max(0.0, flt(total - flt(doc.get("total_amount_reimbursed")), 2))


def own_numbers() -> set:
    """This company's own M-Pesa numbers (every Mpesa Settings' shortcode and till)."""
    own = frappe.get_all("Mpesa Settings", pluck="business_shortcode") + frappe.get_all("Mpesa Settings",
                                                                                     pluck="till_number")
    return {cstr(s).strip() for s in own if cstr(s).strip()}


def amount_cap_problem(row, s) -> str | None:
    """The per-payment cap (M-Pesa Suite Settings; 0 = none) on a destination that is not an
    approved supplier account."""
    if s.max_amount and flt(row.amount) > s.max_amount + 0.001:
        return (f"Row #{row.idx}: KES {money(row.amount)} is more than may be paid at once to a payee that is not an "
                f"approved account (KES {money(s.max_amount)}, M-Pesa Suite Settings). Pay it to an approved Supplier "
                "M-Pesa Account, or reimburse the employee.")
    return None


# ---------------------------------------------------------------- never an employee's destination
def employee_destinations() -> frappe._dict:
    """Every employee's destinations: their phones (Employee cell number and Employee M-Pesa
    Accounts, approved or not) by key, and their bank account numbers (the Employee's, its ERPNext
    Bank Accounts' and their M-Pesa accounts'), each with the employee; the phones of the site's
    enabled users (staff with a login, an Employee or not: Mobile No and Phone) by key, each with
    the user (an employee's own login counts as the employee's); and the destinations blocked for
    claim payees (blocked_destinations)."""
    found = frappe._dict(destinations={}, bank_numbers={}, users={}, blocked={})
    meta = frappe.get_meta("Employee")
    fields = ["name"] + [f for f in ("cell_number", "bank_ac_no", "user_id") if meta.has_field(f)]
    employee_of = {}
    for employee in frappe.get_all("Employee", fields=fields):
        key = destination_key(PHONE, phone=employee.get("cell_number"))
        if key:
            found.destinations.setdefault(key, employee.name)
        number = bank_number(employee.get("bank_ac_no"))
        if number:
            found.bank_numbers.setdefault(number, employee.name)
        if cstr(employee.get("user_id")).strip():
            employee_of.setdefault(employee.user_id, employee.name)
    if frappe.db.has_column("Bank Account", "party_type"):
        # an employee's bank account kept on ERPNext's Bank Account (HRMS pays salaries to it)
        for acc in frappe.get_all("Bank Account", filters={"party_type": "Employee", "party": ["is", "set"]},
                                  fields=["party", "bank_account_no", "iban"]):
            for number in (bank_number(acc.get("bank_account_no")), bank_number(acc.get("iban"))):
                if number:
                    found.bank_numbers.setdefault(number, acc.party)
    columns = ["name", "employee", "account_type", "phone", "shortcode", "account_number"]
    if frappe.db.has_column(EMPLOYEE_ACCOUNT, "destination_key"):
        columns.append("destination_key")
    for acc in frappe.get_all(EMPLOYEE_ACCOUNT, filters={"docstatus": ["<", 2]}, fields=columns):
        key = account_key(acc)
        if key:
            found.destinations.setdefault(key, acc.employee)
        if acc.get("account_type") in (BANK, OLD_BANK) and bank_number(acc.get("account_number")):
            found.bank_numbers.setdefault(bank_number(acc.account_number), acc.employee)
    user_meta = frappe.get_meta("User")
    phones = [f for f in ("mobile_no", "phone") if user_meta.has_field(f)]
    for user in frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, fields=["name", *phones]):
        for number in (user.get(f) for f in phones):
            key = destination_key(PHONE, phone=number)
            if not key:
                continue
            found.users.setdefault(key, user.name)
            if employee_of.get(user.name):
                found.destinations.setdefault(key, employee_of[user.name])
    found.blocked = blocked_destinations()
    return found


def blocked_destinations(keys=None) -> dict:
    """Destinations blocked for claim payees: {key: the flagged line (parent, idx)}, a line M-Pesa
    paid under a staff member's name (staff_check Blocked) that no M-Pesa Manager cleared yet.
    ``keys``: only these."""
    if not frappe.db.has_column(LINE, "staff_check"):
        return {}
    filters = {"parenttype": CLAIM, "staff_check": BLOCKED}
    if keys is not None:
        keys = sorted({k for k in keys if k})
        if not keys:
            return {}
        filters["destination_key"] = ["in", keys]
    found = {}
    for row in frappe.get_all(LINE, filters=filters, fields=["destination_key", "parent", "idx"],
                              order_by="modified desc"):
        if row.destination_key:
            found.setdefault(row.destination_key, row)
    return found


def employee_problem(row, claimant, found, masked: bool = False) -> str | None:
    """Why the line may not pay its destination: it is the session user's own phone, the claimant's
    own or another employee's destination (who is not named: what an employee is paid is not
    everyone's to see), a site user's phone, or blocked for claim payees (a staff or self payment
    found earlier). ``masked``: a phone shown as the send summary shows it."""
    key = row.get("destination_key") or line_key(row)
    users = found.get("users") or {}
    who = found.destinations.get(key)
    user = users.get(key)
    if not who and row.get("payee_type") == BANK:
        who = found.bank_numbers.get(bank_number(row.get("account_number")))
    shown = f"{describe(row, masked)} is"
    if not who and row.get("payee_type") == PAYBILL and bank_number(row.get("account_number")):
        # a paybill that is a bank's (one not on M-Pesa Bank Paybills, or a Sacco's) credits that account
        who = found.bank_numbers.get(bank_number(row.get("account_number")))
        if who:
            whose = "the claimant's own" if who == claimant else "an employee's"
            return (f"Row #{row.idx}: the account number {cstr(row.get('account_number')).strip()} is {whose} bank "
                    "account number. An employee is paid by reimbursing the claim, never as a payee.")
    if not (who or user) and row.get("payee_type") in (PAYBILL, BANK):
        # a bank (or a paybill) that takes a phone number as the account credits that phone's owner
        msisdn = normalize_msisdn(row.get("account_number"))
        who = found.destinations.get(f"B2C:{msisdn}") if msisdn else None
        user = users.get(f"B2C:{msisdn}") if msisdn else None
        number = privacy.phone(msisdn) if masked and msisdn else cstr(row.get("account_number")).strip()
        shown = f"the account number {number} is"
        what = "phone number"
    else:
        what = WHAT.get(row.get("payee_type"), "destination")
    if user and user == frappe.session.user:
        return f"Row #{row.idx}: {shown} your own {what}. Staff are paid by reimbursing their claims, never as payees."
    if who:
        whose = "the claimant's own" if who == claimant else "an employee's"
        return f"Row #{row.idx}: {shown} {whose} {what}. An employee is paid by reimbursing the claim, never as a payee."
    if user:
        return (f"Row #{row.idx}: {shown} the {what} of a user of this site (staff). Staff are paid by reimbursing "
                "their claims, never as payees.")
    flagged = (found.get("blocked") or {}).get(key)
    if flagged:
        return (f"Row #{row.idx}: {describe(row, masked)} is blocked for claim payees: M-Pesa paid it under a staff "
                f"member's name ({flagged.parent}, payee row #{flagged.idx}). An M-Pesa Manager checks it and, if it "
                f"is no staff member's, clears it with Clear Staff Flag on {flagged.parent}.")
    return None


# ---------------------------------------------------------------- approved supplier destinations
def approved_supplier_account(line) -> str | None:
    """A supplier's approved Supplier M-Pesa Account with this destination that can be paid now:
    the payee caps do not apply to it."""
    key = line.get("destination_key") or line_key(line)
    if not key:
        return None
    if frappe.db.has_column(SUPPLIER_ACCOUNT, "destination_key"):
        names = frappe.get_all(SUPPLIER_ACCOUNT, filters={"destination_key": key, "docstatus": 1}, pluck="name")
    elif line.get("payee_type") in B2B_TYPES:
        rows = frappe.get_all(SUPPLIER_ACCOUNT, filters={"shortcode": line.get("shortcode"), "docstatus": 1},
                              fields=["name", "account_type", "shortcode", "account_number"])
        names = [r.name for r in rows if account_key(r) == key]
    else:
        names = []
    for name in names:
        acc = pa.get_account(SUPPLIER_ACCOUNT, name)
        if acc and not pa.usable_problem(acc, None, SUPPLIER_ACCOUNT):
            return name
    return None


# ---------------------------------------------------------------- after submit
def keep_results(doc, method=None) -> None:
    """Expense Claim before_update_after_submit: a save after submit (a form opened before a
    result, the API) writes back what the database holds for the lines' status and result: they
    are the server's, and the double-payment checks read them."""
    rows = doc.get(TABLE) or []
    if not rows:
        return
    current = {r.name: r for r in frappe.get_all(LINE, filters={"parent": doc.name, "parenttype": CLAIM},
                                                 fields=["name", *RESULT_FIELDS])}
    for row in rows:
        stored = current.get(row.name)
        if stored:
            for field in RESULT_FIELDS:
                setattr(row, field, stored.get(field))


def on_cancel_claim(doc, method=None) -> None:
    """Expense Claim on_cancel: its lines are not paid any more (the cancel was refused while a
    payout of the claim was paid or on its way: money_out.employee_documents.before_cancel). A
    draft payout left for a line cannot be sent: the claim is no longer payable. Its failed payouts
    do not hold the cancel either (employee_documents.ignore_payout_links): a wrong phone or till
    is corrected by cancelling and amending the claim."""
    from .employee_documents import ignore_payout_links

    ignore_payout_links(doc)
    for row in doc.get(TABLE) or []:
        if row.get("status") not in (PAID, IN_FLIGHT):
            frappe.db.set_value(LINE, row.name, "status", CANCELLED, update_modified=False)


def lines_paid_or_on_their_way(claim: str) -> list:
    """The payouts of the claim's payee lines that paid it or may have (Paid or In Flight, as the
    lines read them: a request cancelled while Initiated or Unknown too, b2b_state and b2c_state).
    money_out.employee_documents.before_cancel refuses the claim's cancel while there is one: its
    amended copy would start its lines afresh and pay them again."""
    if not paid_in_parts(CLAIM, claim):
        return []
    payouts = payouts_of([r.name for r in read_lines(claim)])
    return list(dict.fromkeys(p.name for p in payouts if p.state in (PAID, IN_FLIGHT)))


# ---------------------------------------------------------------- a line's payouts and status
def _locking(for_update: bool) -> str:
    return "FOR UPDATE" if for_update and frappe.db.db_type == "mariadb" else ""


def read_lines(claim: str, for_update: bool = False) -> list:
    """The claim's payee lines (a locking read when making payouts: one made meanwhile is seen)."""
    return frappe.db.sql(
        f"""SELECT {", ".join(f"`{f}`" for f in LINE_FIELDS)} FROM `tab{LINE}`
        WHERE parent = %(claim)s AND parenttype = %(parenttype)s AND parentfield = %(field)s
        ORDER BY idx {_locking(for_update)}""",
        {"claim": claim, "parenttype": CLAIM, "field": TABLE},
        as_dict=True,
    )


def b2b_state(docstatus, status) -> str | None:
    """A B2B request's say on its line: None when it never took money (cancelled unsent)."""
    status = status or "Not Initiated"
    if cint(docstatus) == 0:
        return QUEUED
    if cint(docstatus) == 1:
        return {"Not Initiated": QUEUED, "Initiated": IN_FLIGHT, "Unknown": IN_FLIGHT, "Paid": PAID,
                "Failed": FAILED}.get(status, IN_FLIGHT)
    # cancelled: only one that may have paid still counts
    return {"Paid": PAID, "Initiated": IN_FLIGHT, "Unknown": IN_FLIGHT}.get(status)


def b2c_state(parent_docstatus, payment_status, request=None, request_docstatus=None, request_status=None) -> str | None:
    """A B2C Payment Disbursement row's say on its line, from its payout (B2C Disbursement
    Request) once it has one, else from the row."""
    if request and cint(request_docstatus) in (1, 2):
        status = request_status or "Not Initiated"
        if cint(request_docstatus) == 2:
            return {"Paid": PAID, "Initiated": IN_FLIGHT, "Status Unknown": IN_FLIGHT}.get(status)
        return {"Paid": PAID, "Failed": FAILED}.get(status, IN_FLIGHT)
    if cint(parent_docstatus) == 0:
        return QUEUED
    if cint(parent_docstatus) == 1:
        return {"Paid": PAID, "Failed": FAILED}.get(payment_status, IN_FLIGHT)
    return None  # a cancelled disbursement whose row never got a payout


def payouts_of(lines, for_update: bool = False) -> list:
    """Every B2B request and B2C row that names one of ``lines``, with what it says about it."""
    names = [cstr(n) for n in lines if cstr(n)]
    if not names:
        return []
    found = []
    if frappe.db.has_column(B2B, "claim_payee"):
        for r in frappe.db.sql(
            f"""SELECT name, claim_payee, docstatus, status, transaction_id, receiver_public_name,
                   trans_completed_time, error_message, result_desc, creation
            FROM `tab{B2B}` WHERE claim_payee IN %(lines)s ORDER BY creation {_locking(for_update)}""",
            {"lines": tuple(names)},
            as_dict=True,
        ):
            state = b2b_state(r.docstatus, r.status)
            found.append(frappe._dict(
                line=r.claim_payee, doctype=B2B, name=r.name, state=state, creation=r.creation,
                receipt=r.transaction_id, receiver_name=r.receiver_public_name, paid_on=r.trans_completed_time,
                error=(r.result_desc or r.error_message) if state == FAILED else None,
                docstatus=cint(r.docstatus), status=r.status,
            ))
    if frappe.db.has_column(B2C_ROW, "claim_payee"):
        for r in frappe.db.sql(
            f"""SELECT ref.name AS row_name, ref.parent, ref.claim_payee, ref.payment_status, ref.creation,
                   d.docstatus AS parent_docstatus, req.name AS request, req.docstatus AS request_docstatus,
                   req.status AS request_status, req.transaction_id, req.receiver_public_name,
                   req.transaction_completed_datetime, req.error_message, req.result_desc
            FROM `tab{B2C_ROW}` ref
            JOIN `tab{B2C}` d ON d.name = ref.parent
            LEFT JOIN `tab{B2C_REQUEST}` req ON req.name = ref.b2c_disbursement_request
            WHERE ref.claim_payee IN %(lines)s AND ref.parenttype = %(parenttype)s
            ORDER BY ref.creation {_locking(for_update)}""",
            {"lines": tuple(names), "parenttype": B2C},
            as_dict=True,
        ):
            state = b2c_state(r.parent_docstatus, r.payment_status, r.request, r.request_docstatus, r.request_status)
            found.append(frappe._dict(
                line=r.claim_payee, doctype=B2C, name=r.parent, row=r.row_name, request=r.request, state=state,
                creation=r.creation, receipt=r.transaction_id, receiver_name=r.receiver_public_name,
                paid_on=r.transaction_completed_datetime,
                error=(r.result_desc or r.error_message or "Not sent") if state == FAILED else None,
                parent_docstatus=cint(r.parent_docstatus), request_docstatus=cint(r.request_docstatus),
            ))
    return found


def counted_in_left_to_pay(payout) -> bool:
    """Whether money_out.owed counts this payout of a line in what the claim has left to pay (or it
    lowered the claim's outstanding already, once booked): a B2B request on its way (drafts too) or
    Paid; a B2C row of a submitted disbursement (or a submitted payout) that did not fail, or Paid.
    A B2C draft is not counted until it is sent (owed.b2c_open lists submitted rows), nor a failed
    payout, nor one cancelled before it could take money."""
    if payout.doctype == B2B:
        status = payout.get("status") or "Not Initiated"
        return status == "Paid" or (cint(payout.get("docstatus")) < 2 and status in owed.OPEN)
    if payout.state == PAID:
        return True
    submitted = cint(payout.get("parent_docstatus")) == 1 or (
        bool(payout.get("request")) and cint(payout.get("request_docstatus")) == 1)
    return submitted and payout.state in (IN_FLIGHT, QUEUED)


def unpaid_lines(claim: str, exclude_lines=(), for_update: bool = False) -> list:
    """What a submitted Expense Claim's approved payee lines still hold of what it owes: each line
    that is not Cancelled and that no payout counted by money_out.owed represents
    (counted_in_left_to_pay): Draft, Failed, or Queued on a draft B2C disbursement. That money is
    the merchants' and casual payees', approved with the claim: owed.left_to_pay (and B2C's
    double-payout guards) take it off what is left for any other payout, so the employee is
    offered only the rest. ``exclude_lines``: the lines the payout being checked pays (its own
    share). As left_to_pay parts: _dict(kind="payee", name, line, amount, label)."""
    if not paid_in_parts(CLAIM, claim):
        return []  # no payee lines (most claims): nothing more to read
    if cint(frappe.db.get_value(CLAIM, claim, "docstatus")) != 1:
        return []  # a draft pays nobody yet; a cancelled claim pays nobody
    skip = {cstr(n).strip() for n in exclude_lines or () if cstr(n).strip()}
    rows = [r for r in read_lines(claim, for_update)
            if r.name not in skip and r.get("status") != CANCELLED and flt(r.amount) > owed.MIN]
    if not rows:
        return []
    payouts = payouts_of([r.name for r in rows], for_update)
    parts = []
    for row in rows:
        if any(counted_in_left_to_pay(p) for p in payouts if p.line == row.name):
            continue
        parts.append(frappe._dict(
            kind="payee", name=row.name, line=row.name, amount=flt(row.amount, 2),
            label=f"payee row #{row.idx} ({row.payee_name}) {money(row.amount)} not paid yet"))
    return parts


def deciding_payout(payouts):
    """The payout that decides a line: the one furthest on its way, the latest of those."""
    live = [p for p in payouts if p.state]
    if not live:
        return None
    return max(live, key=lambda p: (RANK[p.state], cstr(p.creation)))


def line_values(line, payouts, claim_docstatus) -> dict:
    """A line's status and result from its payouts (pure). A Cancelled line stays Cancelled, with its
    note, while no payout of it is live (settle_line: settled outside M-Pesa after its payout failed)."""
    best = deciding_payout(payouts)
    settled = line.get("status") == CANCELLED and (not best or best.state == FAILED)
    status = CANCELLED if settled else (best.state if best else DRAFT)
    if cint(claim_docstatus) == 2 and status not in (PAID, IN_FLIGHT):
        status = CANCELLED
    values = {
        "status": status,
        "payout_doctype": best.doctype if best else None,
        "payout_name": best.name if best else None,
        "receipt": best.receipt if best and status == PAID else None,
        "paid_on": best.paid_on if best and status == PAID else None,
        "receiver_name": (cstr(best.receiver_name).strip() or None) if best and status == PAID else None,
        "error_message": line.get("error_message") if settled else (best.error if best and status == FAILED else None),
    }
    paid = sorted({p.name for p in payouts if p.state == PAID})
    if len(paid) > 1:
        values["error_message"] = (f"Paid {len(paid)} times: {', '.join(paid)}. Check with the payee and recover the "
                                   "extra payment.")
    return values


def sync_lines(claim: str, lines=None, for_update: bool = False) -> list:
    """Each payee line's status and result from its payouts; only what changed is written. A line
    newly paid, or whose payout's receiver name came later (a till's, from Safaricom's Transaction
    Status answer), has its name checked. Returns the lines as they are now."""
    rows = lines if lines is not None else read_lines(claim, for_update)
    if not rows:
        return []
    payouts = payouts_of([r.name for r in rows], for_update)
    docstatus = frappe.db.get_value(CLAIM, claim, "docstatus")
    cache = {}
    for row in rows:
        mine = [p for p in payouts if p.line == row.name]
        values = line_values(row, mine, docstatus)
        changed = {k: v for k, v in values.items() if cstr(row.get(k)) != cstr(v)}
        verdict = staff = best = None
        named = values["status"] == PAID and ("status" in changed or "receiver_name" in changed)
        if named:
            row.update(values)
            verdict, note = name_check(row, row.get("receiver_name"), _bank_name(row))
            changed.update(name_matches=1 if verdict else 0, name_verdict=VERDICT[verdict], name_note=note)
            best = deciding_payout(mine)
            staff = _staff_paid(claim, row, best, cache)
            if staff:
                changed.update(staff_check=BLOCKED, staff_note=staff.note, staff_involved=staff.involved)
        if changed:
            frappe.db.set_value(LINE, row.name, changed, update_modified=False)
            row.update(changed)
        if cstr(changed.get("error_message")).startswith("Paid "):
            pa.alert(f"{claim}: payee row #{row.idx} ({row.payee_name}): {changed['error_message']}",
                     frappe._dict(doctype=CLAIM, name=claim))
        if verdict is False:
            _name_alarm(claim, row)
        if staff:
            _staff_alarm(claim, row, staff.exclude)
        if named and row.get("supplier_account") and best:
            _record_supplier_account(row, best)
    return rows


def on_payout_result(doctype: str, name: str) -> None:
    """After a payout's result is recorded (money_out.b2b's Result, a B2C payout's row, or a
    Transaction Status answer that fills a till's receiver name later): the Expense Claim payee
    line it pays follows it. Safe to call again, and for any payout (one that pays no payee line is
    left alone). Never raises, but for a deadlock or a lock wait past MariaDB's limit: the caller
    records the result again (money_out.b2b.handle_result) or its job fails and is retried."""
    try:
        line = _line_of(doctype, name)
        if not line:
            return
        row = frappe.db.get_value(LINE, line, list(LINE_FIELDS), as_dict=True)
        if row and row.parenttype == CLAIM:
            sync_lines(row.parent, [row])
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        raise  # MariaDB rolled the transaction back: the caller must record the result again
    except Exception:
        frappe.log_error(title="M-Pesa claim payee result not recorded", message=f"{doctype} {name}\n"
                         f"{frappe.get_traceback()}")


def _line_of(doctype: str, name: str) -> str | None:
    if not name:
        return None
    if doctype == B2B:
        return frappe.db.has_column(B2B, "claim_payee") and frappe.db.get_value(B2B, name, "claim_payee") or None
    if doctype == B2C_REQUEST:
        name = frappe.db.get_value(B2C_REQUEST, name, "b2c_payment_disbursement_reference")
        doctype = B2C_ROW
    if doctype == B2C_ROW and name:
        return frappe.db.has_column(B2C_ROW, "claim_payee") and frappe.db.get_value(B2C_ROW, name, "claim_payee") or None
    return None


# ---------------------------------------------------------------- the name M-Pesa paid
def number_prefix(name) -> str | None:
    found = _NUMBER_PREFIX.match(cstr(name))
    return found.group(1) if found else None


def _same_number(prefix: str, line) -> bool:
    """The number in front of the name is the line's: its shortcode, or its phone (M-Pesa masks a
    phone's middle digits: the digits shown must agree)."""
    if line.get("payee_type") != PHONE:
        return prefix == cstr(line.get("shortcode")).strip()
    phone = cstr(line.get("phone")).strip()
    if prefix.isdigit():
        return normalize_msisdn(prefix) == phone
    head = re.match(r"[0-9]*", prefix).group(0)
    tail = re.search(r"[0-9]*$", prefix).group(0)
    if head.startswith("0"):
        head = "254" + head[1:]
    return phone.startswith(head) and phone.endswith(tail)


def name_check(line, received, bank_name=None) -> tuple:
    """(matches, note) for the name M-Pesa paid (pure): True, False, or None when it could not be
    verified (a till's result names no one; a bank account at best: M-Pesa shows the bank's name,
    never who holds the account, and a bank whose Safaricom name is not on file yet is checked by
    its number only). A phone number in the note shows as utils.privacy says (in full, or masked)."""
    received = cstr(received).strip()
    if not received:
        if line.get("payee_type") == TILL:
            return None, ("Name not checked (till): M-Pesa's result names no one. Safaricom's Transaction Status "
                          "answer fills it in later.")
        return None, "Name not checked: M-Pesa's result names no one."
    shown = masked_name(received)
    prefix = number_prefix(received)
    if prefix and not _same_number(prefix, line):
        expected = describe(line, masked=True) if line.get("payee_type") == PHONE else cstr(line.get("shortcode")).strip()
        paid = _masked_number(prefix)
        # 0110 and 0111 lines with the same last four digits mask alike
        hidden = " (they differ in the hidden digits: compare the full numbers)" if paid == expected else ""
        return False, f"M-Pesa paid {shown!r}: number {paid}, not {expected}{hidden}."
    if line.get("payee_type") == BANK:
        bank_name = cstr(bank_name).strip()
        if not bank_name:
            return None, (f"Not verified: M-Pesa paid {shown!r}. A bank paybill names the bank, and this bank's Name "
                          "M-Pesa Shows is not set on M-Pesa Bank Paybill: only the number was checked.")
        if pa.names_match(bank_name, _without_number(received)):
            return None, (f"Not verified: M-Pesa paid {shown!r}, the bank's own name. Who holds the account is not "
                          "shown.")
        return False, f"M-Pesa paid {shown!r}, but the bank's name is {bank_name!r}."
    if pa.names_match(line.get("payee_name"), _without_number(received)):
        return True, f"M-Pesa paid {shown!r}."
    return False, f"M-Pesa paid {shown!r}, but the claim gives {line.get('payee_name')!r}."


def _without_number(name) -> str:
    return _NUMBER_PREFIX.sub("", cstr(name), count=1).strip()


def _masked_number(number) -> str:
    number = cstr(number).strip()
    return privacy.phone(number) if number.isdigit() and normalize_msisdn(number) else number


def masked_name(received) -> str:
    """The name M-Pesa paid with the phone number in front as the send summary shows a phone
    (utils.privacy): '254722000111 - JANE' as it is, or '2547****0111 - JANE' when masked."""
    received = cstr(received).strip()
    prefix = number_prefix(received)
    if not prefix:
        return received
    return _NUMBER_PREFIX.sub(lambda m: m.group(0).replace(prefix, _masked_number(prefix), 1), received, count=1)


def mpesa_names(numbers) -> dict:
    """{2547...: _dict(name, payout, paid_on)}: the name M-Pesa last paid each phone under (a Paid B2C
    payout's ReceiverPartyPublicName without its number; one naming another number is skipped), the
    payout and when (round 4.3). A phone M-Pesa never paid is not in it: its line keeps the typed
    name."""
    wanted = {m for m in (normalize_msisdn(n) for n in numbers or []) if m}
    if not wanted:
        return {}
    forms = [form for m in sorted(wanted) for form in (m, "0" + m[3:], "+" + m)]
    rows = frappe.get_all(B2C_REQUEST, filters={"phone_number": ["in", forms], "status": "Paid",
                                                "receiver_public_name": ["is", "set"]},
                          fields=["name", "phone_number", "receiver_public_name", "transaction_completed_datetime",
                                  "creation"],
                          order_by="transaction_completed_datetime desc, creation desc")
    rows.sort(key=lambda r: cstr(r.transaction_completed_datetime or r.creation), reverse=True)
    found = {}
    for r in rows:
        msisdn = normalize_msisdn(r.phone_number)
        prefix = number_prefix(r.receiver_public_name)
        name = _without_number(r.receiver_public_name)
        if msisdn not in wanted or msisdn in found or not name:
            continue
        if prefix and prefix.isdigit() and normalize_msisdn(prefix) != msisdn:
            continue  # M-Pesa named another number: not this phone's name
        found[msisdn] = frappe._dict(name=name, payout=r.name,
                                     paid_on=r.transaction_completed_datetime or r.creation)
    return found


def name_from(known) -> str:
    """Where a line's M-Pesa name comes from: 'B2C-26-10-000081, paid 2026-10-29'."""
    return f"{known.payout}, paid {cstr(known.paid_on)[:10]}"


@frappe.whitelist()
def mpesa_name(phone: str | None = None) -> dict:
    """The Expense Claim form, when a payee line's phone is typed: the name M-Pesa paid it under
    before, which fills and locks the line's Payee Name at once (the claim's save does it again);
    {} for a number M-Pesa never paid. M-Pesa shows that name to anyone who sends to the number. For
    who may create or change Expense Claims."""
    if not (frappe.has_permission(CLAIM, "create") or frappe.has_permission(CLAIM, "write")):
        frappe.throw("You may not create or change Expense Claims.", frappe.PermissionError)
    msisdn = payable_msisdn(phone)
    known = mpesa_names([msisdn]).get(msisdn) if msisdn else None
    return {"phone": msisdn, "name": known.name, "from": name_from(known)} if known else {}


def _bank_name(line) -> str | None:
    """The name M-Pesa shows for a bank account line's bank (its M-Pesa Bank Paybill row)."""
    if line.get("payee_type") != BANK or not line.get("bank_paybill"):
        return None
    return frappe.db.get_value(BANK_PAYBILL, line.bank_paybill, "receiver_name")


def _name_alarm(claim: str, line) -> None:
    """A Comment on the claim and an alert to the Notify Role: M-Pesa paid someone else's name. A
    phone shows in all three as utils.privacy says."""
    message = (f"Payee row #{line.idx} ({line.payee_name}, {describe(line, masked=True)}): {line.name_note} Check with "
               "the claimant before this payee is paid again.")
    try:
        frappe.get_doc(CLAIM, claim).add_comment("Comment", message)
    except Exception:
        frappe.log_error(title="M-Pesa claim payee comment failed", message=f"{claim}\n{frappe.get_traceback()}")
    frappe.log_error(title="M-Pesa claim payee name mismatch", message=f"{claim}: {message}",
                     reference_doctype=CLAIM, reference_name=claim)
    pa.alert(f"{claim}: {message}", frappe._dict(doctype=CLAIM, name=claim))


# ---------------------------------------------------------------- staff or self payments (round 4.1)
def staff_people(claimant, users=(), employees: bool = True) -> list:
    """Who a claim payee must never be, as [(who, name, user)]: the claimant, ``users`` ([(role,
    user)]: the payout's preparer and sender) and, while M-Pesa Suite Settings says so
    (``employees``), every Employee; ``user`` is the person's login (an employee's User ID), who may
    not clear a flag raised under their name. An employee is named by ID only for the M-Pesa
    Managers' alert."""
    user_field = frappe.get_meta("Employee").has_field("user_id")
    people = []
    if claimant:
        values = frappe.db.get_value("Employee", claimant, ["employee_name", "user_id"] if user_field
                                     else ["employee_name"], as_dict=True) or {}
        people.append(("the claimant", values.get("employee_name"), values.get("user_id")))
    for role, user in users:
        if user and user not in ("Administrator", "Guest"):
            people.append((f"{role} ({user})", frappe.db.get_value("User", user, "full_name"), user))
    if employees:
        fields = ["name", "employee_name"] + (["user_id"] if user_field else [])
        people += [(f"an employee ({e.name})", e.employee_name, e.get("user_id"))
                   for e in frappe.get_all("Employee", fields=fields, order_by="name")]
    return [(who, cstr(name).strip(), user or None) for who, name, user in people if cstr(name).strip()]


def staff_matches(received, people) -> list:
    """[(who, name, user)] of every one of ``people`` M-Pesa's name is (two words at least on both
    sides: one first name alone matches no one): two employees may share a name."""
    received = _without_number(received)
    return [(who, name, (user[0] if user else None)) for who, name, *user in people
            if pa.names_match(name, received, min_words=2)]


def staff_match(received, people) -> tuple | None:
    """(who, name, user) of the first of ``people`` M-Pesa's name is (staff_matches), or None."""
    found = staff_matches(received, people)
    return found[0] if found else None


def involved_users(*users) -> str:
    """Who a staff flag is about (staff_involved): the logins that may never clear it."""
    return ", ".join(sorted({cstr(u).strip() for u in users if cstr(u).strip()}))


def staff_note(who: str, received) -> str:
    """What the line says (any reader of the claim sees it): an employee is not named there."""
    whose = "an employee's name" if who.startswith("an employee") else f"the name of {who.split(' (')[0]}"
    return (f"Staff or self payment: M-Pesa paid {masked_name(received)!r}, {whose}. Its destination is refused for "
            "claim payees until an M-Pesa Manager who was not involved clears it (Clear Staff Flag).")


def cleared_destination(key) -> bool:
    """An M-Pesa Manager cleared this destination for claim payees (a line of it staff_check Cleared)."""
    return bool(key) and frappe.db.has_column(LINE, "staff_check") and bool(
        frappe.db.exists(LINE, {"parenttype": CLAIM, "destination_key": key, "staff_check": CLEARED}))


def _claimant_user(claim: str) -> str | None:
    employee = frappe.db.get_value(CLAIM, claim, "employee")
    if not employee or not frappe.get_meta("Employee").has_field("user_id"):
        return None
    return frappe.db.get_value("Employee", employee, "user_id")


def _payout_owner(payout) -> str | None:
    if not payout or not payout.get("doctype") or not payout.get("name"):
        return None
    return frappe.db.get_value(payout.doctype, payout.name, "owner")


def _staff_paid(claim: str, line, payout, cache: dict):
    """After a line was paid: _dict(note, exclude) when the name M-Pesa paid is staff (staff_match:
    the claimant, its payout's preparer or sender, or an employee), else None. Not for a bank account
    (M-Pesa shows the bank's name), a line already flagged or cleared, nor a destination an M-Pesa
    Manager cleared."""
    if (line.get("payee_type") == BANK or line.get("staff_check") in (BLOCKED, CLEARED)
            or not _without_number(line.get("receiver_name")) or cleared_destination(line.get("destination_key"))):
        return None
    if "claimant" not in cache:
        cache.update(claimant=frappe.db.get_value(CLAIM, claim, "employee"), claimant_user=_claimant_user(claim),
                     employees=settings().employee_names)
    maker, sender = _payout_owner(payout), cstr(line.get("sent_by")).strip() or None
    people = staff_people(cache["claimant"], (("its preparer", maker), ("its sender", sender)), cache["employees"])
    found = staff_matches(line.get("receiver_name"), people)
    if not found:
        return None
    # every person of that name is involved (two employees may share it): none is alerted, none clears it
    exclude = sorted({u for u in (maker, sender, cache["claimant_user"], *(f[2] for f in found)) if u})
    return frappe._dict(note=staff_note(found[0][0], line.get("receiver_name")), who=found[0][0], exclude=exclude,
                        involved=involved_users(*exclude))


def _staff_alarm(claim: str, line, exclude=(), rolled_back: bool = False) -> None:
    """A staff or self payment flagged: a Comment on the claim, an Error Log and an alert to the
    M-Pesa Managers (ADMIN_ROLES) but who was involved (``exclude``: its preparer, sender and the
    claimant). A phone is masked."""
    message = (f"Payee row #{line.idx} ({line.payee_name}, {describe(line, masked=True)}): {line.get('staff_note')} "
               "Check with the claimant and whoever sent it.")
    try:
        frappe.get_doc(CLAIM, claim).add_comment("Comment", message)
    except Exception:
        frappe.log_error(title="M-Pesa claim payee comment failed", message=f"{claim}\n{frappe.get_traceback()}")
    frappe.log_error(title="M-Pesa claim payee staff or self payment", message=f"{claim}: {message}",
                     reference_doctype=CLAIM, reference_name=claim)
    pa.alert(f"{claim}: {message}", frappe._dict(doctype=CLAIM, name=claim), roles=ADMIN_ROLES, exclude=exclude,
             rolled_back=rolled_back)


def names_paid_before(line, limit: int = 20) -> list:
    """The names M-Pesa paid this line's destination under before (latest first): other claim payee
    lines of it, B2C payouts to its phone, B2B payouts to its till or paybill and account. None for
    a bank account (M-Pesa shows the bank's name)."""
    if line.get("payee_type") == BANK or not line.get("destination_key"):
        return []
    names = frappe.get_all(LINE, filters={"parenttype": CLAIM, "destination_key": line.destination_key,
                                          "name": ["!=", cstr(line.get("name"))], "receiver_name": ["is", "set"]},
                           pluck="receiver_name", order_by="modified desc", limit=limit)
    if line.get("payee_type") == PHONE:
        msisdn = normalize_msisdn(line.get("phone"))
        if msisdn:
            names += frappe.get_all(B2C_REQUEST, filters={"phone_number": ["in", [msisdn, "0" + msisdn[3:], "+" + msisdn]],
                                                          "status": "Paid", "receiver_public_name": ["is", "set"]},
                                    pluck="receiver_public_name", order_by="creation desc", limit=limit)
    else:
        filters = {"receiver_shortcode": cstr(line.get("shortcode")).strip(), "status": "Paid",
                   "receiver_public_name": ["is", "set"]}
        if line.get("payee_type") == PAYBILL:
            filters["account_reference"] = cstr(line.get("account_number")).strip()
        names += frappe.get_all(B2B, filters=filters, pluck="receiver_public_name", order_by="creation desc",
                                limit=limit)
    return list(dict.fromkeys(cstr(n).strip() for n in names if cstr(n).strip()))


def staff_paid_before(line, claimant, users=(), employees: bool = True):
    """At the send: (who, received, users) when a name M-Pesa paid this line's number under before is
    staff (staff_matches; ``users``: the logins of everyone of that name), unless an M-Pesa Manager
    cleared the destination; else None."""
    names = names_paid_before(line)
    if not names or cleared_destination(line.get("destination_key")):
        return None
    people = staff_people(claimant, users, employees)
    for received in names:
        found = staff_matches(received, people)
        if found:
            return found[0][0], received, tuple(f[2] for f in found if f[2])
    return None


def own_names(user: str) -> list:
    """The names ``user`` goes by: their User's full name and the name of each Employee whose User ID
    they are."""
    names = [frappe.db.get_value("User", user, "full_name")]
    if frappe.get_meta("Employee").has_field("user_id"):
        names += frappe.get_all("Employee", filters={"user_id": user}, pluck="employee_name")
    return list(dict.fromkeys(cstr(n).strip() for n in names if cstr(n).strip()))


def flag_staff(claim: str, line: str, note: str, exclude=()) -> None:
    """A job queued at a send refused as a staff or self payment (the refusal rolls the send's
    transaction back): the line is flagged (staff_check Blocked, unless its destination was cleared
    meanwhile), who it is about is kept (staff_involved: ``exclude``, the refused sender among them,
    since the send's own record of its sender was rolled back) and the M-Pesa Managers but them
    are alerted."""
    with owed.doc_lock(CLAIM, claim, timeout=owed.BOOKING_WAIT):
        row = frappe.db.get_value(LINE, line, list(LINE_FIELDS), as_dict=True)
        if (not row or row.parent != claim or row.get("staff_check") in (BLOCKED, CLEARED)
                or cleared_destination(row.destination_key)):
            return
        values = {"staff_check": BLOCKED, "staff_note": note, "staff_involved": involved_users(*exclude)}
        frappe.db.set_value(LINE, line, values, update_modified=False)
        row.update(values)
        _staff_alarm(claim, row, exclude)


@frappe.whitelist(methods=["POST"])
def clear_staff_flag(claim: str, line: str, reason: str | None = None) -> dict:
    """Clear Staff Flag: a named M-Pesa Manager (ADMIN_ROLES) who can read the claim and checked that
    a payee line's destination is not a staff member's clears it for claim payees: every line of
    that destination flagged Blocked, and ``line``, become Cleared, with who cleared it and why; the
    names M-Pesa paid it under before no longer refuse its send. Only a destination that is blocked
    (a line of it Blocked); never by who a flag is about: the claimant, who prepared or sent (or
    tried to send) a payout of such a line, the staff members whose name M-Pesa paid
    (staff_involved), nor anyone whose own name (own_names) is a name M-Pesa paid this destination
    under: an employee with no User ID, or one who shares a name, is caught there. A Comment on each
    claim records it."""
    require_named_user()
    require(ADMIN_ROLES)
    doc = frappe.get_doc(CLAIM, claim)
    doc.check_permission("read")
    reason = cstr(reason).strip()
    if not reason:
        frappe.throw("Say how you checked that this payee is no staff member (who you asked, what they showed).")
    user = frappe.session.user
    with owed.doc_lock(CLAIM, claim):
        row = next((r for r in read_lines(claim, for_update=True) if r.name == line), None)
        if not row or not row.get("destination_key"):
            frappe.throw(f"{line} is not a payee row of {claim}.")
        flagged = frappe.get_all(LINE, filters={"parenttype": CLAIM, "destination_key": row.destination_key,
                                               "staff_check": BLOCKED},
                                 fields=["name", "parent", "idx", "sent_by", "payout_doctype", "payout_name",
                                         "staff_note", "staff_involved", "receiver_name"])
        if not flagged:
            # nothing to clear: clearing a destination never flagged would switch its staff check off for good
            frappe.throw(f"Payee row #{row.idx} of {claim} ({describe(row, masked=True)}) is not blocked as a staff or "
                         "self payment: there is no flag to clear.")
        involved = set()
        for f in [*flagged, row]:
            involved |= {cstr(f.get("sent_by")).strip(), _claimant_user(f.parent) or ""}
            involved |= {u.strip() for u in cstr(f.get("staff_involved")).split(",")}
            if f.get("payout_doctype") and f.get("payout_name"):
                involved.add(cstr(frappe.db.get_value(f.payout_doctype, f.payout_name, "owner")))
        paid = [f.get("receiver_name") for f in [*flagged, row]] + names_paid_before(row)
        mine = [("you", name, user) for name in own_names(user)]
        if user in involved - {""} or any(staff_matches(received, mine) for received in paid if cstr(received).strip()):
            frappe.throw("The flag is about you (the claimant, who prepared or sent a payment to this destination, or "
                         "whose name M-Pesa paid): another M-Pesa Manager clears it.", frappe.PermissionError)
        cleared = f"Cleared by {user} on {now_datetime():%Y-%m-%d %H:%M}: {reason}"
        for f in {f.name: f for f in [*flagged, row]}.values():
            note = " ".join(n for n in (cstr(f.get("staff_note")).strip(), cleared) if n)
            frappe.db.set_value(LINE, f.name, {"staff_check": CLEARED, "staff_note": note[:1000]}, update_modified=False)
        for parent in sorted({f.parent for f in [*flagged, row]}):
            frappe.get_doc(CLAIM, parent).add_comment(
                "Comment", f"Staff flag on {describe(row, masked=True)} cleared for claim payees by {user}: {reason}")
    return {"line": row.name, "cleared": len({f.name for f in [*flagged, row]})}


def _record_supplier_account(line, payout) -> None:
    """A line paid to a supplier's approved account (supplier_account, noted at the send): the B2B
    request names that Supplier M-Pesa Account, and the account records who M-Pesa paid
    (last_receiver_name, last_paid_on; a mismatch blocks it), as a supplier's own payout does.
    Never raises."""
    try:
        doc = None
        if payout.doctype == B2B:
            if not frappe.db.get_value(B2B, payout.name, "supplier_mpesa_account"):
                frappe.db.set_value(B2B, payout.name, "supplier_mpesa_account", line.supplier_account,
                                    update_modified=False)
            doc = frappe.get_doc(B2B, payout.name)
        elif payout.get("request"):
            doc = frappe.get_doc(B2C_REQUEST, payout.request)
        if doc and cstr(line.get("receiver_name")).strip():
            pa.record_receiver_name(SUPPLIER_ACCOUNT, line.supplier_account, line.receiver_name, doc, notify=pa.alert)
    except Exception:
        frappe.log_error(title="M-Pesa claim payee supplier account not recorded",
                         message=f"{line.get('parent')} {line.name}\n{frappe.get_traceback()}")


# ---------------------------------------------------------------- a payout of a line (B2B, B2C)
def _line(name: str):
    row = frappe.db.get_value(LINE, name, list(LINE_FIELDS), as_dict=True) if name else None
    if not row or row.parenttype != CLAIM:
        frappe.throw(f"Expense Claim payee {name} not found.")
    return row


def payable_problem(doc, for_update: bool = False) -> str | None:
    """Why the claim may not be paid now (money_out.employee_documents: submitted, approved, in a
    payable workflow state, something owed)."""
    from .employee_documents import payable_problem as problem

    return problem(doc, for_update=for_update) if for_update else problem(doc)


def _memo(owner, key: str, make):
    """``make()`` once per document being checked (a disbursement checks each of its rows)."""
    flags = getattr(owner, "flags", None)
    if flags is None:
        return make()
    if key not in flags:
        flags[key] = make()
    return flags[key]


def _account_of(value, payee_type) -> str | None:
    return None if payee_type == TILL else (cstr(value).strip() or None)


def check_b2b(doc) -> None:
    """A B2B request that pays a merchant line of an Expense Claim (its claim_payee). money_out.b2b
    calls it beside an employee request's checks: on save, at the send (submit) and right before
    the money leaves. A draft takes the line's destination, and every check compares with it: the
    line's claim and claimant, its till, paybill or bank account, its whole amount, the line not
    paid or on its way by another payout. At the send, under the claim's lock: the claim still
    payable, the destination still no employee's, and the payee caps (M-Pesa Suite Settings)."""
    name = cstr(doc.get("claim_payee")).strip()
    if not name:
        return
    line = _line(name)
    claim = cstr(doc.get("expense_claim")).strip()
    if claim != line.parent:
        frappe.throw(f"{name} is a payee of {line.parent}, not of {claim or 'no Expense Claim'}.")
    if line.payee_type not in B2B_TYPES:
        frappe.throw(f"Payee row #{line.idx} of {claim} is a phone: it is paid by B2C, not B2B.")
    sending = cint(doc.docstatus) == 1
    for field, label in (("supplier", "Supplier"), ("supplier_mpesa_account", "Supplier M-Pesa Account"),
                         ("employee_mpesa_account", "Employee M-Pesa Account"), ("salary_slip", "Salary Slip"),
                         ("employee_advance", "Employee Advance"), ("purchase_invoice", "Purchase Invoice"),
                         ("purchase_order", "Purchase Order")):
        if field == "supplier_mpesa_account" and not sending:
            continue  # a draft's is cleared below
        if cstr(doc.get(field)).strip():
            frappe.throw(f"This pays payee row #{line.idx} of {claim} to its own {WHAT[line.payee_type]}, for the "
                         f"claim only: clear {label}.")
    values = frappe.db.get_value(CLAIM, claim, ["name", "employee", "company"], as_dict=True)
    if not values:
        frappe.throw(f"Expense Claim {claim} not found.")
    if not sending:
        # the approved supplier account a line is paid to is named on its request once Paid
        # (_record_supplier_account), never on a draft: an amended copy is cleared
        doc.supplier_mpesa_account = None
        doc.employee = values.employee
        doc.company = values.company or doc.get("company")  # the claim's, not the user's default
        doc.command_id = COMMAND_FOR[line.payee_type]
        doc.receiver_shortcode = line.shortcode
        doc.account_reference = _account_of(line.account_number, line.payee_type)
    if doc.get("employee") != values.employee:
        frappe.throw(f"{claim} is {values.employee}'s claim: its payees are paid on it, with Employee {values.employee}.")
    if values.company and doc.get("company") != values.company:
        frappe.throw(f"{claim} belongs to {values.company}, not {doc.get('company') or 'no company'}.")
    if (doc.get("command_id") != COMMAND_FOR[line.payee_type]
            or cstr(doc.get("receiver_shortcode")).strip() != cstr(line.shortcode).strip()
            or _account_of(doc.get("account_reference"), line.payee_type)
            != _account_of(line.account_number, line.payee_type)):
        frappe.throw(f"The destination on {doc.name or 'this request'} is not payee row #{line.idx}'s of {claim} "
                     f"({describe(line)}).")
    if abs(flt(doc.get("amount")) - flt(line.amount)) > 0.001:
        # a payee is paid its line, whole: a payout of part of it would mark the line Paid and leave the
        # rest of the merchant's money to be reimbursed to the employee
        frappe.throw(f"KES {money(doc.amount)} is not what payee row #{line.idx} of {claim} pays "
                     f"(KES {money(line.amount)}): a payee is paid its whole line.")
    if not sending:
        _check_not_settled(line)
        _check_others(line, (B2B, doc.name))
        return
    with _day_lock(doc.company), owed.doc_lock(CLAIM, claim):
        _check_not_settled(line, for_update=True)
        _check_others(line, (B2B, doc.name), for_update=True)
        _check_sending(claim, line, flt(doc.amount), (B2B, doc.name), doc.company, flt(doc.amount), doc,
                       maker=doc.owner)


def check_supplier_request(doc) -> None:
    """A B2B request that pays no employee (a supplier's, or no one's yet) never pays an Expense
    Claim's payee line: money_out.b2b's supplier checks call it. Such a line is paid on its claim,
    with the claimant as Employee (Pay Merchants and Payees makes that request), and checked by
    check_b2b; paid as a supplier's, its payout would take the line and be booked to the supplier."""
    name = cstr(doc.get("claim_payee")).strip()
    if name:
        frappe.throw(f"This request names Expense Claim payee {name}: a claim's payee is paid on the claim, with the "
                     "claimant as Employee (Pay Merchants and Payees on the claim), never as a supplier's payment.")


def check_b2c_row(parent, row) -> None:
    """A B2C Payment Disbursement row that pays a casual payee (phone) line of an Expense Claim
    (its claim_payee). The row's own validate calls it, on every save and at the send (submit):
    the row pays the line's claim on its claimant, to the line's phone, its whole amount, and the
    line is not paid or on its way by another payout. At the send, under the claim's lock: the
    claim still payable, the phone still no employee's, and the payee caps (the disbursement's rows
    to such payees counted together for the day's cap)."""
    name = cstr(row.get("claim_payee")).strip()
    if not name:
        return
    parent = parent or frappe.get_doc(B2C, row.parent)
    line = _line(name)
    if row.get("reference_doctype") != CLAIM or row.get("reference_name") != line.parent:
        frappe.throw(f"Row #{row.idx} pays payee row #{line.idx} of {line.parent}: its document must be Expense "
                     f"Claim {line.parent}.")
    if line.payee_type != PHONE:
        frappe.throw(f"Row #{row.idx}: payee row #{line.idx} of {line.parent} is a {WHAT[line.payee_type]}: it is "
                     "paid by B2B, not to a phone.")
    values = frappe.db.get_value(CLAIM, line.parent, ["employee", "company"], as_dict=True) or frappe._dict()
    claimant = values.employee
    if row.get("party_type") != "Employee" or row.get("party") != claimant:
        frappe.throw(f"Row #{row.idx}: the party is the claimant, {claimant}: a payee is paid on their claim.")
    if values.company and parent.get("company") != values.company:
        # the payout leaves from the claim's company's shortcode, and its day's cap counts it
        frappe.throw(f"Row #{row.idx}: {line.parent} belongs to {values.company}, not "
                     f"{parent.get('company') or 'no company'}.")
    if normalize_msisdn(row.get("partyb")) != line.phone:
        frappe.throw(f"Row #{row.idx}: the phone is not payee row #{line.idx}'s of {line.parent} ({describe(line)}).")
    if abs(flt(row.get("allocated_amount")) - flt(line.amount)) > 0.001:
        frappe.throw(f"Row #{row.idx}: KES {money(row.allocated_amount)} is not what payee row #{line.idx} of "
                     f"{line.parent} pays (KES {money(line.amount)}): a payee is paid its whole line.")
    twice = [r.idx for r in parent.get("references") or [] if cstr(r.get("claim_payee")).strip() == name]
    if len(twice) > 1:
        frappe.throw(f"Rows {', '.join(f'#{i}' for i in twice)} pay the same payee row #{line.idx} of {line.parent}.")
    if _sent_already(row):
        return  # an amended copy of a row its original sent: not sent again
    if cint(parent.docstatus) != 1:
        _check_not_settled(line)
        _check_others(line, (B2C, parent.name))
        return
    with _day_lock(parent.get("company")), owed.doc_lock(CLAIM, line.parent):
        _check_not_settled(line, for_update=True)
        _check_others(line, (B2C, parent.name), for_update=True)
        batch = _memo(parent, "mpsa_claim_payee_batch", lambda: _capped_rows_total(parent))
        _check_sending(line.parent, line, flt(row.allocated_amount), (B2C, parent.name), parent.get("company"),
                       batch, parent, maker=parent.owner)


def _sent_already(row) -> bool:
    request = cstr(row.get("b2c_disbursement_request")).strip()
    if not request:
        return False
    status = frappe.db.get_value(B2C_REQUEST, request, "status")
    return bool(status) and status != "Failed"


def _capped_rows_total(parent) -> float:
    """What the disbursement's rows send to payees that are not approved supplier accounts now
    (approved_supplier_account, as each row's own check at the send; send_confirm says it first)."""
    rows = [r for r in parent.get("references") or [] if cstr(r.get("claim_payee")).strip() and not _sent_already(r)]
    if not rows:
        return 0.0
    lines = {r.name: r for r in frappe.get_all(
        LINE, filters={"name": ["in", [r.claim_payee for r in rows]]}, fields=list(LINE_FIELDS))}
    return flt(sum(flt(r.allocated_amount) for r in rows
                   if not (lines.get(r.claim_payee) and approved_supplier_account(lines[r.claim_payee]))), 2)


def _check_not_settled(line, for_update: bool = False) -> None:
    """A Cancelled line (settled outside M-Pesa: settle_line; or its claim was cancelled) is never paid
    by M-Pesa. ``for_update``: read again with a lock, under the claim's lock (a send)."""
    status = frappe.db.get_value(LINE, line.name, "status", for_update=True) if for_update else line.get("status")
    if status == CANCELLED:
        frappe.throw(f"Payee row #{line.idx} of {line.parent} ({line.payee_name}) is Cancelled: it is not paid by "
                     "M-Pesa.")


def _check_others(line, this: tuple, for_update: bool = False) -> None:
    """The line is not paid, on its way, or about to be sent by another payout (a draft is not
    one: whichever is sent first takes the line)."""
    for p in payouts_of([line.name], for_update):
        if (p.doctype, p.name) == this:
            continue
        if p.state in (PAID, IN_FLIGHT) or (p.state == QUEUED and _submitted(p)):
            how = "paid" if p.state == PAID else "being paid"
            frappe.throw(f"Payee row #{line.idx} of {line.parent} ({line.payee_name}) is {how} by {p.doctype} "
                         f"{p.name}: it is never paid twice.")


def retry_problem(request: str, for_update: bool = False) -> str | None:
    """Why a failed B2C payout (B2C Disbursement Request) of a casual payee line may not be sent
    again by its own retry: the line has another payout paid, on its way or about to be sent
    (Retry failed payees made a new one). None for a payout that pays no payee line."""
    line = _line_of(B2C_REQUEST, request)
    if not line:
        return None
    parent = frappe.db.get_value(B2C_REQUEST, request, "b2c_payment_disbursement")
    for p in payouts_of([line], for_update):
        if p.doctype == B2C and p.name == parent:
            continue
        if p.state in (PAID, IN_FLIGHT) or (p.state == QUEUED and _submitted(p)):
            return (f"{request} pays an Expense Claim payee that {p.doctype} {p.name} pays now: retry the payee from "
                    "its claim (Retry Failed Payees), never this payout as well.")
    return None


def check_retry(request: str) -> None:
    """B2C Disbursement Request.retry_failed_payment (both retry buttons), under the claim's lock
    (the request's document lock), before it sends again: a failed payout of a casual payee line
    is a send like the first, so the line has no other payout paid, on its way or about to be
    sent (retry_problem: Retry Failed Payees may have paid it since), and the send's checks apply
    (the switch, the claim payable, the phone no employee's, the caps). Nothing for a payout that
    pays no payee line."""
    name = _line_of(B2C_REQUEST, request)
    if not name:
        return
    line = _line(name)
    values = frappe.db.get_value(B2C_REQUEST, request, ["amount", "b2c_payment_disbursement"], as_dict=True)
    company = frappe.db.get_value(B2C, values.b2c_payment_disbursement, "company") or frappe.db.get_value(
        CLAIM, line.parent, "company")
    with _day_lock(company), owed.doc_lock(CLAIM, line.parent):
        _check_not_settled(line, for_update=True)
        problem = retry_problem(request, for_update=True)
        if problem:
            frappe.throw(problem)
        # the day's cap counts the rest of its batch that did not fail, and this payout
        _check_sending(line.parent, line, flt(values.amount), (B2C, values.b2c_payment_disbursement), company,
                       flt(values.amount), exclude=(None, None),
                       maker=frappe.db.get_value(B2C, values.b2c_payment_disbursement, "owner"))


def _submitted(payout) -> bool:
    """A payout submitted and not sent yet (a B2B request Not Initiated, a B2C row before its
    payout is made): it is about to take money."""
    return cint(frappe.db.get_value(payout.doctype, payout.name, "docstatus")) == 1


def paid_in_parts(doctype: str, name: str) -> bool:
    """An Expense Claim with payee lines: paid by several payouts at once (a B2B request per
    merchant, a B2C batch for the phones, the rest to the employee). B2C's double-payout guards
    (B2C Payment Disbursement.validate_no_double_payout, B2C Disbursement Request
    .validate_still_owed) then count its other payouts in the amount instead of refusing while
    one is open; each line's own double payment is refused by check_b2b and check_b2c_row."""
    if doctype != CLAIM or not name or not frappe.db.table_exists(LINE):
        return False
    return bool(frappe.db.exists(LINE, {"parent": name, "parenttype": CLAIM}))


def others_taken_off(doctype: str, name: str, others: list) -> tuple:
    """For B2C Payment Disbursement.validate_no_double_payout: (the other payouts that still refuse
    the send, those taken off what the document owes instead, like an order's advances: [{amount,
    label}]). A claim paid in parts (paid_in_parts) has none of the first."""
    if not others or not paid_in_parts(doctype, name):
        return others, []
    taken = []
    for o in others:
        status = o.get("request_status") or o.get("payment_status") or "in progress"
        taken.append(frappe._dict(amount=flt(o.get("allocated_amount"), 2),
                                  label=f"{o.get('parent')} {status} {money(o.get('allocated_amount'))}"))
    return [], taken


def _check_sending(claim: str, line, amount: float, payout: tuple, company, batch: float, owner=None,
                   exclude: tuple | None = None, maker: str | None = None) -> None:
    """At the send: the switch, the claim payable (money_out.employee_documents), the destination
    still no employee's, user's or blocked one (employee_problem), no name M-Pesa paid it under
    before a staff member's (staff_paid_before: the claimant, the payout's preparer ``maker``, the
    sender, an employee), the caps on a destination that is not an approved supplier account (the
    day's: ``batch`` and what was sent today but ``exclude``, by default this payout). Then the
    line notes the payout sending it and who sent it (the day's cap counts it from now; a send that
    fails before the money leaves rolls this back with it)."""
    s = settings()
    if not s.allow:
        frappe.throw("Paying merchants and payees from an Expense Claim is switched off (M-Pesa Suite Settings).")
    doc = frappe.get_doc(CLAIM, claim)
    problem = payable_problem(doc, for_update=True)  # under the claim's lock: its reimbursed total as it is now
    if problem:
        frappe.throw(problem)
    found = _memo(owner, "mpsa_employee_destinations", employee_destinations)
    problem = employee_problem(line, doc.employee, found, masked=True)
    if problem:
        frappe.throw(f"{claim}: {problem}")
    sender = frappe.session.user
    staff = staff_paid_before(line, doc.employee, (("its preparer", maker), ("its sender", sender)), s.employee_names)
    if staff:
        note = staff_note(staff[0], staff[1])
        try:  # the line is flagged by a job queued now: this refusal rolls the send back, its sender with it
            frappe.enqueue(f"{__name__}.flag_staff", queue="short", timeout=300, claim=claim, line=line.name, note=note,
                           exclude=sorted({u for u in (maker, sender, _claimant_user(claim), *staff[2]) if u}))
        except Exception:
            frappe.log_error(title="M-Pesa claim payee staff flag not queued",
                             message=f"{claim}\n{frappe.get_traceback()}")
        frappe.throw(f"{claim}: payee row #{line.idx} ({line.payee_name}, {describe(line, masked=True)}): {note}")
    supplier = approved_supplier_account(line)
    if not supplier:
        problem = amount_cap_problem(frappe._dict(line, amount=amount), s)
        if problem:
            frappe.throw(f"{claim}: {problem}")
        if s.daily_cap:
            with _day_lock(company, s):
                # a locking read once the company's lock is taken: a send of another claim that
                # committed while this one waited is counted
                sent = sent_today(company, payout if exclude is None else exclude, for_update=True)
            if sent + batch > s.daily_cap + 0.001:
                frappe.throw(f"KES {money(sent)} was sent today to Expense Claim payees that are not approved "
                             f"accounts; with this KES {money(batch)} it would pass the day's cap of KES "
                             f"{money(s.daily_cap)} (M-Pesa Suite Settings). Send it tomorrow, or pay an approved "
                             "Supplier M-Pesa Account.")
    frappe.db.set_value(LINE, line.name, {"status": IN_FLIGHT, "payout_doctype": payout[0], "payout_name": payout[1],
                                          "sent_on": now_datetime(), "sent_by": sender, "supplier_account": supplier,
                                          "error_message": None}, update_modified=False)


def _day_lock(company, s=None):
    """The company's lock for the day's cap on payees that are not approved accounts (held until
    commit), while that cap is on: one such send of the company's claims at a time, so the next
    one counts this one. A send takes it before the claim's lock (a B2C batch may pay several
    claims) and re-enters it under it; a retry already holds its claim's lock. Two sends that wait
    on each other are told to try again (the lock's timeout), never both let through."""
    if not company or not (s or settings()).daily_cap:
        return contextlib.nullcontext()
    from ..money_in.common import named_lock

    busy = f"Another payment to Expense Claim payees of {company} is being sent. Try again in a minute."
    return named_lock(f"claim-payee-day:{company}", busy=busy)


def sent_today(company, exclude: tuple = (None, None), for_update: bool = False) -> float:
    """What Expense Claim payee lines of ``company``'s claims sent today to destinations that are
    not approved supplier accounts, by payouts that did not fail (``exclude``: the payout being
    sent, which the caller counts). ``for_update``: a locking read, under the company's lock."""
    if not company:
        return 0.0
    rows = frappe.db.sql(
        f"""
        SELECT COALESCE(SUM(p.amount), 0)
        FROM `tab{LINE}` p
        JOIN `tab{CLAIM}` c ON c.name = p.parent
        LEFT JOIN `tab{B2B}` r ON p.payout_doctype = %(b2b)s AND r.name = p.payout_name
        LEFT JOIN `tab{B2C_ROW}` ref ON p.payout_doctype = %(b2c)s AND ref.parent = p.payout_name
            AND ref.claim_payee = p.name
        LEFT JOIN `tab{B2C}` d ON d.name = ref.parent
        LEFT JOIN `tab{B2C_REQUEST}` req ON req.name = ref.b2c_disbursement_request
        WHERE p.parenttype = %(claim)s AND c.company = %(company)s AND p.sent_on >= %(today)s
          AND COALESCE(p.supplier_account, '') = ''
          AND NOT (p.payout_doctype = %(dt)s AND p.payout_name = %(dn)s)
          AND (
            (r.docstatus = 1 AND COALESCE(r.status, '') != 'Failed')
            OR (r.docstatus = 2 AND r.status IN ('Paid', 'Initiated', 'Unknown'))
            OR (d.docstatus = 1 AND COALESCE(req.status, ref.payment_status, '') != 'Failed')
            OR (req.docstatus IN (1, 2) AND req.status IN ('Paid', 'Initiated', 'Status Unknown'))
          )
        {_locking(for_update)}
        """,
        {"b2b": B2B, "b2c": B2C, "claim": CLAIM, "company": company, "today": f"{nowdate()} 00:00:00",
         "dt": exclude[0] or "", "dn": exclude[1] or ""},
    )
    return flt(rows[0][0] if rows else 0, 2)


# ---------------------------------------------------------------- making the payouts (drafts)
@frappe.whitelist(methods=["POST"])
def pay_payees(claim: str, b2b_settings: str | None = None, b2c_settings: str | None = None) -> dict:
    """Pay merchants and payees: draft payouts for the claim's payee lines with none yet (one B2B
    request per merchant, one B2C Payment Disbursement for the phones). An M-Pesa Payer who did
    not prepare them sends them (Send via M-Pesa). For PREPARE_ROLES who can read the claim."""
    require(PREPARE_ROLES)
    doc = frappe.get_doc(CLAIM, claim)
    doc.check_permission("read")
    return make_payouts([doc], (DRAFT,), b2b_settings, b2c_settings)


@frappe.whitelist(methods=["POST"])
def retry_payees(claim: str, b2b_settings: str | None = None, b2c_settings: str | None = None) -> dict:
    """Retry failed payees: new draft payouts for the claim's lines whose last payout failed, and
    only those. For PREPARE_ROLES who can read the claim."""
    require(PREPARE_ROLES)
    doc = frappe.get_doc(CLAIM, claim)
    doc.check_permission("read")
    return make_payouts([doc], (FAILED,), b2b_settings, b2c_settings)


@frappe.whitelist(methods=["POST"])
def pay_claims(claims, b2c_settings: str | None = None) -> dict:
    """The Expense Claim list's Pay casual payees by M-Pesa: one draft B2C Payment Disbursement for
    the phone lines of the ticked claims not paid yet (or failed), of one company and one payable
    account. For PREPARE_ROLES who can read every ticked claim."""
    require(PREPARE_ROLES)
    if isinstance(claims, str):
        claims = frappe.parse_json(claims)
    names = sorted({cstr(c).strip() for c in claims or []} - {""})
    if not names:
        frappe.throw("Tick the Expense Claims whose payees to pay.")
    if len(names) > 200:
        frappe.throw("Pay the payees of at most 200 claims at a time.")
    for name in names:
        frappe.has_permission(CLAIM, "read", doc=name, throw=True)
    docs = [frappe.get_doc(CLAIM, name) for name in names]
    return make_payouts(docs, (DRAFT, FAILED), None, b2c_settings, phones_only=True)


def make_payouts(docs, states, b2b_settings=None, b2c_settings=None, phones_only: bool = False) -> dict:
    """Draft payouts for the lines of ``docs`` in ``states``, under each claim's lock (a second
    press waits, then finds the lines Queued). Made with ignore_permissions: the caller checked
    the preparer's roles and read permission on each claim, and every payout checks its lines
    again when it is saved and sent. Nothing is sent here."""
    if not settings().allow:
        frappe.throw("Paying merchants and payees from an Expense Claim is switched off (M-Pesa Suite Settings).")
    merchants, phones = [], []
    for doc in sorted(docs, key=lambda d: d.name):
        problem = payable_problem(doc)
        if problem:
            frappe.throw(problem if doc.name in cstr(problem) else f"{doc.name}: {problem}")
        with owed.doc_lock(CLAIM, doc.name):
            lines = sync_lines(doc.name, for_update=True)
            picked = [line for line in lines
                      if line.status in states and (not phones_only or line.payee_type == PHONE)]
            _check_left(doc, picked)
        for line in picked:
            (phones if line.payee_type == PHONE else merchants).append((doc, line))
    if not (merchants or phones):
        frappe.throw("No payee line has failed." if states == (FAILED,) else
                     "Nothing to pay: every payee line is paid, on its way, or has a payout waiting to be sent.")
    made = frappe._dict(b2b=[], b2c=None, lines=len(merchants) + len(phones),
                        total=flt(sum(flt(line.amount) for _doc, line in merchants + phones), 2))
    for doc, line in merchants:
        request = new_b2b(doc, line, pick_settings(b2b_settings, apis.B2B, doc.company))
        request.insert(ignore_permissions=True)
        _queued(line, B2B, request.name)
        made.b2b.append(request.name)
    if phones:
        disbursement = new_b2c(phones, b2c_settings)
        disbursement.insert(ignore_permissions=True)
        for _doc, line in phones:
            _queued(line, B2C, disbursement.name)
        made.b2c = disbursement.name
    names = ", ".join(made.b2b + ([made.b2c] if made.b2c else []))
    from .schedule import maker_checker_on

    who = "An M-Pesa Payer who did not prepare them" if maker_checker_on() else "An M-Pesa Payer"
    for doc in {d.name: d for d, _line in merchants + phones}.values():
        doc.add_comment("Comment", f"{frappe.session.user} prepared M-Pesa payouts for its payees: {names}. {who} sends "
                                   "them with Send via M-Pesa.")
    return made


def _check_left(doc, picked) -> None:
    """The lines to pay fit in what the claim has left to pay, its payouts on their way and its
    other unpaid payee lines counted (money_out.owed.employee_doc_left_to_pay, under the claim's
    lock); the lines being paid are their own share."""
    total = flt(sum(flt(line.amount) for line in picked), 2)
    if not total:
        return
    from .owed import employee_doc_left_to_pay

    result = employee_doc_left_to_pay(CLAIM, doc.name, exclude_payees=[line.name for line in picked])
    left = flt(result.get("left") if isinstance(result, dict) else result, 2)
    if total > left + 0.001:
        frappe.throw(f"{doc.name} has KES {money(left)} left to pay, less than its payees' KES {money(total)}: check "
                     "its payouts on their way before paying again.")


def _queued(line, doctype: str, name: str) -> None:
    frappe.db.set_value(LINE, line.name, {"status": QUEUED, "payout_doctype": doctype, "payout_name": name,
                                          "error_message": None}, update_modified=False)
    line.update(status=QUEUED, payout_doctype=doctype, payout_name=name)


def pick_settings(name, api: str, company) -> str:
    """The shortcode to pay from: the one picked, or the company's only one that may send ``api``
    (B2B or B2C) and has an initiator."""
    has_company = frappe.get_meta("Mpesa Settings").has_field("company")
    label = "Pay From (B2B)" if api == apis.B2B else "Pay From (B2C)"
    if name:
        fields = ["name", "initiator_name", *apis.ALL] + (["company"] if has_company else [])
        values = frappe.db.get_value("Mpesa Settings", name, fields, as_dict=True)
        if not values:
            frappe.throw(f"Mpesa Settings {name} not found")
        apis.require(values, api)
        if values.get("company") and company and values.company != company:
            frappe.throw(f"Mpesa Settings {name} pays for {values.company}, not {company}.")
        return name
    filters = {api: 1, **({"company": company} if has_company and company else {})}
    found = [s.name for s in frappe.get_all("Mpesa Settings", filters=filters, fields=["name", "initiator_name"])
             if s.initiator_name]
    if len(found) != 1:
        frappe.throw(f"Pick {label}: {company or 'this company'} has "
                     + ("no shortcode that may send it." if not found else f"{len(found)} shortcodes that may send it."))
    return found[0]


def new_b2b(doc, line, settings_name: str):
    """A draft B2B request for a merchant line: on the claimant and the claim, to the line's
    destination (check_b2b compares it again on every save and at the send)."""
    request = frappe.new_doc(B2B)
    request.update({
        "mpesa_settings": settings_name,
        "company": doc.company,
        "employee": doc.employee,
        "expense_claim": doc.name,
        "claim_payee": line.name,
        "command_id": COMMAND_FOR[line.payee_type],
        "receiver_shortcode": line.shortcode,
        "account_reference": _account_of(line.account_number, line.payee_type),
        "amount": flt(line.amount),
        "remarks": f"{doc.name} {line.payee_name}"[:100],
    })
    request.flags.ignore_permissions = True
    return request


def new_b2c(entries, b2c_settings=None):
    """One draft B2C Payment Disbursement for phone lines [(claim, line)]: a row per line on the
    claimant and the claim, to the line's phone. The claims are of one company and one payable
    account (the disbursement's Account Paid To)."""
    first = entries[0][0]
    companies = sorted({cstr(doc.company) for doc, _line in entries})
    if len(companies) > 1:
        frappe.throw(f"Pay the casual payees of one company at a time ({', '.join(companies)}).")
    payables = sorted({cstr(doc.get("payable_account")) for doc, _line in entries})
    if len(payables) > 1:
        frappe.throw(f"The claims must have one Payable Account to be paid in one batch ({', '.join(payables)}).")
    if not payables[0]:
        frappe.throw(f"{first.name} has no Payable Account.")
    settings_name = pick_settings(b2c_settings, apis.B2C, first.company)
    from ..money_in import common as money_in_common
    from .schedule import check_b2c_settings

    check_b2c_settings(settings_name, max(flt(line.amount) for _doc, line in entries), first.company)
    mop = money_in_common.mode_of_payment_for(settings_name)
    disbursement = frappe.new_doc(B2C)
    disbursement.update({
        "naming_series": "MPESA-B2C-.YYYY.-",
        "company": first.company,
        "posting_date": nowdate(),
        "payment_type": "Mpesa Disbursement",
        "party_type": "Employee",
        "transaction_to_pay_against": CLAIM,
        "mode_of_payment": mop,
        "paid_from": _b2c_paid_from(settings_name, mop, first.company),
        "paid_from_account_currency": "KES",
        "paid_to": payables[0],
        "paid_to_account_currency": "KES",
        "paid_amount": flt(sum(flt(line.amount) for _doc, line in entries), 2),
        "source_exchange_rate": 1,
        "target_exchange_rate": 1,
    })
    for doc, line in entries:
        disbursement.append("references", {
            "reference_doctype": CLAIM, "reference_name": doc.name, "party_type": "Employee", "party": doc.employee,
            "allocated_amount": flt(line.amount), "partyb": line.phone, "currency": "KES", "exchange_rate": 1,
            "claim_payee": line.name,
        })
    disbursement.flags.ignore_permissions = True
    return disbursement


def _b2c_paid_from(settings_name: str, mop, company) -> str:
    """The GL the payouts leave from: the shortcode's Utility Bank Account (as Pay Selected), else
    its Mode of Payment's account (as the disbursement form)."""
    from ..wallet import accounts as wallet_accounts

    bank_account = wallet_accounts.bank_account(settings_name, wallet_accounts.UTILITY)
    paid_from = frappe.db.get_value("Bank Account", bank_account, "account") if bank_account else None
    if not paid_from and mop:
        paid_from = frappe.db.get_value("Mode of Payment Account", {"parent": mop, "company": company},
                                        "default_account")
    if not paid_from:
        frappe.throw(f"Map a Bank Account on Mpesa Settings {settings_name} (Accounting Mapping).")
    return paid_from


# ---------------------------------------------------------------- the claim's form
@frappe.whitelist(methods=["POST"])
def claim_state(claim: str) -> dict:
    """What the claim's form shows: whether payees may be entered, each line's status (brought up
    to date from its payouts), the totals, the supplier suggestions, why the claim cannot be paid
    now and whether its reimbursed total needs repairing. For who can read the claim (a claim not
    saved yet: who may create one, and only the switch)."""
    if not claim or not frappe.db.exists(CLAIM, claim):
        frappe.has_permission(CLAIM, "create", throw=True)
        return {"enabled": settings().allow, "changed": False, "totals": {}, "lines": [], "suggestions": []}
    frappe.has_permission(CLAIM, "read", doc=claim, throw=True)
    s = settings()
    doc = frappe.get_doc(CLAIM, claim)
    before = {r.name: tuple(cstr(r.get(f)) for f in RESULT_FIELDS) for r in doc.get(TABLE) or []}
    lines = sync_lines(claim) if before else []
    changed = any(before.get(r.name) != tuple(cstr(r.get(f)) for f in RESULT_FIELDS) for r in lines)
    totals = dict.fromkeys((PAID, IN_FLIGHT, QUEUED, FAILED, DRAFT, CANCELLED), 0.0)
    for line in lines:
        status = line.get("status") or DRAFT
        totals[status] = flt(totals.get(status, 0) + flt(line.amount), 2)
    submitted = cint(doc.docstatus) == 1
    from .employee_documents import payable_problem as problem_of

    return {
        "enabled": s.allow,
        "changed": changed,
        "totals": totals,
        "lines": [{f: line.get(f) for f in ("name", "idx", *RESULT_FIELDS) if f not in PRIVATE_FIELDS}
                  for line in lines],
        "suggestions": suggestions(lines, s.suggest_after) if lines else [],
        # the reimbursed total's gap is said by "reimbursed", to each reader in their words
        "payable_problem": problem_of(doc, skip_gap=True) if submitted and lines else None,
        "allowed_types": sorted(types_allowing_payees({r.expense_type for r in doc.get("expenses") or []})),
        "reimbursed": _gap(doc) if submitted else None,
        "blocked": _blocked_lines(lines) if lines else [],
    }


def _blocked_lines(lines) -> list:
    """The lines not paid yet whose destination is blocked for claim payees by a staff or self
    payment flagged on another line (blocked_destinations): [{line, idx, claim, flagged_idx}]."""
    open_lines = [line for line in lines if line.get("status") in (DRAFT, QUEUED, FAILED)]
    blocked = blocked_destinations(line.get("destination_key") for line in open_lines)
    return [{"line": line.name, "idx": line.idx, "claim": blocked[line.destination_key].parent,
             "flagged_idx": blocked[line.destination_key].idx}
            for line in open_lines if line.get("destination_key") in blocked
            and line.get("staff_check") != BLOCKED]


def _gap(doc) -> dict | None:
    gap = reimbursed_gap(doc)
    return {"stored": gap[0], "booked": gap[1]} if gap else None


# ---------------------------------------------------------------- suggest a supplier account
def paid_counts(keys) -> dict:
    """How many times each destination was paid from Expense Claims (Paid payee lines)."""
    keys = sorted({k for k in keys if k})
    if not keys:
        return {}
    return {k: cint(n) for k, n in frappe.db.sql(
        f"""SELECT destination_key, COUNT(*) FROM `tab{LINE}`
        WHERE parenttype = %(claim)s AND status = %(paid)s AND destination_key IN %(keys)s
        GROUP BY destination_key""",
        {"claim": CLAIM, "paid": PAID, "keys": tuple(keys)},
    )}


def suggestions(lines, after: int) -> list:
    """Merchant lines whose till, paybill or bank account was paid ``after`` times or more from
    Expense Claims and is no supplier's approved account yet: offered as a new Supplier M-Pesa
    Account (prefilled; the user picks the supplier), approved once and then paid like any
    supplier."""
    if after <= 0:
        return []
    merchants = [line for line in lines if line.get("payee_type") in B2B_TYPES]
    counts = paid_counts(line.get("destination_key") for line in merchants)
    found = []
    for line in merchants:
        count = counts.get(line.get("destination_key"), 0)
        if count < after or approved_supplier_account(line):
            continue
        account = {"account_type": line.payee_type,
                   "expected_name": _without_number(line.get("receiver_name")) or line.payee_name}
        if line.payee_type == BANK:
            account.update(bank_paybill=line.bank_paybill, account_number=line.account_number)
        else:
            account.update(shortcode=line.shortcode, account_number=_account_of(line.account_number, line.payee_type))
        found.append({
            "line": line.name, "idx": line.idx, "count": count, "account": account,
            "message": f"Row #{line.idx}: {describe(line)} ({line.payee_name}) was paid {count} times from Expense "
                       "Claims: make it an approved Supplier M-Pesa Account.",
        })
    return found


# ---------------------------------------------------------------- the reimbursed total
def reimbursed_gap(doc, for_update: bool = False) -> tuple | None:
    """(stored, booked) when HRMS's Total Amount Reimbursed disagrees with the Payment and Journal
    Entries booked against the claim (HRMS's own count), else None. Two payouts booked at the same
    moment can leave it short.

    ``for_update`` (a send or submit under the claim's lock): both sides are read as they are now,
    with locking reads, never a committed total against entries summed in an older snapshot, which
    would refuse a send while another payout of the claim was booked (review R5). The entries'
    rows are locked before the claim's row, the order of R4 and of ERPNext and HRMS (a Payment
    Entry's submit writes its reference rows, then HRMS the claim's total): the reverse order could
    deadlock a send with a desk entry of the same claim (fix check S10)."""
    if for_update:
        booked = booked_now(doc.name)
        stored = frappe.db.get_value(CLAIM, doc.name, ["total_amount_reimbursed", "is_paid", "grand_total"],
                                     as_dict=True, for_update=True) or frappe._dict()
        if cint(stored.is_paid):
            booked = flt(stored.grand_total, 2)
        stored = flt(stored.total_amount_reimbursed, 2)
        return (stored, booked) if abs(stored - booked) > 0.005 else None
    from hrms.hr.doctype.expense_claim.expense_claim import get_total_reimbursed_amount

    stored, booked = flt(doc.get("total_amount_reimbursed"), 2), flt(get_total_reimbursed_amount(doc), 2)
    return (stored, booked) if abs(stored - booked) > 0.005 else None


def booked_now(claim: str) -> float:
    """HRMS's count of what was reimbursed on the claim (get_total_reimbursed_amount: submitted
    Journal Entry rows and Payment Entry references naming it; HRMS 16 leaves out a reference that
    carries an advance voucher, an advance reconciled against the claim), with locking reads: what
    is committed now, not the transaction's snapshot."""
    locking = _locking(True)
    je = frappe.db.sql(
        f"""SELECT COALESCE(SUM(debit_in_account_currency - credit_in_account_currency), 0)
        FROM `tabJournal Entry Account` WHERE reference_name = %s AND docstatus = 1 {locking}""", (claim,))
    advances = ("AND advance_voucher_type IS NULL" if hrms_skips_advance_vouchers()
                and frappe.db.has_column("Payment Entry Reference", "advance_voucher_type") else "")
    pe = frappe.db.sql(
        f"""SELECT COALESCE(SUM(allocated_amount), 0) FROM `tabPayment Entry Reference`
        WHERE reference_name = %s AND docstatus = 1 {advances} {locking}""", (claim,))
    return flt(flt(je[0][0] if je else 0) + flt(pe[0][0] if pe else 0), 2)


_SKIPS_ADVANCE_VOUCHERS = None


def hrms_skips_advance_vouchers() -> bool:
    """Whether the installed HRMS's get_total_reimbursed_amount leaves out Payment Entry references
    with an advance_voucher_type (HRMS 16 does; HRMS 15, 15.64.6 included, counts them): booked_now
    counts as HRMS does, or a send would see a gap that Repair Reimbursed Total never finds. Read
    from HRMS's own code once per process; its major version when the code cannot be read."""
    global _SKIPS_ADVANCE_VOUCHERS
    if _SKIPS_ADVANCE_VOUCHERS is None:
        from hrms.hr.doctype.expense_claim import expense_claim

        try:
            _SKIPS_ADVANCE_VOUCHERS = "advance_voucher_type" in inspect.getsource(
                expense_claim.get_total_reimbursed_amount)
        except (OSError, TypeError):
            import hrms

            _SKIPS_ADVANCE_VOUCHERS = cint(cstr(getattr(hrms, "__version__", "")).split(".")[0]) >= 16
    return _SKIPS_ADVANCE_VOUCHERS


@frappe.whitelist(methods=["POST"])
def repair_reimbursed(claim: str) -> dict:
    """Repair reimbursed total: HRMS's own update_reimbursed_amount sets Total Amount Reimbursed
    (and the claim's status) from the entries booked against the claim. For ADMIN_ROLES who can
    read the claim; under the claim's lock taken first (owed.booking_lock), so no payout is booked
    meanwhile and the entries are counted as committed once it is held, never from an older snapshot
    (review R7)."""
    require(ADMIN_ROLES)
    frappe.get_doc(CLAIM, claim).check_permission("read")
    from hrms.hr.doctype.expense_claim.expense_claim import update_reimbursed_amount

    with owed.booking_lock(CLAIM, claim):
        doc = frappe.get_doc(CLAIM, claim, for_update=True)  # as it is now, under the lock
        if cint(doc.docstatus) != 1:
            frappe.throw(f"{claim} is not submitted.")
        gap = reimbursed_gap(doc)
        if not gap:
            return {"repaired": False, "total": flt(doc.total_amount_reimbursed, 2)}
        update_reimbursed_amount(doc)
        doc.add_comment("Comment", f"Total Amount Reimbursed repaired from {money(gap[0])} to {money(gap[1])} (the "
                                   f"entries booked against the claim) by {frappe.session.user}.")
    return {"repaired": True, "from": gap[0], "to": gap[1]}


# ---------------------------------------------------------------- a line settled outside M-Pesa
@frappe.whitelist(methods=["POST"])
def settle_line(claim: str, line: str, reason: str | None = None) -> dict:
    """Settle Payee Outside M-Pesa: a Draft or Failed payee line of a submitted claim that M-Pesa will
    not pay (the merchant was paid by bank while the till was down, payees were switched off since) is
    Cancelled, with who settled it and how (``reason``: the bank Payment Entry, say). Its money is no
    longer held from the employee (unpaid_lines) and M-Pesa never pays it (make_payouts, check_b2b,
    check_b2c_row and a retry refuse it). For a named M-Pesa Manager (ADMIN_ROLES) who can read the
    claim; under the claim's lock, refused while a payout of the line is queued, on its way, paid or
    in doubt. A Comment on the claim records it."""
    require_named_user()
    require(ADMIN_ROLES)
    doc = frappe.get_doc(CLAIM, claim)
    doc.check_permission("read")
    from .employee_accounts import own_payment_problem

    if own_payment_problem(doc.employee):  # its money would go back to the settler's own share
        frappe.throw(f"{claim} is your own claim: another M-Pesa Manager settles its payees.", frappe.PermissionError)
    reason = cstr(reason).strip()
    if not reason:
        frappe.throw("Say how the payee was paid outside M-Pesa (the bank Payment Entry, say).")
    with owed.doc_lock(CLAIM, claim):
        if cint(frappe.db.get_value(CLAIM, claim, "docstatus")) != 1:
            frappe.throw(f"{claim} is not submitted.")
        # the lines and their payouts as they are now (locking reads): a payout sent meanwhile is seen
        row = next((r for r in sync_lines(claim, for_update=True) if r.name == line), None)
        if not row:
            frappe.throw(f"{line} is not a payee row of {claim}.")
        live = [p for p in payouts_of([row.name], for_update=True) if p.state in (QUEUED, IN_FLIGHT, PAID)]
        if row.status not in (DRAFT, FAILED) or live:
            by = f" by {live[0].doctype} {live[0].name}" if live else ""
            frappe.throw(f"Payee row #{row.idx} of {claim} ({row.payee_name}) is {row.status}{by}: only a Draft or "
                         "Failed line is settled outside M-Pesa.")
        note = f"Settled outside M-Pesa by {frappe.session.user}: {reason}"
        frappe.db.set_value(LINE, row.name, {"status": CANCELLED, "error_message": note}, update_modified=False)
        doc.add_comment("Comment", f"Payee row #{row.idx} ({row.payee_name}, {describe(row, masked=True)}, KES "
                                   f"{money(row.amount)}) "
                                   f"settled outside M-Pesa by {frappe.session.user}: {reason}. M-Pesa will not pay "
                                   "it, and its amount is no longer held from the employee.")
    return {"line": row.name, "status": CANCELLED}


# ---------------------------------------------------------------- casual payees: paste or upload
_HEADERS = {
    "payee_name": ("name", "payee"),
    "phone": ("phone", "mobile", "msisdn", "number", "tel"),
    "amount": ("amount", "kes", "ksh", "pay"),
}


def parse_text(text) -> list:
    """Pasted rows: a tab between columns (copied from a spreadsheet), else commas (quotes kept)."""
    rows = []
    for raw in cstr(text).splitlines():
        if not raw.strip():
            continue
        cells = raw.split("\t") if "\t" in raw else next(csv.reader([raw]))
        rows.append([c.strip() for c in cells])
    return rows


def parse_amount(value) -> float | None:
    """'1,500', 'KES 1500', 1500.0 -> 1500.0; None when it is no number."""
    text = re.sub(r"(?i)\b(kes|kshs?|sh)\b\.?", "", cstr(value)).replace(",", "").replace(" ", "")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _header(cells) -> dict | None:
    """{field: column} when the first row names the columns (in any order)."""
    found = {}
    for i, cell in enumerate(cells):
        text = cstr(cell).strip().lower()
        if parse_amount(cell) is not None or normalize_msisdn(cell):
            continue
        for field, words in _HEADERS.items():
            if field not in found and any(w in text for w in words):
                found[field] = i
                break
    return found if len(found) == len(_HEADERS) else None


def table_rows(table) -> list:
    """Rows of name, phone, amount (a first row naming the columns may come first, in any order):
    [{row, payee_name, phone, amount_text}]. Without names, the phone is the first cell after the
    name that is a phone number, and the cells after it are the amount ('1,500' split by a comma
    comes back whole)."""
    table = [[cstr(c).strip() if c is not None else "" for c in row or []] for row in table or []]
    table = [(n, row) for n, row in enumerate(table, start=1) if any(row)]
    if not table:
        return []
    order = _header(table[0][1])
    if order:
        table = table[1:]
    rows = []
    for n, cells in table:
        if order:
            name, phone, amount = (cells[order[f]] if order[f] < len(cells) else "" for f in ("payee_name", "phone",
                                                                                              "amount"))
        else:
            name, rest = cells[0], cells[1:]
            at = next((i for i, cell in enumerate(rest) if normalize_msisdn(cell)), 0)
            phone = rest[at] if rest else ""
            amount = "".join(rest[at + 1:])
        rows.append(frappe._dict(row=n, payee_name=name, phone=phone, amount_text=amount))
    return rows


def check_pasted(rows, claimant=None, existing=(), found=None) -> list:
    """Each pasted row checked: a name, a number B2C may pay that is not another network's (B2C pays
    M-Pesa lines only; a number moved from another network is added by hand; a number of a block no
    network list names yet is taken, and the claim's save warns), whole shillings B2C may pay, never
    an employee's phone, and no phone twice (in the list, or on the claim already)."""
    found = found or frappe._dict(destinations={}, bank_numbers={})
    seen = {normalize_msisdn(p) for p in existing or [] if normalize_msisdn(p)}
    checked = []
    for r in rows:
        errors = []
        if not cstr(r.payee_name).strip():
            errors.append("no name")
        msisdn = payable_msisdn(r.phone)
        if not msisdn:
            errors.append(f"{r.phone or 'no phone'} is not a Kenyan mobile number M-Pesa can pay ({FORMATS})")
        elif network_of(msisdn) not in ("Safaricom", None):
            errors.append(f"{to_local(msisdn)} does not start like a Safaricom number (B2C pays M-Pesa lines only: add "
                          "it by hand if it is an M-Pesa line moved from another network)")
        amount = parse_amount(r.amount_text)
        if amount is None or amount <= 0:
            errors.append("the amount must be more than 0")
        elif amount != int(amount):
            errors.append("M-Pesa pays whole shillings")
        elif not B2C_MIN_AMOUNT <= amount <= B2C_MAX_AMOUNT:
            errors.append(f"M-Pesa B2C pays KES {B2C_MIN_AMOUNT} to {B2C_MAX_AMOUNT:,}")
        if msisdn:
            who = found.destinations.get(f"B2C:{msisdn}")
            user = (found.get("users") or {}).get(f"B2C:{msisdn}")
            if user and user == frappe.session.user:
                errors.append("your own phone: staff are paid by reimbursing their claims")
            elif who:
                errors.append("the claimant's own phone" if who == claimant else
                              "an employee's phone: an employee is paid by reimbursing the claim")
            elif user:
                errors.append("the phone of a user of this site (staff): staff are paid by reimbursing their claims")
            elif (found.get("blocked") or {}).get(f"B2C:{msisdn}"):
                flagged = found.blocked[f"B2C:{msisdn}"]
                errors.append(f"blocked for claim payees: M-Pesa paid it under a staff member's name ({flagged.parent}, "
                              f"payee row #{flagged.idx})")
            if msisdn in seen:
                errors.append("this phone is on the claim already, or earlier in the list")
            seen.add(msisdn)
        checked.append({"row": r.row, "payee_name": cstr(r.payee_name).strip(), "phone": msisdn or cstr(r.phone),
                        "amount": amount, "errors": errors})
    return checked


@frappe.whitelist(methods=["POST"])
def parse_payees(text: str | None = None, file_url: str | None = None, claim: str | None = None,
                 employee: str | None = None, existing=None) -> dict:
    """The Paste payees dialog: pasted rows (name, phone, amount) or an uploaded CSV / XLSX,
    checked row by row (check_pasted). Adds nothing: the form appends the good rows to the claim,
    and its save checks them again. For who may change the claim (or create one, before its first
    save)."""
    if claim and frappe.db.exists(CLAIM, claim):
        frappe.has_permission(CLAIM, "write", doc=claim, throw=True)
        employee = frappe.db.get_value(CLAIM, claim, "employee")
    else:
        frappe.has_permission(CLAIM, "create", throw=True)
    table = _read_file(file_url) if file_url else parse_text(text)
    rows = table_rows(table)
    if len(rows) > PASTE_LIMIT:
        frappe.throw(f"Paste at most {PASTE_LIMIT} payees at a time.")
    if isinstance(existing, str):
        existing = frappe.parse_json(existing)
    # round 4.3: a phone M-Pesa paid before takes the name M-Pesa paid it under, never the pasted one
    names = mpesa_names(payable_msisdn(r.phone) for r in rows)
    known = [names.get(payable_msisdn(r.phone)) for r in rows]
    typed = [cstr(r.payee_name).strip() for r in rows]
    for r, k in zip(rows, known):
        if k:
            r.payee_name = k.name
    checked = check_pasted(rows, employee, existing or [], employee_destinations())
    for r, k, name in zip(checked, known, typed):
        if k:
            r.update(mpesa_name=k.name, mpesa_name_from=name_from(k), typed_name=name)
    bad = sum(1 for r in checked if r["errors"])
    return {"rows": checked, "good": len(checked) - bad, "bad": bad}


def _read_file(file_url: str) -> list:
    """An uploaded CSV, TXT or XLSX the user may read, as rows of cells."""
    file = frappe.get_doc("File", {"file_url": file_url})
    file.check_permission("read")
    name = cstr(file.file_name or file_url).lower()
    if name.endswith(".xlsx"):
        from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

        return read_xlsx_file_from_attached_file(file_url=file_url) or []
    if not name.endswith((".csv", ".txt")):
        frappe.throw("Upload a CSV or XLSX file (name, phone, amount).")
    content = file.get_content()
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    return parse_text(content)
