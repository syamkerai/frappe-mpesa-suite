"""M-Pesa Bulk Payout: casual wages and overtime paid by B2C in bulk, to casuals and employees alike.

A sheet (DocType M-Pesa Bulk Payout, rows M-Pesa Bulk Payout Row) lists who is paid, how much, and
for what: each row's Expense Claim Type (its account for the company), cost center, project and
accounting dimensions. Its rows name no ERPNext party: they are paid through money_out.partyless
(registered in hooks.py ``mpsa_partyless_payouts``), whose handler here checks, books and reconciles
them.

Who does what:
- a preparer (Accounts User, HR User, M-Pesa Manager...) adds the rows, many at once with Paste Rows
  (``parse_rows``: claim_payees' paste reader, with optional Expense Type, Project and Cost Center
  columns) and saves the draft;
- a named M-Pesa Manager or Accounts Manager submits it: that is the approval. When a row's number
  is new to M-Pesa, or M-Pesa knows it under another name, the approver ticks that they checked them
  (``names_checked``, required on the server at the submit);
- someone in PREPARE_ROLES presses Pay by M-Pesa (``pay``): one draft party-less B2C Payment
  Disbursement for the Draft rows; Retry Failed (``retry_failed``) makes one for the rows M-Pesa
  failed; an M-Pesa Payer sends each with Send via M-Pesa (maker-checker as M-Pesa Suite Settings
  says).

Nobody pays themselves (money_out.self_pay): whoever saves, approves, prepares the payout of or sends
a sheet is refused when one of its rows pays their phone or their name. Employees are paid like
anyone else; a row whose phone is an employee's names the Employee (informational, for PAYE).

A row is sent again only when M-Pesa said it Failed. A row whose fate is unknown (no Result, a
transport error) is asked about (Check with M-Pesa Now, and the 15-minute Transaction Status sweep),
never re-sent; an M-Pesa Manager who checked the statement may Mark it as Not Paid.

Each row's status follows its payouts (``sync_rows``, from the B2C rows that name it): Draft, Queued
(in a batch not sent yet), In Flight (sent, waiting; "Checking with M-Pesa" when the send's answer
never came), Paid, Failed, On Hold (its KES 10 test was paid under another name), Cancelled (settled
outside M-Pesa, or the sheet was cancelled).

"Pay New Numbers KES 10 First" (the sheet's ``verify_new_numbers``, defaulting from M-Pesa Suite
Settings): a number M-Pesa never paid first gets KES 10 (its paying row is ``<row>/test``). When the
Result's name matches the name given, the row is Verified and its rest is paid by the next Pay by
M-Pesa; when not, the row is On Hold for an M-Pesa Manager (Release Held Row, or Settle Outside
M-Pesa).

Booking: one Journal Entry per sheet, posted once no row waits for M-Pesa (``book_sheet``): every
Paid payout not booked yet, the debit lines grouped by expense account, cost center, project and
accounting dimensions, the credit on the shortcode's Utility GL. A row that waits more than
``bulk_payout_book_after_hours`` (M-Pesa Suite Settings) after its send no longer holds the others;
a payout paid later gets a small Journal Entry of its own. Each payout's statement line takes its
own amount of the journal (partial allocation: ``statement_entry``).
"""

import importlib
import json
from collections import Counter

import frappe
from frappe.utils import add_to_date, cint, cstr, flt, get_datetime, get_url, getdate, now_datetime, nowdate

from ..daraja import apis
from ..daraja import money_out as daraja
from ..daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT
from ..permissions import ADMIN_ROLES, PAYOUT_ROLES, PREPARE_ROLES, require, require_named_user
from ..utils import privacy
from ..utils.phone import FORMATS, is_safaricom, network_of, normalize_msisdn, payable_msisdn, to_local
from . import claim_payees as cp
from . import owed, partyless
from . import party_accounts as pa

SHEET = "M-Pesa Bulk Payout"
ROW = "M-Pesa Bulk Payout Row"
TABLE = "rows"
DISB, B2C_ROW, REQUEST = partyless.DISB, partyless.ROW, partyless.REQUEST
JE = "Journal Entry"
SUITE = "M-Pesa Suite Settings"
CLAIM_TYPE = "Expense Claim Type"
STATUS_QUERY = "Mpesa B2C Status Query"

# Who approves (submits) a sheet, and who books it at once (Book Now)
APPROVER_ROLES = ("M-Pesa Manager", "Accounts Manager")
BOOK_ROLES = ("M-Pesa Manager", "Accounts Manager")

# A row's status
DRAFT, QUEUED, IN_FLIGHT, PAID, FAILED, ON_HOLD, CANCELLED = (
    "Draft", "Queued", "In Flight", "Paid", "Failed", "On Hold", "Cancelled")
LIVE = (QUEUED, IN_FLIGHT)
# The sheet's status
S_DRAFT, TO_PAY, PAYING, PARTLY, S_PAID, S_CANCELLED = "Draft", "To Pay", "Paying", "Partly Paid", "Paid", "Cancelled"
# The KES 10 test of a new number (verification)
TESTING, VERIFIED, DIFFERS, RELEASED = "Testing", "Verified", "Name Differs", "Released"
TEST_AMOUNT = 10
TEST_SUFFIX = "/test"
# The number's M-Pesa name at entry (known_flag)
KNOWN, NAME_DIFFERS, NEW = "Known", "Differs", "New"
# Written by the server from the row's payouts; a save after submit keeps what the database holds
RESULT_FIELDS = ("status", "progress", "payout", "payout_request", "sent_on", "attempts", "receipt", "paid_on",
                 "paid_amount", "receiver_name", "name_verdict", "name_note", "error_message", "journal_entry",
                 "bookings", "verification", "test_request", "test_receipt")
HEADER_FIELDS = ("status", "progress", "paid_amount", "failed_amount", "left_amount")
# Pasted at most this many rows at a time
PASTE_LIMIT = 1000
# The optional columns of a pasted sheet (claim_payees.read_rows): the strongest word first
EXTRA_COLUMNS = {
    "expense_type": ("expense type", "expense", "type"),
    "project": ("project",),
    "cost_center": ("cost cent", "cost"),
}
# The daily alert lists a row waiting this long after its send
ATTENTION_HOURS = 24
# Common B2C failures in plain words: (result codes, words of the result's description, kind, what to do)
FAILURES = (
    (("1",), ("insufficient", "balance is insufficient"), "insufficient",
     "The shortcode's Utility account had too little money: top it up, then press Retry Failed."),
    (("2040", "7", "12", "SFC_IC0003"), ("not registered", "unresolved receiver", "receiver party", "credit party",
                                         "invalid receiver"), "receiver",
     "M-Pesa does not know this number as a person's M-Pesa line (not registered, or not a phone M-Pesa pays): "
     "check it with the person, then Correct Phone and Retry."),
    (("2", "3", "4", "5", "8", "17"), ("limit",), "limit",
     "M-Pesa refused it for a limit (the payment's size, or the person's daily or balance limit): pay it later or "
     "in parts, then press Retry Failed."),
    (("2001", "8006"), ("initiator", "security credential"), "initiator",
     "M-Pesa refused the shortcode's initiator: an M-Pesa Manager fixes it on Mpesa Settings, then Retry Failed."),
)


def settings() -> frappe._dict:
    """M-Pesa Suite Settings for the sheets: the book-after hours (2 unless set; 0 = wait for every
    row) and whether a new sheet pays new numbers KES 10 first."""
    after = frappe.db.get_single_value(SUITE, "bulk_payout_book_after_hours")
    return frappe._dict(book_after_hours=2 if after in (None, "") else cint(after),
                        verify=bool(cint(frappe.db.get_single_value(SUITE, "verify_new_numbers"))))


def money(value) -> str:
    return owed.money(value)


def _locking(for_update: bool) -> str:
    return "FOR UPDATE" if for_update and frappe.db.db_type == "mariadb" else ""


def split_paying_row(value) -> tuple:
    """(the sheet's row, is the KES 10 test) of a B2C row's paying row: 'abc' or 'abc/test'."""
    value = cstr(value).strip()
    if value.endswith(TEST_SUFFIX):
        return value[: -len(TEST_SUFFIX)], True
    return value, False


def bookings_of(row) -> dict:
    """{B2C Disbursement Request: Journal Entry} of a row's paid payouts that are booked."""
    try:
        value = json.loads(cstr(row.get("bookings")) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# Compared as numbers, or as times to the second: the database gives Decimal('1500.000000000') for 1500.0
NUMERIC_FIELDS = ("paid_amount", "attempts", "failed_amount", "left_amount")
TIME_FIELDS = ("sent_on", "paid_on")


def _norm(field: str, value) -> str:
    """A stored or computed value as compared, so a value read back is never seen as changed."""
    if value is None or value == "":
        return ""
    if field in NUMERIC_FIELDS:
        return f"{flt(value):.2f}"
    if field in TIME_FIELDS:
        return cstr(value)[:19]
    return cstr(value)


def live_journal(journal) -> tuple:
    """(name, docstatus) of the Journal Entry that books in ``journal``'s place: ``journal`` itself
    while it is not cancelled; once cancelled, the copy that amended it (ERPNext's Cancel and
    Amend: ``amended_from``, followed down the chain, a submitted copy before a draft); (None,
    None) when it is gone, or was cancelled with no live amended copy."""
    name, seen = cstr(journal).strip(), set()
    while name and name not in seen:
        seen.add(name)
        status = frappe.db.get_value(JE, name, "docstatus")
        if status is None:
            return None, None
        if cint(status) != 2:
            return name, cint(status)
        copies = frappe.get_all(JE, filters={"amended_from": name}, fields=["name", "docstatus"],
                                order_by="creation asc")
        live = [c for c in copies if cint(c.docstatus) < 2]
        if live:
            best = max(live, key=lambda c: cint(c.docstatus))
            return best.name, cint(best.docstatus)
        name = copies[-1].name if copies else None
    return None, None


def _journal_status(journal) -> int | None:
    """The docstatus of the live journal that books in ``journal``'s place (``live_journal``), or
    None when there is none."""
    return live_journal(journal)[1]


def receipt_entries(receipts) -> dict:
    """{receipt (upper case): docstatus} of the live entries that carry a payout's receipt: a
    Journal Entry with it as Cheque No (a voucher made from its bank line, Bank Reconciliation's
    Create Voucher), a Payment Entry with it as Reference No. A sheet's own journals carry the
    sheet's name, never a receipt. Such a payout is in the books already: the sheet's journal never
    books it again (b2c_postprocess_service.entry_carrying_receipt keeps it from any other booking)."""
    given = {cstr(r).strip() for r in receipts or () if cstr(r).strip()}
    keys = sorted(given | {r.upper() for r in given})
    if not keys:
        return {}
    found = {}
    for doctype, field in ((JE, "cheque_no"), ("Payment Entry", "reference_no")):
        for value, status in frappe.db.sql(
                f"SELECT {field}, MAX(docstatus) FROM `tab{doctype}` WHERE {field} IN %(keys)s AND docstatus < 2 "
                f"GROUP BY {field}", {"keys": tuple(keys)}):
            key = cstr(value).strip().upper()
            found[key] = max(found.get(key, 0), cint(status))
    return found


# ---------------------------------------------------------------- the sheet (DocType events)
def validate_sheet(doc) -> None:
    """M-Pesa Bulk Payout validate, on every save of a draft and at the submit (the approval): the
    rows cleaned and checked (a payable Safaricom number, whole shillings 10 to 250,000, an Expense
    Type with an account for the company, a cost center of the company and an open project), the
    name M-Pesa paid each phone under before (round 4.3, locked), the employee whose phone it is,
    the same phone twice for the same expense type warned about, and nobody paid on what they
    prepare or approve."""
    at_submit = cint(doc.docstatus) == 1
    rows = doc.get(TABLE) or []
    if not at_submit:
        doc.status = S_DRAFT
    if getattr(doc, "_action", None) != "submit":
        # ticked at the submit only (the approval), never stored on a draft
        doc.names_checked = 0
        doc.approved_by = doc.approved_on = doc.names_summary = None
    if not at_submit or getattr(doc, "_action", None) == "submit":
        clear_results(rows)  # a draft and its approval paid nothing: what an amend copied (or the API set) goes
    if not rows:
        frappe.throw("Add the payees: name, phone and amount (Paste Rows takes many at once).")
    currency = frappe.db.get_value("Company", doc.company, "default_currency") if doc.get("company") else None
    if currency and currency != "KES":
        frappe.throw(f"{doc.company} keeps its books in {currency}. M-Pesa pays KES only.")
    ctx = _context(doc, rows)
    check_credit_dimensions(ctx)
    warnings, seen = [], {}
    for row in rows:
        clean_row(row, doc, ctx, warnings)
        seen.setdefault((row.phone, row.expense_type), []).append(row.idx)
    for (_phone, expense_type), found in seen.items():
        if len(found) > 1:
            warnings.append(f"Rows {', '.join(f'#{i}' for i in found)} pay the same phone for {expense_type}: check "
                            "that this is not one payment entered twice.")
    problem = self_pay_problem(frappe.session.user, rows, "approve" if at_submit else "prepare")
    if problem:
        frappe.throw(problem, frappe.PermissionError)
    doc.total_rows = len(rows)
    doc.total_amount = flt(sum(flt(r.amount) for r in rows), 2)
    if warnings:
        frappe.msgprint("<br>".join(warnings), title="Check the payees", indicator="orange")


def clear_results(rows) -> None:
    for row in rows:
        for field in RESULT_FIELDS:
            setattr(row, field, None)
        row.status = DRAFT
        row.attempts = 0
        row.paid_amount = 0


def _context(doc, rows) -> frappe._dict:
    """What every row's check reads once: the names M-Pesa paid the phones under, the Known Name
    records, the employees' phones, and the accounts, cost centers and projects (memoised)."""
    from . import known_names

    phones = [m for m in (payable_msisdn(r.get("phone")) for r in rows) if m]
    return frappe._dict(
        company=doc.get("company"),
        names=cp.mpesa_names(phones),
        records=known_names.lookup(f"B2C:{m}" for m in phones),
        employees=cp.employee_destinations().destinations,
        accounts={}, cost_centers={}, projects={}, limits={}, dimension_values={}, report_types={},
        default_cost_center=frappe.db.get_value("Company", doc.get("company"), "cost_center") if doc.get(
            "company") else None,
        shortcode=sheet_shortcode(doc),
        dimensions=dimension_rules(doc.get("company")),
        filters=None,  # the Accounting Dimension Filters, read at the first row (dimension_filters)
    )


def sheet_shortcode(doc) -> str | None:
    """The B2C shortcode the sheet pays from: its own (Pay From), else the company's only one that may
    send B2C and has an initiator (what Pay by M-Pesa picks: claim_payees.pick_settings); None when
    that is not one."""
    if cstr(doc.get("mpesa_settings")).strip():
        return doc.mpesa_settings
    has_company = frappe.get_meta("Mpesa Settings").has_field("company")
    filters = {apis.B2C: 1, **({"company": doc.company} if has_company and doc.get("company") else {})}
    found = [s.name for s in frappe.get_all("Mpesa Settings", filters=filters, fields=["name", "initiator_name"])
             if s.get("initiator_name")]
    return found[0] if len(found) == 1 else None


def _limit_problem(amount, ctx) -> str | None:
    """services.b2c_request_service.payout_limit_problem for the sheet's shortcode (its Transaction
    Limit, and the Minimum B2C Payout), once per amount."""
    key = flt(amount, 2)
    if key not in ctx.limits:
        from ..services import b2c_request_service

        ctx.limits[key] = b2c_request_service.payout_limit_problem(key, ctx.shortcode)
    return ctx.limits[key]


def clean_row(row, doc, ctx, warnings) -> None:
    """One row as it is kept: the phone as 2547..., M-Pesa's earlier name for it (locked; what was
    typed kept as Name Given when it differs, round 4.3), the defaults filled, the accounting
    checked, the employee named."""
    idx = row.get("idx")
    row.payee_name = cstr(row.get("payee_name")).strip()
    given, stale = cstr(row.get("typed_name")).strip(), cstr(row.get("mpesa_name")).strip()
    row.mpesa_name = row.mpesa_name_from = row.typed_name = None
    msisdn = payable_msisdn(row.get("phone"))
    if not msisdn:
        frappe.throw(f"Row #{idx}: {cstr(row.get('phone')) or 'no phone'} is not a Kenyan mobile number M-Pesa can pay "
                     f"({FORMATS}).")
    row.phone = msisdn
    known = ctx.names.get(msisdn)
    if known:
        if row.payee_name and row.payee_name not in (known.name, stale):
            given = row.payee_name  # typed (by hand, a paste or the API), not a name M-Pesa filled in
        row.typed_name = given if given and not pa.names_match(given, known.name) else None
        row.payee_name = row.mpesa_name = known.name
        row.mpesa_name_from = cp.name_from(known)
        row.known_flag = NAME_DIFFERS if row.typed_name else KNOWN
        if row.typed_name:
            warnings.append(f"Row #{idx}: M-Pesa paid {to_local(msisdn)} as {known.name} before, not "
                            f"{row.typed_name!r}: check the number with the person.")
    else:
        if stale and row.payee_name == stale:
            row.payee_name = given  # the name filled in for the row's earlier number, not this one
        row.known_flag = KNOWN if f"B2C:{msisdn}" in ctx.records else NEW
    if not row.payee_name:
        frappe.throw(f"Row #{idx}: enter the Payee Name.")
    if not is_safaricom(msisdn):
        warnings.append(f"Row #{idx}: {to_local(msisdn)} does not start like a Safaricom number, and B2C pays M-Pesa "
                        "lines only.")
    try:
        daraja.whole_shillings(row.get("amount"))
    except ValueError as e:
        frappe.throw(f"Row #{idx}: {e}")
    if not B2C_MIN_AMOUNT <= flt(row.amount) <= B2C_MAX_AMOUNT:
        frappe.throw(f"Row #{idx}: M-Pesa B2C pays KES {B2C_MIN_AMOUNT} to {B2C_MAX_AMOUNT:,} to a phone.")
    # the shortcode's Transaction Limit and the Minimum B2C Payout, as the send checks them: refused
    # here, not when the batch is sent
    problem = _limit_problem(row.amount, ctx)
    if problem:
        frappe.throw(f"Row #{idx}: {problem}" + (f" (Mpesa Settings {ctx.shortcode})." if ctx.shortcode and
                                                  "transaction limit" in problem else "."))
    row.expense_type = cstr(row.get("expense_type")).strip() or cstr(doc.get("expense_type")).strip() or None
    if not row.expense_type:
        frappe.throw(f"Row #{idx}: pick its Expense Type, or the sheet's default Expense Type.")
    row.expense_account = _expense_account(row.expense_type, ctx, idx)
    row.cost_center = (cstr(row.get("cost_center")).strip() or cstr(doc.get("cost_center")).strip()
                       or ctx.default_cost_center or None)
    if not row.cost_center:
        frappe.throw(f"Row #{idx}: pick its Cost Center, or the sheet's default Cost Center.")
    _check_cost_center(row.cost_center, ctx, idx)
    row.project = cstr(row.get("project")).strip() or cstr(doc.get("project")).strip() or None
    if row.project:
        _check_project(row.project, ctx, idx)
    _check_dimensions(row, ctx, idx)
    row.employee = ctx.employees.get(f"B2C:{msisdn}") or None
    if not row.get("status"):
        row.status = DRAFT


def expense_account(expense_type: str, company: str) -> str | None:
    """The Expense Claim Type's account for the company (its Accounts table)."""
    if not expense_type or not company:
        return None
    return frappe.db.get_value("Expense Claim Account", {"parent": expense_type, "parenttype": CLAIM_TYPE,
                                                         "company": company}, "default_account")


def _expense_account(expense_type: str, ctx, idx) -> str:
    if expense_type not in ctx.accounts:
        problem, account = None, None
        if not frappe.db.exists(CLAIM_TYPE, expense_type):
            problem = f"Expense Claim Type {expense_type} not found."
        else:
            account = expense_account(expense_type, ctx.company)
            values = frappe.db.get_value("Account", account, ["company", "is_group", "account_currency", "disabled"],
                                         as_dict=True) if account else None
            if not account:
                problem = (f"{expense_type} has no account for {ctx.company}: set it on the Expense Claim Type "
                           "(Accounts).")
            elif not values:
                problem = f"{expense_type}'s account {account} not found."
            elif values.company and values.company != ctx.company:
                problem = f"{expense_type}'s account {account} is not {ctx.company}'s."
            elif cint(values.is_group) or cint(values.get("disabled")):
                problem = f"{expense_type}'s account {account} is a group or disabled account."
            elif values.account_currency and values.account_currency != "KES":
                problem = f"{expense_type}'s account {account} is in {values.account_currency}. M-Pesa pays KES only."
        ctx.accounts[expense_type] = (account, problem)
    account, problem = ctx.accounts[expense_type]
    if problem:
        frappe.throw(f"Row #{idx}: {problem}")
    return account


def _check_cost_center(cost_center: str, ctx, idx) -> None:
    if cost_center not in ctx.cost_centers:
        values = frappe.db.get_value("Cost Center", cost_center, ["company", "is_group", "disabled"], as_dict=True)
        problem = None
        if not values:
            problem = f"Cost Center {cost_center} not found."
        elif values.company and values.company != ctx.company:
            problem = f"Cost Center {cost_center} is not {ctx.company}'s."
        elif cint(values.is_group):
            problem = f"{cost_center} is a group Cost Center: pick one under it."
        elif cint(values.get("disabled")):
            problem = f"Cost Center {cost_center} is disabled."
        ctx.cost_centers[cost_center] = problem
    if ctx.cost_centers[cost_center]:
        frappe.throw(f"Row #{idx}: {ctx.cost_centers[cost_center]}")


def _check_project(project: str, ctx, idx) -> None:
    if project not in ctx.projects:
        values = frappe.db.get_value("Project", project, ["company", "status", "is_active"], as_dict=True)
        problem = None
        if not values:
            problem = f"Project {project} not found."
        elif values.company and values.company != ctx.company:
            problem = f"Project {project} is not {ctx.company}'s."
        elif values.status in ("Completed", "Cancelled") or cstr(values.get("is_active")) == "No":
            state = values.status if values.status != "Open" else "not active"
            problem = f"Project {project} is {state}: pick an open one."
        ctx.projects[project] = problem
    if ctx.projects[project]:
        frappe.throw(f"Row #{idx}: {ctx.projects[project]}")


# ---------------------------------------------------------------- accounting dimensions (as ERPNext checks them)
DIMENSIONS_MODULE = "erpnext.accounts.doctype.accounting_dimension.accounting_dimension"
DIMENSION_FILTERS_MODULE = "erpnext.accounts.doctype.accounting_dimension_filter.accounting_dimension_filter"
PL, BS = "Profit and Loss", "Balance Sheet"


def dimension_rules(company) -> list:
    """The active accounting dimensions (``dimensions``: the rows' own fields, project apart) as ERPNext
    checks them on a ledger line of ``company`` (GL Entry.validate_dimensions_for_pl_and_bs, from
    accounting_dimension.get_checks_for_pl_and_bs_accounts, in v15 and v16): _dict(fieldname, label,
    doctype, mandatory_for_pl, mandatory_for_bs, default: the company's default value)."""
    fields = dimensions()
    if not fields:
        return []
    checks = getattr(importlib.import_module(DIMENSIONS_MODULE), "get_checks_for_pl_and_bs_accounts", None)
    per = {d.get("fieldname"): d for d in ((checks() if callable(checks) else None) or [])
           if d.get("company") == company and not cint(d.get("disabled"))}
    out = []
    for field in fields:
        meta = frappe.db.get_value("Accounting Dimension", {"fieldname": field}, ["label", "document_type"],
                                   as_dict=True) or frappe._dict()
        check = frappe._dict(per.get(field) or {})
        out.append(frappe._dict(
            fieldname=field, label=meta.get("label") or check.get("label") or field, doctype=meta.get("document_type"),
            mandatory_for_pl=cint(check.get("mandatory_for_pl")), mandatory_for_bs=cint(check.get("mandatory_for_bs")),
            default=cstr(check.get("default_dimension")).strip() or None))
    return out


def dimension_filters() -> dict:
    """ERPNext's Accounting Dimension Filters, {(dimension field, account): rule} (accounting_dimension_filter
    .get_dimension_filter_map, in v15 and v16: GL Entry.validate_allowed_dimensions); {} without it."""
    try:
        module = importlib.import_module(DIMENSION_FILTERS_MODULE)
    except ImportError:
        return {}
    found = getattr(module, "get_dimension_filter_map", None)
    return (found() if callable(found) else None) or {}


def _report_type(account, ctx) -> str | None:
    if account not in ctx.report_types:
        ctx.report_types[account] = frappe.db.get_value("Account", account, "report_type") if account else None
    return ctx.report_types[account]


def _dimension_value_problem(rule, value, ctx) -> str | None:
    """A dimension's value exists and, when its DocType has a company, is the sheet company's (as
    accounts_controller.validate_company_in_accounting_dimension says it)."""
    key = (rule.doctype, value)
    if key not in ctx.dimension_values:
        problem = None
        if not frappe.db.exists(rule.doctype, value):
            problem = f"{rule.label} {value} not found."
        elif frappe.get_meta(rule.doctype).has_field("company"):
            company = frappe.db.get_value(rule.doctype, value, "company")
            if company and company != ctx.company:
                problem = f"{rule.doctype}: {value} does not belong to the Company: {ctx.company}."
        ctx.dimension_values[key] = problem
    return ctx.dimension_values[key]


def _check_dimensions(row, ctx, idx) -> None:
    """The row's accounting dimensions checked as ERPNext checks the journal's line when it posts, so
    the journal is never refused after the money left: a dimension mandatory for the account's kind
    (Profit and Loss, Balance Sheet) at the company is set (its company default when the row has none,
    as ERPNext's own forms fill it in; refused when there is none); a value exists and belongs to the
    company; and the Accounting Dimension Filters of the expense account allow it."""
    kind = _report_type(row.get("expense_account"), ctx) if ctx.dimensions else None
    for rule in ctx.dimensions:
        value = cstr(row.get(rule.fieldname)).strip() or None
        mandatory = (kind == PL and rule.mandatory_for_pl) or (kind == BS and rule.mandatory_for_bs)
        if not value and mandatory:
            if not rule.default:
                frappe.throw(f"Row #{idx}: Accounting Dimension {rule.label} is required for '{kind}' account "
                             f"{row.expense_account}: pick its {rule.label}, or set a default {rule.label} for "
                             f"{ctx.company} on the Accounting Dimension.")
            value = rule.default
            setattr(row, rule.fieldname, value)
        if value and rule.doctype:
            problem = _dimension_value_problem(rule, value, ctx)
            if problem:
                frappe.throw(f"Row #{idx}: {problem}")
    if ctx.filters is None:
        ctx.filters = dimension_filters()
    for (dimension, account), rule in ctx.filters.items():
        if account != row.get("expense_account"):
            continue
        value = cstr(row.get(dimension)).strip()
        label = cstr(dimension).replace("_", " ").title()
        allowed = list(rule.get("allowed_dimensions") or [])
        if cint(rule.get("is_mandatory")) and not value:
            frappe.throw(f"Row #{idx}: {label} is mandatory for account {account} (Accounting Dimension Filter).")
        if value and ((rule.get("allow_or_restrict") == "Allow" and value not in allowed)
                      or (rule.get("allow_or_restrict") == "Restrict" and value in allowed)):
            frappe.throw(f"Row #{idx}: {value} is not allowed for {label} against account {account} (Accounting "
                         "Dimension Filter).")


def check_credit_dimensions(ctx) -> None:
    """The journal's credit line (the shortcode's M-Pesa account, a Balance Sheet account) carries no
    row's dimensions, only the company's default of each one mandatory for Balance Sheet accounts
    (build_journal): one with no default would have ERPNext refuse the journal after the money left."""
    missing = [r.label for r in ctx.dimensions if r.mandatory_for_bs and not r.default]
    if missing:
        frappe.throw(f"Accounting Dimension {', '.join(missing)} is required for 'Balance Sheet' accounts at "
                     f"{ctx.company}, and the sheet's journal credits the shortcode's M-Pesa account with the "
                     f"company's default: set a default {', '.join(missing)} for {ctx.company} on the Accounting "
                     "Dimension.")


def self_pay_rows(rows) -> list:
    """The people ``rows`` pay, as money_out.self_pay reads them: the phone, the name given and the
    name M-Pesa paid the phone under before."""
    return [{"phone": r.get("phone"), "payee_name": cstr(r.get("typed_name")).strip() or r.get("payee_name"),
             "mpesa_name": r.get("mpesa_name") or r.get("payee_name"), "label": f"Row #{r.get('idx')}"}
            for r in rows or []]


def self_pay_problem(user, rows, action: str) -> str | None:
    """Why ``user`` may not ``action`` (prepare, approve, pay) a sheet with ``rows``: one pays their
    phone or their name."""
    from . import self_pay

    return self_pay.self_pay_problem(user, self_pay_rows(rows), action)


def name_targets(rows) -> list:
    """Per row, (its known-name key, the name given): what known_names.summary compares."""
    return [(f"B2C:{normalize_msisdn(r.get('phone'))}" if normalize_msisdn(r.get("phone")) else None,
             cstr(r.get("typed_name")).strip() or cstr(r.get("payee_name")).strip() or None) for r in rows or []]


def names_summary(rows) -> dict | None:
    """The approval's M-Pesa names (known_names.summary): the counts, and what the approver ticks
    when a number is new or known under another name."""
    from . import known_names

    return known_names.summary(name_targets(rows), acknowledge=True)


def before_submit(doc) -> None:
    """The approval: a named M-Pesa Manager or Accounts Manager (never the shared Administrator
    login while M-Pesa Suite Settings refuses it), who ticked that they checked the numbers M-Pesa
    knows under another name and the new ones, when there are any (asked by the form at Submit,
    required here)."""
    require_named_user()
    require(APPROVER_ROLES)
    names = names_summary(doc.get(TABLE) or [])
    if names and names.get("acknowledge") and not cint(doc.get("names_checked")):
        frappe.throw(f"{doc.name} pays numbers whose M-Pesa names need checking ({names['header']}). Tick "
                     f"\"{names['acknowledge']}\" and submit again.", title="Check the M-Pesa names")
    doc.names_summary = names["header"] if names else None
    doc.approved_by = frappe.session.user
    doc.approved_on = now_datetime()
    doc.status = TO_PAY
    doc.progress = f"{TO_PAY}: {len(doc.get(TABLE) or [])} rows, KES {money(doc.get('total_amount'))}"
    doc.left_amount = doc.get("total_amount")
    doc.paid_amount = doc.failed_amount = 0


def keep_results(doc) -> None:
    """before_update_after_submit: a save after submit (a form opened before a result, the API)
    writes back what the database holds for the rows' results and the sheet's totals: they are the
    server's, and the double-payment checks read them."""
    current = {r.name: r for r in read_rows(doc.name, for_update=True)}
    for row in doc.get(TABLE) or []:
        stored = current.get(row.name)
        if stored:
            for field in RESULT_FIELDS:
                setattr(row, field, stored.get(field))
    header = frappe.db.get_value(SHEET, doc.name, list(HEADER_FIELDS), as_dict=True) or {}
    for field in HEADER_FIELDS:
        setattr(doc, field, header.get(field))


def before_cancel(doc) -> None:
    """A sheet is cancelled only while none of its rows is queued in a batch, on its way or paid
    (a paid KES 10 test too): the money left, and an amended copy would pay the rows again. Under
    the sheet's lock, the payouts read as they are now. Its failed payouts do not hold the cancel."""
    with owed.doc_lock(SHEET, doc.name):
        rows = sync_rows(doc.name, for_update=True)
        busy = [r for r in rows if r.status in (QUEUED, IN_FLIGHT, PAID) or flt(r.get("paid_amount")) > owed.MIN]
        if busy:
            listed = ", ".join(f"#{r.idx} ({r.status})" for r in busy[:5])
            frappe.throw(f"{doc.name} cannot be cancelled: rows {listed}{' and more' if len(busy) > 5 else ''} are "
                         "queued, on their way or paid by M-Pesa. Settle the rows that M-Pesa will not pay outside "
                         "M-Pesa instead; delete a batch that was never sent first.")
    from .employee_documents import PAYOUT_LINKS

    current = tuple(doc.get("ignore_linked_doctypes") or ())
    doc.ignore_linked_doctypes = current + tuple(d for d in PAYOUT_LINKS if d not in current)


def on_cancel(doc) -> None:
    """Its rows are not paid any more (none was: before_cancel). The Journal Entries and Payment
    Entries made are left alone."""
    for row in doc.get(TABLE) or []:
        if row.get("status") not in (PAID, IN_FLIGHT):
            frappe.db.set_value(ROW, row.name, "status", CANCELLED, update_modified=False)
    doc.db_set({"status": S_CANCELLED, "progress": S_CANCELLED}, update_modified=False)


# ---------------------------------------------------------------- the rows' payouts and status
def read_rows(sheet: str, for_update: bool = False) -> list:
    """The sheet's rows as stored (a locking read when making payouts or booking)."""
    return frappe.db.sql(
        f"""SELECT * FROM `tab{ROW}` WHERE parent = %(sheet)s AND parenttype = %(dt)s AND parentfield = %(field)s
        ORDER BY idx {_locking(for_update)}""",
        {"sheet": sheet, "dt": SHEET, "field": TABLE},
        as_dict=True,
    )


def payouts_of(sheet: str, for_update: bool = False) -> list:
    """Every B2C row that pays a row of the sheet, with its payout and what it says about the row."""
    found = []
    for r in frappe.db.sql(
        f"""
        SELECT ref.name AS ref, ref.parent, ref.idx AS ref_idx, ref.reference_row, ref.partyb, ref.allocated_amount,
               ref.payment_status, ref.creation, d.docstatus AS parent_docstatus, d.creation AS prepared_on,
               d.paid_from, req.name AS request, req.docstatus AS request_docstatus, req.status AS request_status,
               req.transaction_id, req.receiver_public_name, req.transaction_completed_datetime, req.error_message,
               req.result_desc, req.result_code, req.conversation_id, req.creation AS sent_on, req.mpesa_settings,
               req.send_started_on
        FROM `tab{B2C_ROW}` ref
        JOIN `tab{DISB}` d ON d.name = ref.parent
        LEFT JOIN `tab{REQUEST}` req ON req.name = ref.b2c_disbursement_request
        WHERE ref.reference_doctype = %(dt)s AND ref.reference_name = %(sheet)s AND ref.parenttype = %(parent)s
        ORDER BY ref.creation {_locking(for_update)}
        """,
        {"dt": SHEET, "sheet": sheet, "parent": DISB},
        as_dict=True,
    ):
        state = cp.b2c_state(r.parent_docstatus, r.payment_status, r.request, r.request_docstatus, r.request_status)
        if state is None and r.request and partyless.fate_unknown(frappe._dict(
                status=r.request_status, send_started_on=r.send_started_on)):
            state = IN_FLIGHT  # cancelled after its send began, never answered: the money may have left
        row_name, is_test = split_paying_row(r.reference_row)
        found.append(frappe._dict(
            ref=r.ref, parent=r.parent, row=row_name, is_test=is_test, state=state, amount=flt(r.allocated_amount, 2),
            request=r.request, request_status=r.request_status, request_docstatus=cint(r.request_docstatus),
            parent_docstatus=cint(r.parent_docstatus), creation=r.creation, prepared_on=r.prepared_on,
            sent_on=r.sent_on if r.request else None, conversation_id=r.conversation_id, paid_from=r.paid_from,
            mpesa_settings=r.mpesa_settings, phone=r.partyb,
            receipt=cstr(r.transaction_id).strip() or None if state == PAID else None,
            receiver_name=cstr(r.receiver_public_name).strip() or None if state == PAID else None,
            paid_on=r.transaction_completed_datetime if state == PAID else None,
            result_code=cstr(r.result_code).strip(),
            error=(r.result_desc or r.error_message or "Not sent") if state == FAILED else None,
        ))
    return found


def failure(result_code, text) -> tuple:
    """(kind, what to do) of a failed B2C payout, from its result code or description; (None, None)
    when it is none of the common ones."""
    code, text = cstr(result_code).strip().upper(), cstr(text).lower()
    for codes, words, kind, hint in FAILURES:
        if code in codes or any(w in text for w in words):
            return kind, hint
    return None, None


def _line(row) -> frappe._dict:
    """The row as claim_payees.name_check reads a phone payee line."""
    return frappe._dict(payee_type=cp.PHONE, phone=row.get("phone"), typed_name=row.get("typed_name"),
                        payee_name=row.get("payee_name"), shortcode=None)


def _clock(value) -> str:
    when = get_datetime(value) if value else None
    return when.strftime("%H:%M") if when else ""


def _progress(row_status, current, verification, test=None) -> str | None:
    """What the row shows of where its payout is (``test``: its test payout, whose amount it names)."""
    tried = f"KES {money(test.amount if test else TEST_AMOUNT)} test"
    if not current:
        if verification == VERIFIED and row_status == DRAFT:
            return f"{tried} paid under the name given: the rest is paid by the next Pay by M-Pesa"
        if verification == DIFFERS:
            return f"Held: its {tried} was paid under another name; an M-Pesa Manager checks it"
        return None
    what = f"KES {money(current.amount)} test" if current.is_test else "Payout"
    if current.state == QUEUED:
        return f"{what} queued in {current.parent}, not sent yet"
    if current.state == IN_FLIGHT:
        if not current.request:
            return f"{what} of {current.parent} being sent"
        if current.request_status == "Status Unknown" or not cstr(current.conversation_id).strip():
            return f"Checking with M-Pesa: {current.request} got no answer to its send (it is asked, never re-sent)"
        return f"{what} sent {_clock(current.sent_on)}, waiting for M-Pesa"
    if current.state == FAILED:
        return f"{what} failed: {current.error}"
    if current.state == PAID and current.is_test and row_status == ON_HOLD:
        return f"Held: its {tried} was paid under another name; an M-Pesa Manager checks it"
    return None


def row_values(row, payouts, sheet_docstatus) -> dict:
    """A row's status and results from its payouts (pure). ``payouts``: the row's own: its main
    payouts and its KES 10 tests, each group decided by the payout furthest on its way. A Cancelled
    row stays Cancelled, with its note, while no main payout of it is live or paid (settle_row)."""
    mains = [p for p in payouts if not p.is_test]
    best = cp.deciding_payout(mains)
    test = cp.deciding_payout([p for p in payouts if p.is_test])
    verification = cstr(row.get("verification")) or None
    values = {"test_request": test.request if test else None, "test_receipt": test.receipt if test else None}
    test_ok, test_note = None, None
    if test and test.state == PAID:
        test_ok, test_note = cp.name_check(_line(row), test.receiver_name)
        if verification != RELEASED:
            verification = VERIFIED if test_ok else DIFFERS
    elif test and verification not in (VERIFIED, RELEASED):
        verification = TESTING
    # where the row is now: a test on its way, else its main payout, else its test
    current = test if test and test.state in LIVE else (best or test)
    settled = row.get("status") == CANCELLED and not any(
        p.state in LIVE or (p.state == PAID and not p.is_test) for p in payouts)
    if settled:
        status = CANCELLED
    elif current is None:
        status = DRAFT
    elif current.state == PAID and current.is_test:
        status = ON_HOLD if verification == DIFFERS else DRAFT  # the rest is paid next, or held
    else:
        status = current.state
    if cint(sheet_docstatus) == 2 and status not in (PAID, IN_FLIGHT):
        status = CANCELLED
    values.update(
        status=status, verification=verification,
        payout=current.parent if current else None, payout_request=current.request if current else None,
        sent_on=(current.sent_on or current.prepared_on) if current else None,
        attempts=sum(1 for p in payouts if p.request),
        paid_amount=flt(sum(p.amount for p in payouts if p.state == PAID), 2),
        receipt=best.receipt if best and best.state == PAID else None,
        paid_on=best.paid_on if best and best.state == PAID else None,
        receiver_name=best.receiver_name if best and best.state == PAID else None,
        progress=None if settled else _progress(status, None if status == DRAFT else current, verification, test),
    )
    if settled:
        values["error_message"] = row.get("error_message")
    elif current and current.state == FAILED:
        _kind, hint = failure(current.result_code, current.error)
        values["error_message"] = f"{current.error}{f' {hint}' if hint else ''}"[:1000]
    else:
        values["error_message"] = None
    if best and best.state == PAID:
        verdict, note = cp.name_check(_line(row), best.receiver_name)
        values.update(name_verdict=cp.VERDICT[verdict], name_note=note)
    elif test and test.state == PAID:
        values.update(name_verdict=cp.VERDICT[test_ok], name_note=f"KES {money(test.amount)} test: {test_note}")
    else:
        values.update(name_verdict=None, name_note=None)
    twice = sorted({p.request for p in mains if p.state == PAID})
    if len(twice) > 1:
        values["error_message"] = (f"Paid {len(twice)} times: {', '.join(twice)}. Check with the payee and recover the "
                                   "extra payment.")
    return values


def sync_rows(sheet: str, rows=None, for_update: bool = False, with_payouts: bool = False):
    """Each row's status and results from its payouts; only what changed is written. A row newly
    paid (or its receiver's name come later) has its name checked: a mismatch is commented on the
    sheet and alerted; a KES 10 test paid under another name holds the row for an M-Pesa Manager.
    Then the sheet's status and totals. Returns the rows as they are now (and the payouts)."""
    rows = rows if rows is not None else read_rows(sheet, for_update)
    payouts = payouts_of(sheet, for_update) if rows else []
    docstatus = frappe.db.get_value(SHEET, sheet, "docstatus")
    for row in rows:
        mine = [p for p in payouts if p.row == row.name]
        values = row_values(row, mine, docstatus)
        changed = {k: v for k, v in values.items() if _norm(k, row.get(k)) != _norm(k, v)}
        if not changed:
            continue
        newly_paid = values["status"] == PAID and ("status" in changed or "receiver_name" in changed)
        newly_held = values.get("verification") == DIFFERS and "verification" in changed
        frappe.db.set_value(ROW, row.name, changed, update_modified=False)
        row.update(changed)
        if newly_paid and values.get("name_verdict") == cp.NO_MATCH:
            _alarm(sheet, row, f"{row.name_note} Check with the person before this phone is paid again.")
        if newly_held:
            _alarm(sheet, row, f"{row.name_note} The rest (KES {money(flt(row.amount) - flt(row.paid_amount))}) is "
                               "held: an M-Pesa Manager checks it with the person, then Release Held Row, or Settle "
                               "Outside M-Pesa.", roles=ADMIN_ROLES)
        if cstr(changed.get("error_message")).startswith("Paid "):
            _alarm(sheet, row, row.error_message)
    if rows:
        update_sheet(sheet, rows, docstatus)
    return (rows, payouts) if with_payouts else rows


def _alarm(sheet: str, row, message: str, roles=None) -> None:
    """A Comment on the sheet and an alert to the Notify Role (``roles``: to them instead). The
    phone shows as utils.privacy says."""
    text = f"Row #{row.idx} ({row.payee_name}, {privacy.phone(row.phone)}): {message}"
    try:
        frappe.get_doc(SHEET, sheet).add_comment("Comment", text)
    except Exception:
        frappe.log_error(title="M-Pesa bulk payout comment failed", message=f"{sheet}\n{frappe.get_traceback()}")
    pa.alert(f"{sheet}: {text}"[:500], frappe._dict(doctype=SHEET, name=sheet), roles=roles)


def sheet_values(docstatus, rows) -> dict:
    """The sheet's status, progress and totals from its rows (pure)."""
    counts = Counter(r.get("status") or DRAFT for r in rows)
    paid_amount = flt(sum(flt(r.get("paid_amount")) for r in rows), 2)
    failed_amount = flt(sum(flt(r.amount) for r in rows if r.get("status") == FAILED), 2)
    left = flt(sum(max(0.0, flt(r.amount) - flt(r.get("paid_amount"))) for r in rows
                   if r.get("status") not in (PAID, CANCELLED)), 2)
    if cint(docstatus) == 0:
        status = S_DRAFT
    elif cint(docstatus) == 2:
        status = S_CANCELLED
    elif counts[QUEUED] or counts[IN_FLIGHT]:
        status = PAYING
    elif counts[PAID] and counts[PAID] + counts[CANCELLED] == len(rows):
        status = S_PAID
    elif paid_amount > owed.MIN:
        status = PARTLY
    elif counts[CANCELLED] == len(rows):
        status = S_PAID  # every row settled outside M-Pesa: nothing left to pay
    else:
        status = TO_PAY
    parts = [f"{counts[PAID]} paid"] if counts[PAID] else []
    for label, key in (("failed", FAILED), ("waiting", None), ("held", ON_HOLD), ("to pay", DRAFT),
                       ("settled outside M-Pesa", CANCELLED)):
        n = counts[QUEUED] + counts[IN_FLIGHT] if key is None else counts[key]
        if n and not (key == CANCELLED and cint(docstatus) == 2):
            parts.append(f"{n} {label}")
    progress = f"{status}: {', '.join(parts)}" if parts and status not in (S_DRAFT, S_CANCELLED) else status
    return {"status": status, "progress": progress, "paid_amount": paid_amount, "failed_amount": failed_amount,
            "left_amount": left}


def update_sheet(sheet: str, rows=None, docstatus=None) -> dict:
    """The sheet's status and totals brought up to its rows, written only when they changed."""
    rows = rows if rows is not None else read_rows(sheet)
    if docstatus is None:
        docstatus = frappe.db.get_value(SHEET, sheet, "docstatus")
    values = sheet_values(docstatus, rows)
    current = frappe.db.get_value(SHEET, sheet, list(HEADER_FIELDS), as_dict=True) or {}
    changed = {k: v for k, v in values.items() if _norm(k, current.get(k)) != _norm(k, v)}
    if changed:
        frappe.db.set_value(SHEET, sheet, changed, update_modified=False)
    return values


# ---------------------------------------------------------------- making the payouts (drafts)
@frappe.whitelist(methods=["POST"])
def pay(sheet: str, b2c_settings: str | None = None) -> dict:
    """Pay by M-Pesa: one draft B2C Payment Disbursement for the sheet's Draft rows (a new number's
    KES 10 test when the sheet says so; a verified row's rest). An M-Pesa Payer sends it with Send
    via M-Pesa. For PREPARE_ROLES who can read the sheet."""
    require(PREPARE_ROLES)
    frappe.has_permission(SHEET, "read", doc=sheet, throw=True)
    return make_payout(sheet, (DRAFT,), b2c_settings)


@frappe.whitelist(methods=["POST"])
def retry_failed(sheet: str, b2c_settings: str | None = None) -> dict:
    """Retry Failed: one new draft batch for the rows whose last payout M-Pesa failed, and only
    those (a row whose fate is unknown is never sent again: it is asked about). For PREPARE_ROLES
    who can read the sheet."""
    require(PREPARE_ROLES)
    frappe.has_permission(SHEET, "read", doc=sheet, throw=True)
    return make_payout(sheet, (FAILED,), b2c_settings)


def _new_numbers(rows) -> set:
    """The phones of ``rows`` M-Pesa never paid (no M-Pesa Known Name record at all)."""
    from . import known_names

    found = known_names.lookup(f"B2C:{r.phone}" for r in rows)
    return {r.phone for r in rows if f"B2C:{r.phone}" not in found}


def _test_paid(row, payouts) -> float:
    return flt(sum(p.amount for p in payouts if p.row == row.name and p.is_test and p.state == PAID), 2)


def min_payout() -> float:
    """The least M-Pesa Suite Settings lets one B2C payout be (Minimum B2C Payout, never below KES 10)."""
    from ..services import b2c_request_service

    return flt(b2c_request_service.min_payout_amount())


def trial_amount() -> float:
    """What a new number's test pays: KES 10, or the Minimum B2C Payout when that is more (a smaller
    payout would be refused at the send, and the whole batch with it)."""
    return float(max(TEST_AMOUNT, min_payout()))


def plan(doc, rows, payouts) -> list:
    """[(row, paying row, amount, is test)] of a batch for ``rows``: a new number's test first
    (``trial_amount``) when the sheet pays new numbers so, and only when what is left after it can be
    paid too (no less than the Minimum B2C Payout: KES 20 or more for a KES 10 test); a smaller row
    is its own test, paid whole. A row whose test was paid gets the rest, any other row its whole
    amount."""
    new = _new_numbers(rows) if cint(doc.get("verify_new_numbers")) else set()
    first, least = (trial_amount(), min_payout()) if new else (0.0, 0.0)
    out = []
    for row in rows:
        tested = _test_paid(row, payouts)
        if tested > owed.MIN:
            out.append((row, row.name, flt(flt(row.amount) - tested, 2), False))
        elif (row.phone in new and flt(row.amount) - first >= least - owed.MIN
              and row.get("verification") != RELEASED):
            out.append((row, f"{row.name}{TEST_SUFFIX}", first, True))
        else:
            out.append((row, row.name, flt(row.amount, 2), False))
    return out


def make_payout(sheet: str, states, b2c_settings=None, only=None) -> dict:
    """A draft party-less B2C Payment Disbursement for the sheet's rows in ``states`` (``only``:
    these rows), under the sheet's lock (a second press waits, then finds the rows Queued). Made with
    ignore_permissions: the caller checked the preparer's roles and read permission, and every row
    is checked again when the batch is saved and sent. Nothing is sent here."""
    with owed.doc_lock(SHEET, sheet):
        doc = frappe.get_doc(SHEET, sheet)
        if cint(doc.docstatus) != 1:
            frappe.throw(f"{sheet} is not approved: an M-Pesa Manager or Accounts Manager submits it first."
                         if cint(doc.docstatus) == 0 else f"{sheet} is cancelled.")
        rows, payouts = sync_rows(sheet, for_update=True, with_payouts=True)
        picked = [r for r in rows if r.status in states and (only is None or r.name in only)]
        if not picked:
            frappe.throw("No row has failed." if tuple(states) == (FAILED,) else
                         "Nothing to pay: every row is paid, on its way, held, settled outside M-Pesa or in a batch "
                         "waiting to be sent.")
        problem = self_pay_problem(frappe.session.user, picked, "prepare")
        if problem:
            frappe.throw(problem, frappe.PermissionError)
        planned = plan(doc, picked, payouts)
        settings_name = cp.pick_settings(b2c_settings or doc.get("mpesa_settings"), apis.B2C, doc.company)
        from ..money_in import common as money_in_common
        from .schedule import check_b2c_settings

        check_b2c_settings(settings_name, max(amount for _r, _p, amount, _t in planned), doc.company)
        mop = money_in_common.mode_of_payment_for(settings_name)
        disbursement = frappe.new_doc(DISB)
        disbursement.update({
            "naming_series": "MPESA-B2C-.YYYY.-",
            "company": doc.company,
            "posting_date": nowdate(),
            "payment_type": "Mpesa Disbursement",
            "party_type": None,
            "transaction_to_pay_against": SHEET,
            "mode_of_payment": mop,
            "paid_from": cp._b2c_paid_from(settings_name, mop, doc.company),
            "paid_from_account_currency": "KES",
            "paid_to": None,
            "paid_to_account_currency": "KES",
            "paid_amount": flt(sum(amount for _r, _p, amount, _t in planned), 2),
            "source_exchange_rate": 1,
            "target_exchange_rate": 1,
        })
        for row, paying, amount, _test in planned:
            disbursement.append("references", {
                "reference_doctype": SHEET, "reference_name": sheet, "reference_row": paying,
                "partyb": row.phone, "allocated_amount": amount, "currency": "KES", "exchange_rate": 1,
            })
        disbursement.flags.ignore_permissions = True
        disbursement.insert(ignore_permissions=True)
        sync_rows(sheet, rows)
    tests = sum(1 for *_x, test in planned if test)
    total = flt(sum(amount for _r, _p, amount, _t in planned), 2)
    from .schedule import maker_checker_on

    who = "An M-Pesa Payer who did not prepare it" if maker_checker_on() else "An M-Pesa Payer"
    first = (f"; {tests} new numbers get KES {money(next(a for *_x, a, t in planned if t))} first"
             if tests else "")
    doc.add_comment("Comment", f"{frappe.session.user} prepared {disbursement.name} for {len(planned)} rows (KES "
                               f"{money(total)}{first}). {who} sends it with Send via M-Pesa.")
    return frappe._dict(b2c=disbursement.name, rows=len(planned), total=total, tests=tests)


# ---------------------------------------------------------------- managers' actions on a row
def _manager_row(sheet: str, row: str, reason: str | None, ask: str):
    """A named M-Pesa Manager who can read the sheet, the row of it, and a reason given."""
    require_named_user()
    require(ADMIN_ROLES)
    frappe.has_permission(SHEET, "read", doc=sheet, throw=True)
    reason = cstr(reason).strip()
    if not reason:
        frappe.throw(ask)
    if cint(frappe.db.get_value(SHEET, sheet, "docstatus")) != 1:
        frappe.throw(f"{sheet} is not submitted.")
    return reason


def _row_of(rows, row: str, sheet: str):
    found = next((r for r in rows if r.name == row), None)
    if not found:
        frappe.throw(f"{row} is not a row of {sheet}.")
    return found


def _check_not_own(row, action: str) -> None:
    problem = self_pay_problem(frappe.session.user, [row], action)
    if problem:
        frappe.throw(problem, frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
def settle_row(sheet: str, row: str, reason: str | None = None) -> dict:
    """Settle Outside M-Pesa: a Draft, Failed or On Hold row M-Pesa will not pay (paid in cash, or
    by bank) is Cancelled with who settled it and how. It is left out of the journal: its cost is
    booked where it was paid (a KES 10 test M-Pesa paid stays booked). For a named M-Pesa Manager
    who can read the sheet; under its lock, refused while a payout of the row is queued, on its way
    or paid."""
    reason = _manager_row(sheet, row, reason, "Say how the row was paid outside M-Pesa (the bank entry, say).")
    with owed.doc_lock(SHEET, sheet):
        found = _row_of(sync_rows(sheet, for_update=True), row, sheet)
        _check_not_own(found, "settle")
        if found.status not in (DRAFT, FAILED, ON_HOLD):
            frappe.throw(f"Row #{found.idx} ({found.payee_name}) is {found.status}: only a Draft, Failed or On Hold "
                         "row is settled outside M-Pesa.")
        note = f"Settled outside M-Pesa by {frappe.session.user}: {reason}"
        frappe.db.set_value(ROW, found.name, {"status": CANCELLED, "error_message": note, "progress": None},
                            update_modified=False)
        found.update(status=CANCELLED, error_message=note)
        sync_rows(sheet)
    frappe.get_doc(SHEET, sheet).add_comment(
        "Comment", f"Row #{found.idx} ({found.payee_name}, {privacy.phone(found.phone)}, KES {money(found.amount)}) "
                   f"settled outside M-Pesa by {frappe.session.user}: {reason}. M-Pesa will not pay it.")
    return {"row": found.name, "status": CANCELLED}


@frappe.whitelist(methods=["POST"])
def release_row(sheet: str, row: str, reason: str | None = None) -> dict:
    """Release Held Row: a row whose KES 10 test M-Pesa paid under another name, which an M-Pesa
    Manager checked with the person (the name M-Pesa holds for them is another), is released: its
    rest is paid by the next Pay by M-Pesa. For a named M-Pesa Manager who can read the sheet."""
    reason = _manager_row(sheet, row, reason, "Say how you checked the number with the person.")
    with owed.doc_lock(SHEET, sheet):
        found = _row_of(sync_rows(sheet, for_update=True), row, sheet)
        _check_not_own(found, "release")
        if found.status != ON_HOLD:
            frappe.throw(f"Row #{found.idx} ({found.payee_name}) is {found.status}: only a row On Hold is released.")
        frappe.db.set_value(ROW, found.name, {"verification": RELEASED}, update_modified=False)
        found.verification = RELEASED
        sync_rows(sheet)
    frappe.get_doc(SHEET, sheet).add_comment(
        "Comment", f"Row #{found.idx} ({found.payee_name}, {privacy.phone(found.phone)}): its KES "
                   f"{money(found.paid_amount or TEST_AMOUNT)} test was paid under another name; released by "
                   f"{frappe.session.user}: {reason}.")
    return {"row": found.name, "status": DRAFT}


@frappe.whitelist(methods=["POST"])
def correct_phone(sheet: str, row: str, phone: str | None = None, reason: str | None = None,
                  b2c_settings: str | None = None) -> dict:
    """Correct Phone and Retry: a Failed row's phone corrected by a named M-Pesa Manager, with why;
    the change is commented on the sheet, the name M-Pesa holds for the new number and self-pay are
    checked again, and a draft batch is made for the row (an M-Pesa Payer sends it)."""
    reason = _manager_row(sheet, row, reason, "Say why the phone is corrected (who gave the right number).")
    msisdn = payable_msisdn(phone)
    if not msisdn:
        frappe.throw(f"{cstr(phone) or 'No phone'} is not a Kenyan mobile number M-Pesa can pay ({FORMATS}).")
    if network_of(msisdn) not in ("Safaricom", None):
        frappe.throw(f"{to_local(msisdn)} does not start like a Safaricom number: B2C pays M-Pesa lines only.")
    with owed.doc_lock(SHEET, sheet):
        found = _row_of(sync_rows(sheet, for_update=True), row, sheet)
        if found.status != FAILED:
            frappe.throw(f"Row #{found.idx} ({found.payee_name}) is {found.status}: only a Failed row's phone is "
                         "corrected.")
        old = found.phone
        if msisdn == old:
            frappe.throw(f"Row #{found.idx} already pays {privacy.phone(msisdn)}.")
        given = cstr(found.get("typed_name")).strip() or found.payee_name
        values = {"phone": msisdn, "payee_name": given, "typed_name": None, "mpesa_name": None, "mpesa_name_from": None,
                  "known_flag": NEW}
        known = cp.mpesa_names([msisdn]).get(msisdn)
        if known:
            differs = not pa.names_match(given, known.name)
            values.update(payee_name=known.name, mpesa_name=known.name, mpesa_name_from=cp.name_from(known),
                          typed_name=given if differs else None, known_flag=NAME_DIFFERS if differs else KNOWN)
        if found.get("verification") == TESTING:
            values["verification"] = None  # a new number: tested afresh when the sheet says so
        _check_not_own(frappe._dict(found, **values), "pay")
        frappe.db.set_value(ROW, found.name, values, update_modified=False)
    frappe.get_doc(SHEET, sheet).add_comment(
        "Comment", f"Row #{found.idx} ({given}): phone corrected from {privacy.phone(old)} to {privacy.phone(msisdn)} "
                   f"by {frappe.session.user}: {reason}." + (f" M-Pesa knows the new number as {known.name}." if known
                                                             else " M-Pesa never paid the new number."))
    made = make_payout(sheet, (FAILED,), b2c_settings, only={found.name})
    return dict(made, row=found.name, phone=msisdn)


@frappe.whitelist(methods=["POST"])
def mark_not_paid(sheet: str, row: str, reason: str | None = None) -> dict:
    """Mark as Not Paid: a row stuck Checking with M-Pesa (its send got no answer, and a
    Transaction Status query M-Pesa answered settled nothing), which a named M-Pesa Manager checked
    on the M-Pesa statement, becomes Failed and may be retried with the sheet's Retry Failed (a new
    payout: the marked one keeps its IDs, so a Result that still comes for it settles it as M-Pesa
    says). Refused while M-Pesa holds the payout (Initiated: it acknowledged it, and its Result or a
    query will say), and for the person the row pays. For a named M-Pesa Manager who can read the
    sheet."""
    reason = _manager_row(sheet, row, reason, "Say what the M-Pesa statement shows for this payment.")
    with owed.doc_lock(SHEET, sheet):
        rows, payouts = sync_rows(sheet, for_update=True, with_payouts=True)
        found = _row_of(rows, row, sheet)
        _check_not_own(found, "settle")  # nobody clears the way to paying themselves again
        mine = [p for p in payouts if p.row == found.name and p.state == IN_FLIGHT and p.request]
        if found.status != IN_FLIGHT or not mine:
            frappe.throw(f"Row #{found.idx} ({found.payee_name}) is {found.status}: only a row sent and waiting for "
                         "M-Pesa is marked as not paid.")
        payout = mine[-1]
        request = frappe.db.get_value(REQUEST, payout.request, ["status", "result_code"], as_dict=True,
                                      for_update=True)
        if request.status not in ("Initiated", "Status Unknown", "Not Initiated") or cstr(request.result_code).strip():
            frappe.throw(f"{payout.request} is {request.status}: M-Pesa answered it.")
        if request.status == "Initiated":
            frappe.throw(f"M-Pesa acknowledged {payout.request} (Initiated): it has the payout, and its Result, or a "
                         "Transaction Status query (Check with M-Pesa Now), says whether it paid. Only a payout whose "
                         "send got no answer is marked as not paid.")
        answered = frappe.get_all(STATUS_QUERY, filters={"b2c_request": payout.request, "status": "Result Received"},
                                  fields=["name", "outcome"], order_by="creation desc")
        if not [q for q in answered if q.get("outcome") != "Paid"]:
            frappe.throw(f"M-Pesa has not answered a Transaction Status query about {payout.request} yet: press Check "
                         "with M-Pesa Now and wait for its answer. Mark it only when the answer settles nothing and "
                         "the M-Pesa statement shows no such payment.")
        note = f"Marked as not paid by {frappe.session.user}, who checked the M-Pesa statement: {reason}"
        frappe.db.set_value(REQUEST, payout.request, {"status": "Failed", "error_message": note[:500]},
                            update_modified=False)
        frappe.db.set_value(B2C_ROW, payout.ref, "payment_status", "Failed", update_modified=False)
        sync_rows(sheet)
    from ..services.b2c_batch_status import sync_status_later

    sync_status_later(payout.parent)
    book_later(sheet)  # it no longer holds the journal back
    frappe.get_doc(SHEET, sheet).add_comment(
        "Comment", f"Row #{found.idx} ({found.payee_name}): {payout.request} {note}. A Result that still comes settles "
                   "it as M-Pesa says.")
    try:
        frappe.get_doc(REQUEST, payout.request).add_comment("Comment", note)
    except Exception:
        frappe.log_error(title="M-Pesa bulk payout comment failed", message=frappe.get_traceback())
    return {"row": found.name, "status": FAILED, "request": payout.request}


# ---------------------------------------------------------------- Check with M-Pesa Now
@frappe.whitelist(methods=["POST"])
def check_now(sheet: str) -> dict:
    """Check with M-Pesa Now: a Transaction Status query for each of the sheet's payouts still
    waiting (money_out.b2c_status.start_query: one open query per payout within its cooldown); the
    answer settles the row. For an M-Pesa Manager or Payer who can read the sheet."""
    require(PAYOUT_ROLES)
    frappe.has_permission(SHEET, "read", doc=sheet, throw=True)
    from .b2c_status import start_query

    payouts = [p for p in payouts_of(sheet) if p.state == IN_FLIGHT and p.request]
    asked, skipped = [], []
    for p in payouts:
        try:
            start_query(p.request)
            asked.append(p.request)
        except frappe.ValidationError as e:
            skipped.append(f"{p.request}: {cstr(e)}")
    if not payouts:
        frappe.throw("No payout of this sheet is waiting for M-Pesa.")
    return {"asked": asked, "skipped": skipped}


# ---------------------------------------------------------------- the journal
def holding_rows(rows, now=None, hours=None) -> list:
    """The rows that hold the sheet's journal back: a payout queued or on its way, or a verified
    row whose rest is not sent yet, for less than ``hours`` since its send (0: however long)."""
    now = now or now_datetime()
    hours = settings().book_after_hours if hours is None else cint(hours)
    out = []
    for row in rows:
        waiting = row.get("status") in LIVE or (
            row.get("status") == DRAFT and row.get("verification") in (VERIFIED, RELEASED))
        if not waiting:
            continue
        since = get_datetime(row.get("sent_on")) if row.get("sent_on") else None
        if hours and since and since <= add_to_date(now, hours=-hours):
            continue  # waited long enough: the paid rows are booked without it
        out.append(row)
    return out


def _credit_account(payout) -> tuple:
    """(GL, Bank Account) the payout left: the shortcode's Utility account's own when it has one,
    else the batch's Paid From (claim_payees._b2c_paid_from: the Utility Bank Account's GL)."""
    from ..wallet import accounts as wallet_accounts

    gl = wallet_accounts.gl(payout.mpesa_settings, wallet_accounts.UTILITY) if payout.mpesa_settings else None
    gl = gl or payout.paid_from
    bank_account = wallet_accounts.bank_account(payout.mpesa_settings, wallet_accounts.UTILITY) if (
        payout.mpesa_settings) else None
    if bank_account and frappe.db.get_value("Bank Account", bank_account, "account") != gl:
        bank_account = None
    return gl, bank_account


def dimensions() -> list:
    """The active accounting dimensions' fields (ERPNext), project apart."""
    from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions

    return [f for f in dict.fromkeys(get_accounting_dimensions()) if f and f != "project"]


def build_journal(sheet, items, posting_date=None):
    """The bulk Journal Entry (a Bank Entry, the sheet's name as Reference No): debit lines grouped
    by expense account, cost center, project and accounting dimensions; a credit line per Utility
    GL. ``items``: [(row, payout)]."""
    from .common import payout_cost_center

    dims = dimensions()
    groups, credits = {}, {}
    for row, payout in items:
        if not row.get("expense_account") or not row.get("cost_center"):
            frappe.throw(f"Row #{row.idx} of {sheet.name} has no Expense Account or Cost Center.")
        key = (row.expense_account, row.cost_center, cstr(row.get("project")) or None,
               tuple(cstr(row.get(d)) or None for d in dims))
        line = groups.setdefault(key, {"amount": 0.0, "rows": set()})
        line["amount"] = flt(line["amount"] + payout.amount, 2)
        line["rows"].add(row.idx)
        gl, bank_account = _credit_account(payout)
        if not gl:
            frappe.throw(f"Map a Bank Account on Mpesa Settings {payout.mpesa_settings} (Accounting Mapping).")
        credit = credits.setdefault(gl, {"amount": 0.0, "bank_account": bank_account,
                                         "settings": payout.mpesa_settings})
        credit["amount"] = flt(credit["amount"] + payout.amount, 2)
    total = flt(sum(c["amount"] for c in credits.values()), 2)
    date = getdate(posting_date) if posting_date else max(
        (getdate(p.paid_on) for _r, p in items if p.paid_on), default=getdate(nowdate()))
    remark = (f"M-Pesa Bulk Payout {sheet.name} ({sheet.title}): {len(items)} payouts paid by M-Pesa, KES "
              f"{money(total)}")
    je = frappe.new_doc(JE)
    je.voucher_type = "Bank Entry"
    je.company = sheet.company
    je.posting_date = je.cheque_date = date
    je.cheque_no = sheet.name
    je.user_remark = remark
    if je.meta.has_field("custom_remark"):  # v16 hides user_remark and builds remark itself unless told
        je.custom_remark = 1
        je.remark = remark
    for key in sorted(groups, key=lambda k: tuple(cstr(x) for x in (*k[:3], *k[3]))):
        account, cost_center, project, values = key
        rows = sorted(groups[key]["rows"])
        row = {"account": account, "debit_in_account_currency": groups[key]["amount"], "cost_center": cost_center,
               "user_remark": f"{sheet.name} rows {', '.join(f'#{i}' for i in rows)}"[:140]}
        if project:
            row["project"] = project
        row.update({d: v for d, v in zip(dims, values) if v})
        je.append("accounts", row)
    # a dimension mandatory for Balance Sheet accounts: the company's default on the credit line (checked
    # at the save: check_credit_dimensions)
    defaults = {r.fieldname: r.default for r in dimension_rules(sheet.company) if r.mandatory_for_bs and r.default}
    for gl in sorted(credits):
        credit = credits[gl]
        je.append("accounts", {"account": gl, "credit_in_account_currency": credit["amount"],
                               "bank_account": credit["bank_account"],
                               "cost_center": payout_cost_center(credit["settings"], sheet.company),
                               "user_remark": remark[:140], **defaults})
    return je


def unbooked(rows, payouts) -> list:
    """[(row, payout)]: the sheet's Paid payouts nothing books yet. Booked: by the journal the row's
    bookings name, or by the copy that amended it once it was cancelled (Cancel and Amend:
    ``live_journal``); or by another live entry that carries the payout's receipt (a voucher made
    from its bank line: ``receipt_entries``, which _handle counts as its booking too). Only a payout
    whose journal was cancelled with no amended copy, and that no other entry carries, is booked
    again."""
    by_name = {r.name: r for r in rows}
    out = []
    for payout in payouts:
        row = by_name.get(payout.row)
        if payout.state != PAID or not payout.request or not payout.receipt or not row:
            continue
        if _journal_status(bookings_of(row).get(payout.request)) is None:
            out.append((row, payout))
    if not out:
        return out
    elsewhere = receipt_entries(p.receipt for _r, p in out)
    return [(r, p) for r, p in out if cstr(p.receipt).strip().upper() not in elsewhere]


def book_sheet(sheet: str, rows=None, payouts=None, current: str | None = None, posting_date=None) -> str | None:
    """Post one submitted Journal Entry for every Paid payout of the sheet that no live journal
    books yet, under the sheet's lock (each row's bookings read again under it: a journal is never
    posted twice). Each row notes the journal; the payouts it books are marked booked and their
    statement lines reconciled with their part of it (``current``: the payout being booked by its
    own job, which does both itself). Returns the journal, or None when nothing waits."""
    from ..accounting.posting import insert_entry

    with owed.doc_lock(SHEET, sheet, timeout=owed.BOOKING_WAIT):
        if rows is None or payouts is None:
            rows, payouts = sync_rows(sheet, for_update=True, with_payouts=True)
        items = unbooked(rows, payouts)
        if not items:
            return None
        doc = frappe.db.get_value(SHEET, sheet, ["name", "title", "company", "docstatus"], as_dict=True)
        je = build_journal(doc, items, posting_date)
        insert_entry(je, submit=True, ignore_permissions=True)  # posted at once (accounting.posting)
        for row in {r.name: r for r, _p in items}.values():
            bookings = bookings_of(row)
            bookings.update({p.request: je.name for r, p in items if r.name == row.name})
            values = {"bookings": json.dumps(bookings, sort_keys=True), "journal_entry": je.name}
            frappe.db.set_value(ROW, row.name, values, update_modified=False)
            row.update(values)
        others = sorted({p.request for _r, p in items if p.request != current})
        for request in others:
            frappe.db.set_value(REQUEST, request, {"accounting_booked": 1, "accounting_error": None},
                                update_modified=False)
    total = flt(sum(p.amount for _r, p in items), 2)
    frappe.get_doc(SHEET, sheet).add_comment(
        "Comment", f"Journal Entry {je.name} books {len(items)} payouts paid by M-Pesa (KES {money(total)}).")
    from .statement import after_payout

    for request in others:
        after_payout(REQUEST, request)  # its statement line takes its part of the journal (never raises)
    return je.name


def book_later(sheet: str) -> None:
    """book_waiting from a job once this commits. Never raises."""
    try:
        frappe.enqueue(f"{__name__}.book_waiting", queue="short", timeout=600, enqueue_after_commit=True,
                       sheet=sheet)
    except Exception:
        frappe.log_error(title="M-Pesa bulk payout booking not queued",
                         message=f"{sheet}\n{frappe.get_traceback()}")


def book_waiting(sheet: str) -> str | None:
    """A job: once no row holds the sheet's journal (its last waiting row failed, or a manager marked
    it not paid), its Paid payouts are booked the way a Paid result books them
    (b2c_response_service.update_b2c_reference_status: one of them books them all, a failure is
    recorded on it and the 15-minute sweep tries again). Returns the payout it booked through.

    The sheet's lock first (held until the result job below commits): sync_rows writes the sheet's
    rows, and a booking that holds the lock reads them with a locking read (partyless.result_lock)."""
    with owed.doc_lock(SHEET, sheet, timeout=owed.BOOKING_WAIT):
        rows, payouts = sync_rows(sheet, for_update=True, with_payouts=True)
    waiting = unbooked(rows, payouts)
    if holding_rows(rows) or not waiting:
        return None
    from ..services.b2c_response_service import update_b2c_reference_status

    request = waiting[0][1].request
    update_b2c_reference_status(request, enqueue_next=True)
    return request


@frappe.whitelist(methods=["POST"])
def book_now(sheet: str, posting_date=None) -> dict:
    """Book Now: the sheet's Paid payouts no journal books yet, booked at once (after a closed
    period, a frozen account or a missing account was put right), whatever still waits.
    ``posting_date``: a later day than M-Pesa paid them (the day's period is closed). For an M-Pesa
    Manager or Accounts Manager who can read the sheet."""
    require(("M-Pesa Manager", "Accounts Manager"))  # BOOK_ROLES, spelt out for the permission matrix
    frappe.has_permission(SHEET, "read", doc=sheet, throw=True)
    if posting_date and getdate(posting_date) < getdate(frappe.db.get_value(SHEET, sheet, "posting_date")):
        frappe.throw("The Posting Date is the sheet's or a later day.")
    with owed.booking_lock(SHEET, sheet):
        journal = book_sheet(sheet, posting_date=posting_date)
    return {"journal_entry": journal}


# ---------------------------------------------------------------- the handler (money_out.partyless)
def _sheet_row(reference_row, sheet=None):
    name, is_test = split_paying_row(reference_row)
    if not name:
        return None, is_test
    row = frappe.db.get_value(ROW, name, ["name", "idx", "parent", "parenttype", "payee_name", "typed_name",
                                          "mpesa_name", "phone", "amount", "status", "verification", "project",
                                          "expense_type", "bookings"], as_dict=True)
    if row and (row.parenttype != SHEET or (sheet and row.parent != sheet)):
        row = None
    return row, is_test


class BulkPayoutHandler(partyless.Handler):
    """The sheet's party-less B2C rows (money_out.partyless)."""

    def check_row(self, row, parent) -> None:
        """The B2C row pays a row of an approved sheet, of the batch's company, to the row's phone,
        its whole amount (a KES 10 test: 10; a tested row: what is left), the row not settled, held
        or waiting for its test, and not paid or on its way by the sheet's own record."""
        sheet, idx = cstr(row.get("reference_name")).strip(), row.get("idx")
        values = frappe.db.get_value(SHEET, sheet, ["docstatus", "company"], as_dict=True) if sheet else None
        if not values:
            frappe.throw(f"Row #{idx}: {SHEET} {sheet} not found.")
        if cint(values.docstatus) != 1:
            frappe.throw(f"Row #{idx}: {sheet} is not approved (submitted)." if cint(values.docstatus) == 0 else
                         f"Row #{idx}: {sheet} is cancelled.")
        if parent is not None and values.company and parent.get("company") != values.company:
            frappe.throw(f"Row #{idx}: {sheet} belongs to {values.company}, not "
                         f"{parent.get('company') or 'no company'}.")
        found, is_test = _sheet_row(row.get("reference_row"), sheet)
        if not found:
            frappe.throw(f"Row #{idx}: {cstr(row.get('reference_row'))} is not a row of {sheet}.")
        if normalize_msisdn(row.get("partyb")) != found.phone:
            frappe.throw(f"Row #{idx}: the phone is not row #{found.idx}'s of {sheet} ({privacy.phone(found.phone)}).")
        if found.status == CANCELLED:
            frappe.throw(f"Row #{found.idx} of {sheet} ({found.payee_name}) is Cancelled: it is not paid by M-Pesa.")
        # read once per batch and sheet (each of its rows is checked on every save and at the send)
        payouts = [p for p in cp._memo(parent, f"mpsa_bulk_payouts:{sheet}", lambda: payouts_of(sheet))
                   if p.row == found.name]
        # the row's own status, from its other payouts (b2c_state: a cancelled payout M-Pesa may have
        # paid counts as on its way): its main payouts and its test together, whichever this one is
        mine = parent.get("name") if parent is not None else None
        others = [p for p in payouts if not (mine and p.parent == mine)]
        busy = [p for p in others if p.state in (IN_FLIGHT, PAID) and (is_test or not p.is_test)]
        if busy:
            p = max(busy, key=lambda p: (p.state == PAID, cstr(p.creation)))
            frappe.throw(f"Row #{idx}: row #{found.idx} of {sheet} ({found.payee_name}) is "
                         f"{'paid' if p.state == PAID else 'being paid'} by {p.parent}"
                         f"{f' (its KES {money(p.amount)} test)' if p.is_test else ''}"
                         f"{f', {p.request} ({p.request_status})' if p.request else ''}. A row is never paid twice; "
                         "a payout whose fate is unknown is asked about (Check with M-Pesa Now), never sent again.")
        tested = flt(sum(p.amount for p in payouts if p.is_test and p.state == PAID), 2)
        if is_test:
            expected = trial_amount()
            if found.verification in (VERIFIED, DIFFERS, RELEASED):
                frappe.throw(f"Row #{found.idx} of {sheet}: its test is done ({found.verification}); it is not "
                             "tested again.")
        else:
            expected = flt(flt(found.amount) - tested, 2)
            if any(p.is_test and p.state in LIVE for p in others):
                frappe.throw(f"Row #{found.idx} of {sheet}: its KES {money(trial_amount())} test is not answered "
                             "yet.")
            if found.verification == DIFFERS:
                frappe.throw(f"Row #{found.idx} of {sheet} is held: its KES {money(tested or trial_amount())} test was "
                             "paid under another name. An M-Pesa Manager checks it first (Release Held Row).")
        if abs(flt(row.get("allocated_amount")) - expected) > 0.001:
            frappe.throw(f"Row #{idx}: KES {money(row.get('allocated_amount'))} is not what row #{found.idx} of "
                         f"{sheet} pays (KES {money(expected)}).")

    def on_result(self, row, request) -> None:
        """The sheet's row follows its payout. A Paid payout is booked by its own job next; when a
        Failed one was the last the journal waited for, a job books the paid ones."""
        sheet = cstr(row.reference_name).strip()
        if not sheet:
            return
        # taken already by the result job, before it wrote the B2C row (partyless.result_lock): re-entered
        with owed.doc_lock(SHEET, sheet, timeout=owed.BOOKING_WAIT):
            rows, payouts = sync_rows(sheet, with_payouts=True)
        if row.payment_status != "Paid" and not holding_rows(rows) and unbooked(rows, payouts):
            book_later(sheet)

    def book(self, row):
        """Deferred while a row of the sheet waits for M-Pesa (less than the book-after hours since
        its send); else the sheet's journal books every Paid payout not booked yet, this one too."""
        sheet = cstr(row.reference_name).strip()
        rows, payouts = sync_rows(sheet, for_update=True, with_payouts=True)
        waiting = holding_rows(rows)
        if waiting:
            hours = settings().book_after_hours
            return partyless.deferred(
                f"{sheet}'s journal books its paid rows once no row waits for M-Pesa ({len(waiting)} waiting"
                + (f"; at most {hours} hours after a send)" if hours else ")"))
        book_sheet(sheet, rows, payouts, current=row.b2c_disbursement_request)
        if self.booked(row) is None:
            frappe.throw(f"{row.b2c_disbursement_request} pays {row.reference_row}, which is no row of {sheet} its "
                         "journal could book: book it by hand.")
        return None

    def booked(self, row) -> int | None:
        """The sheet's journal that books the payout (or the copy that amended it), else a live
        entry that carries its receipt (a voucher made from its bank line): booked by it, never
        swept into the sheet's next journal."""
        found, _test = _sheet_row(row.reference_row, row.reference_name)
        request = cstr(row.get("b2c_disbursement_request")).strip()
        if not found or not request:
            return None
        status = _journal_status(bookings_of(found).get(request))
        if status is not None:
            return status
        receipt = cstr(row.get("reference_no")).strip() or cstr(
            frappe.db.get_value(REQUEST, request, "transaction_id")).strip()
        return receipt_entries([receipt]).get(receipt.upper()) if receipt else None

    def statement_entry(self, request):
        found, _test = _sheet_row(request.reference_row, request.reference_name)
        journal, status = live_journal(bookings_of(found).get(request.name) if found else None)
        if status != 1:
            return None  # not booked yet; or booked by an entry carrying the receipt, found as usual
        return JE, journal, flt(request.amount, 2)

    def describe(self, row) -> dict:
        found, is_test = _sheet_row(row.get("reference_row"), row.get("reference_name"))
        if not found:
            return {}
        what = ", ".join(x for x in (found.project, found.expense_type) if cstr(x).strip())
        document = f"{SHEET} {found.parent}, row #{found.idx}"
        if is_test:
            document += f" (KES {money(row.get('allocated_amount') or trial_amount())} test of a new number)"
        # the name given: M-Pesa's own name for the number is shown beside it (known_names); the name
        # M-Pesa paid it under before is the self-pay checks' too (a retry's, as the sheet's own)
        return {"receiver": cstr(found.typed_name).strip() or found.payee_name, "party": what or None,
                "document": document, "mpesa_name": cstr(found.mpesa_name).strip() or None}

    def related_rows(self, paying_row) -> list:
        """A KES 10 test is held by its row's own payouts too: a row paid, or on its way, is not tested
        (a main payout is not held by its paid test: it pays the rest)."""
        name, is_test = split_paying_row(paying_row)
        return [name] if is_test and name else []

    def retry_problem(self, row, request) -> str:
        """A sheet's row is retried only by the sheet's own Retry Failed (a new payout, a new request),
        never by sending a failed request again (Retry Payment on it, Retry Failed Transactions on its
        batch): that would replace the OriginatorConversationID of the first attempt, whose late Result
        (a row marked as not paid may still be paid) could then never be matched."""
        sheet = cstr(partyless._value(row, "reference_name")).strip()
        name = cstr(partyless._value(request, "name")).strip() or "This payout"
        return (f"{name} pays a row of {SHEET} {sheet}: retry it with Retry Failed on {sheet}, which makes a new "
                "payout (a new B2C Disbursement Request with its own OriginatorConversationID). This request is "
                "not sent again: it stays the record of the first attempt, so a late Result for it is still "
                "matched.")

    def self_pay_rows(self, parent):
        out = []
        for ref in partyless._partyless_rows(parent):
            found, _test = _sheet_row(ref.get("reference_row"), ref.get("reference_name"))
            label = f"Row #{found.idx} of {found.parent}" if found else f"Row #{ref.get('idx')}"
            out.append({"phone": ref.get("partyb"),
                        "payee_name": (cstr(found.typed_name).strip() or found.payee_name) if found else None,
                        "mpesa_name": (found.mpesa_name or found.payee_name) if found else None, "label": label})
        return out

    def send_warnings(self, parent) -> list:
        """The Utility balance against what the batch sends, when it looks short."""
        from ..wallet import accounts as wallet_accounts
        from ..wallet import balances

        mop = cstr(parent.get("mode_of_payment"))
        settings_name = (frappe.db.get_value("Mpesa Settings", {"payment_gateway_name": mop[6:]}, "name")
                         if mop else None)
        if not settings_name:
            return []
        total = flt(sum(flt(r.get("allocated_amount")) for r in partyless._partyless_rows(parent)), 2)
        found = balances.available(settings_name, wallet_accounts.UTILITY)
        if found.amount is None:
            return [f"No Utility balance of the shortcode is known yet: check it covers KES {money(total)} before "
                    "sending (rows past the balance fail with insufficient funds; Retry Failed pays them after a "
                    "top-up)."]
        if flt(found.after) + 0.001 < total:
            return [f"The Utility account had about KES {money(found.after)} (Safaricom's last balance, less the "
                    f"payouts on their way), less than this batch's KES {money(total)}: top it up first, or the rows "
                    "past the balance fail with insufficient funds (Retry Failed pays them after a top-up)."]
        return []

    def closed_period_hint(self, row, receipt: str) -> str:
        return ("Its date is in a closed Accounting Period, in books closed by a Period Closing Voucher or on or "
                "before Accounts Frozen Till: open the period (or have an Accounts Manager pick a later Posting Date) "
                f"and press Book Now on {row.reference_name}; the sweep tries again every 15 minutes meanwhile.")


HANDLER = BulkPayoutHandler()


# ---------------------------------------------------------------- Paste Rows
def _resolve(doctype: str, value, title_field: str | None = None, company: str | None = None):
    """The record a pasted name means: its name, a name in any case, or its title (``company``: one
    of the company's)."""
    value = cstr(value).strip()
    if not value:
        return None
    if frappe.db.exists(doctype, value):
        return value
    filters = {"company": company} if company else {}
    for field in ("name", title_field):
        if not field:
            continue
        found = [n for n, v in ((r.name, r.get(field)) for r in frappe.get_all(
            doctype, filters=filters, fields=["name", field], order_by="name asc", limit=1000))
                 if cstr(v).strip().lower() == value.lower()]
        if len(found) == 1:
            return found[0]
    return None


@frappe.whitelist(methods=["POST"])
def parse_rows(text: str | None = None, file_url: str | None = None, sheet: str | None = None,
               company: str | None = None, existing=None, expense_type: str | None = None,
               cost_center: str | None = None, project: str | None = None, amount_column=None) -> dict:
    """The Paste Rows dialog: pasted rows (name, phone, amount; optional Expense Type, Project and
    Cost Center columns) or an uploaded CSV / XLSX, checked row by row with claim_payees' reader
    and checks (employees allowed). A phone M-Pesa paid before takes M-Pesa's name (what was pasted
    kept as Name Given). ``amount_column``: the column of the header row the user picked as the
    amount (round 4.5 review); the answer's ``columns`` says which column each field came from
    (claim_payees.read_rows). Adds nothing: the form appends the good rows, and the save checks them
    again. For who may change the sheet (or create one, before its first save)."""
    if sheet and frappe.db.exists(SHEET, sheet):
        frappe.has_permission(SHEET, "write", doc=sheet, throw=True)
        company = frappe.db.get_value(SHEET, sheet, "company")
    else:
        frappe.has_permission(SHEET, "create", throw=True)
    table = cp._read_file(file_url) if file_url else cp.parse_text(text)
    read = cp.read_rows(table, extra=EXTRA_COLUMNS, amount_column=amount_column)
    rows, left_out = read
    if len(rows) > PASTE_LIMIT:
        frappe.throw(f"Paste at most {PASTE_LIMIT} rows at a time.")
    if isinstance(existing, str):
        existing = frappe.parse_json(existing)
    names = cp.mpesa_names(m for m in (payable_msisdn(r.phone) for r in rows) if m)
    known = [names.get(payable_msisdn(r.phone)) for r in rows]
    typed = [cstr(r.payee_name).strip() for r in rows]
    for r, k in zip(rows, known):
        if k:
            r.payee_name = k.name
    # employees are paid here like anyone: no employee's or user's phone is refused, a phone may come twice
    checked = cp.check_pasted(rows, found=frappe._dict(destinations={}, bank_numbers={}), twice_ok=True)
    seen = {(normalize_msisdn(e[0]), cstr(e[1])) for e in existing or [] if isinstance(e, (list, tuple)) and e}
    cache = {}

    def link(doctype, value, title_field=None, of_company=False):
        key = (doctype, cstr(value).strip())
        if key not in cache:
            cache[key] = _resolve(doctype, value, title_field, company if of_company else None)
        return cache[key]

    from . import self_pay

    own = {label for label, _why in self_pay.matches(frappe.session.user, [
        {"phone": r["phone"], "payee_name": name, "mpesa_name": k.name if k else None, "label": str(r["row"])}
        for r, k, name in zip(checked, known, typed)])}
    for r, source, k, name in zip(checked, rows, known, typed):
        if k:
            r.update(mpesa_name=k.name, mpesa_name_from=cp.name_from(k),
                     typed_name=name if name and not pa.names_match(name, k.name) else None)
        r["warnings"] = []
        for field, doctype, title, default, of_company in (
                ("expense_type", CLAIM_TYPE, "expense_type", expense_type, False),
                ("project", "Project", "project_name", project, True),
                ("cost_center", "Cost Center", "cost_center_name", cost_center, True)):
            pasted = cstr(source.get(field)).strip()
            value = link(doctype, pasted, title, of_company) if pasted else (cstr(default).strip() or None)
            if pasted and not value:
                r["errors"].append(f"no {doctype} {pasted!r}")
            r[field] = value
        if str(r["row"]) in own:
            r["errors"].append("your own phone or name: you may not prepare a payment to yourself")
        key = (r["phone"], cstr(r.get("expense_type")))
        if key in seen:
            r["warnings"].append("the same phone for the same expense type is on the sheet already, or earlier in the "
                                 "list: check it is not one payment entered twice")
        seen.add(key)
    bad = sum(1 for r in checked if r["errors"])
    good_total = flt(sum(flt(r["amount"]) for r in checked if not r["errors"]), 2)
    return {"rows": checked, "good": len(checked) - bad, "bad": bad, "good_total": good_total, "left_out": left_out,
            "columns": read.columns}


# ---------------------------------------------------------------- the form
@frappe.whitelist(methods=["POST"])
def sheet_state(sheet: str | None = None) -> dict:
    """What the sheet's form shows: each row's status (brought up to date from its payouts), totals
    by project, the journals, what holds the journal or why it could not post, the paid rows whose
    statement line is not reconciled yet, failures grouped (insufficient funds: top up, then Retry
    Failed), and for a draft the M-Pesa names the approver checks. For who can read the sheet (a
    sheet not saved yet: who may create one, and the defaults)."""
    defaults = {"verify_new_numbers": settings().verify}
    if not sheet or not frappe.db.exists(SHEET, sheet):
        frappe.has_permission(SHEET, "create", throw=True)
        return {"defaults": defaults, "rows": [], "changed": False}
    frappe.has_permission(SHEET, "read", doc=sheet, throw=True)
    docstatus = cint(frappe.db.get_value(SHEET, sheet, "docstatus"))
    stored = read_rows(sheet)
    before = {r.name: tuple(_norm(f, r.get(f)) for f in RESULT_FIELDS) for r in stored}
    if docstatus == 1:
        rows, payouts = sync_rows(sheet, stored, with_payouts=True)
    else:
        rows, payouts = stored, []
    changed = any(before.get(r.name) != tuple(_norm(f, r.get(f)) for f in RESULT_FIELDS) for r in rows)
    by_project = {}
    for row in rows:
        part = by_project.setdefault(cstr(row.get("project")) or "", {"paid": 0.0, "failed": 0.0, "left": 0.0,
                                                                      "rows": 0})
        part["rows"] += 1
        part["paid"] = flt(part["paid"] + flt(row.get("paid_amount")), 2)
        if row.get("status") == FAILED:
            part["failed"] = flt(part["failed"] + flt(row.amount), 2)
        if row.get("status") not in (PAID, CANCELLED):
            part["left"] = flt(part["left"] + max(0.0, flt(row.amount) - flt(row.get("paid_amount"))), 2)
    # each as the live journal that books in its place (a cancelled one's amended copy)
    journals = sorted({live_journal(j)[0] or j for r in rows for j in bookings_of(r).values()})
    failures = {}
    for row in rows:
        failed = [p for p in payouts if p.row == row.name and p.state == FAILED]
        if row.get("status") == FAILED and failed:
            kind, hint = failure(failed[-1].result_code, failed[-1].error)
            group = failures.setdefault(kind or "other", {"rows": [], "amount": 0.0, "hint": hint})
            group["rows"].append(row.idx)
            group["amount"] = flt(group["amount"] + flt(row.amount), 2)
    out = {
        "defaults": defaults, "changed": changed, "docstatus": docstatus,
        "rows": [{f: row.get(f) for f in ("name", "idx", *RESULT_FIELDS) if f != "bookings"} for row in rows],
        "by_project": by_project,
        "journals": [{"name": j, "docstatus": frappe.db.get_value(JE, j, "docstatus")} for j in journals],
        "failures": failures,
        "holding": [r.idx for r in holding_rows(rows)] if docstatus == 1 else [],
        "book_after_hours": settings().book_after_hours,
        "can_book": not set(BOOK_ROLES).isdisjoint(frappe.get_roles()),
    }
    if docstatus == 0:
        out["names"] = names_summary(rows)
    if docstatus == 1:
        waiting = unbooked(rows, payouts)
        out["unbooked"] = {"rows": sorted({r.idx for r, _p in waiting}),
                           "amount": flt(sum(p.amount for _r, p in waiting), 2),
                           "problems": _booking_problems(p.request for _r, p in waiting)}
        out["unreconciled"] = _unreconciled(rows, payouts)
    return out


def _booking_problems(requests) -> list:
    """Why the journal did not post: the booking error the payouts carry (not a "Waiting" note)."""
    requests = sorted({r for r in requests if r})
    if not requests or not frappe.get_meta(REQUEST).has_field("accounting_error"):
        return []
    errors = frappe.get_all(REQUEST, filters={"name": ["in", requests]}, pluck="accounting_error")
    return sorted({cstr(e) for e in errors if cstr(e).strip() and not partyless.is_waiting_note(e)})


def _unreconciled(rows, payouts) -> list:
    """The booked paid rows whose statement line is not reconciled yet (still unallocated)."""
    if not frappe.get_meta(REQUEST).has_field("bank_transaction"):
        return []
    by_name = {r.name: r for r in rows}
    out = []
    for p in payouts:
        row = by_name.get(p.row)
        if p.state != PAID or not row or _journal_status(bookings_of(row).get(p.request)) != 1:
            continue
        line = frappe.db.get_value(REQUEST, p.request, "bank_transaction")
        left = flt(frappe.db.get_value("Bank Transaction", line, "unallocated_amount")) if line else None
        if line is None or left > owed.MIN:
            out.append({"row": row.idx, "request": p.request, "bank_transaction": line})
    return out


# ---------------------------------------------------------------- the daily alert (money_out.attention)
def attention_section():
    """The daily "needs attention" alert's section: sheets part paid, with failed or held rows, or
    with a row waiting for M-Pesa more than a day after its send. Its link opens the sheets' list."""
    if not frappe.db.table_exists(SHEET):
        return None
    cutoff = add_to_date(now_datetime(), hours=-ATTENTION_HOURS)
    lines = []
    for sheet in frappe.get_all(SHEET, filters={"docstatus": 1, "status": ["in", [TO_PAY, PAYING, PARTLY]]},
                                fields=["name", "title"], order_by="creation asc", limit=200):
        rows = sync_rows(sheet.name)
        counts = Counter(r.get("status") for r in rows)
        waiting = [r for r in rows if r.get("status") in LIVE and r.get("sent_on")
                   and get_datetime(r.sent_on) < cutoff]
        part_paid = counts[PAID] and counts[PAID] + counts[CANCELLED] < len(rows)
        if not (part_paid or counts[FAILED] or counts[ON_HOLD] or waiting):
            continue
        parts = [f"{counts[PAID]} paid"] if counts[PAID] else []
        if counts[FAILED]:
            failed = flt(sum(flt(r.amount) for r in rows if r.get("status") == FAILED), 2)
            parts.append(f"{counts[FAILED]} failed (KES {money(failed)})")
        if counts[ON_HOLD]:
            parts.append(f"{counts[ON_HOLD]} held for a manager")
        if waiting:
            parts.append(f"{len(waiting)} waiting for M-Pesa over {ATTENTION_HOURS} hours")
        left = flt(sum(max(0.0, flt(r.amount) - flt(r.get("paid_amount"))) for r in rows
                       if r.get("status") not in (PAID, CANCELLED)), 2)
        lines.append(f"{sheet.name} ({sheet.title}): {', '.join(parts)}; KES {money(left)} left")
    return "Bulk payout sheets", lines, f"{get_url().rstrip('/')}/app/m-pesa-bulk-payout?docstatus=1"
