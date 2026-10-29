"""M-Pesa payouts of Expense Claims and Employee Advances (HRMS), to the employee by B2C or B2B.

A submitted claim or advance is paid by M-Pesa only while ``payable_problem`` finds nothing in the
way: a claim approved, not paid at entry and with its Payable Account; an advance with its Advance
Account; in KES; in a workflow state M-Pesa Suite Settings lists as payable, when the site runs a
Workflow on the DocType and the list is set; and still owing by HRMS's own reading (a claim's
sanctioned amount plus taxes, less its advances and what was reimbursed; an advance's
advance_amount less paid_amount). Every pay path asks it: the form buttons and the list's Pay by
M-Pesa, a B2C row that pays the document (a B2C request made by hand may not), a B2B request
naming it, the retry of a failed B2C payout. A payout of the document pays its own employee,
except a payee of the claim (money_out.claim_payees). What is left to pay counts every payout of
the document on its way, whoever it pays (owed.employee_doc_left_to_pay), under the document's
lock.

The buttons only prepare a draft, for what is left in whole shillings: a B2C Payment Disbursement
of one row to the employee's approved phone (or, while M-Pesa Suite Settings lets B2C pay phones
nobody approved, the Employee's Cell Number), or a B2B request to one of the employee's approved
bank, paybill or till accounts. An M-Pesa Payer sends it with Send via M-Pesa; nobody pays
themselves. Pay by M-Pesa on the list prepares one B2C disbursement for the ticked documents of
employees paid by phone and a B2B request per document of an employee whose default account is a
bank account, paybill or till.

While M-Pesa Suite Settings says so (``b2c_approved_phones_only``, on for new sites), every B2C row
or request paying an employee goes to a phone approved on one of the employee's Employee M-Pesa
Accounts, and the row names that account: the name M-Pesa paid is checked against it.

A paid payout is booked as a Payment Entry HRMS accepts: party the document's employee, its own
Payable Account (claim) or Advance Account (advance), the document referenced
(money_out.b2b_entries for B2B, accounting.payment_entry_service for B2C), whoever M-Pesa paid.
These payouts are not salaries: Accounts roles see them (money_out.salary_access). A claim or
advance is not cancelled while one of its payouts is paid or on its way (``before_cancel``).
"""

import frappe
from frappe.utils import cint, cstr, flt, nowdate

from ..daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT
from ..money_in import common as money_in_common
from ..utils.phone import FORMATS, normalize_msisdn, payable_msisdn, shown
from ..permissions import PREPARE_ROLES, require
from . import employee_accounts, owed, party_accounts

EXPENSE_CLAIM, ADVANCE = "Expense Claim", "Employee Advance"
DOCTYPES = (EXPENSE_CLAIM, ADVANCE)
B2B = owed.B2B
DISB, DISB_ROW, B2C_REQUEST = "B2C Payment Disbursement", "B2C Payment Disbursement Reference", "B2C Disbursement Request"
LINE = owed.SCHEDULE_LINE
EMPLOYEE_ACCOUNT = party_accounts.EMPLOYEE_ACCOUNT
SUPPLIER_ACCOUNT = party_accounts.SUPPLIER_ACCOUNT
PHONE = party_accounts.PHONE
# The account types B2B pays (a phone is paid by B2C)
B2B_TYPES = party_accounts.B2B_TYPES
AUTO = "Book automatically"
# What the checks read of each document (fields the site's version lacks are left out)
READ = {
    EXPENSE_CLAIM: ("name", "docstatus", "employee", "employee_name", "company", "currency", "approval_status",
                    "status", "is_paid", "payable_account", "project", "cost_center", "grand_total",
                    "total_sanctioned_amount", "total_taxes_and_charges", "total_advance_amount",
                    "total_amount_reimbursed"),
    ADVANCE: ("name", "docstatus", "employee", "employee_name", "company", "currency", "exchange_rate", "status",
              "advance_account", "advance_amount", "paid_amount"),
}
# Payouts still on their way, or Paid (the payroll cancel guard's)
B2B_LIVE = ("Not Initiated", "Initiated", "Unknown")
B2C_LIVE = ("Not Initiated", "Initiated", "Status Unknown")
LINE_SENT = ("Sending", "Sent", "Unknown", "Paid")
# Most documents one Pay by M-Pesa prepares
BULK_LIMIT = 200


def _settings():
    try:
        return money_in_common.suite()
    except frappe.DoesNotExistError:
        return frappe._dict()


def approved_phones_only() -> bool:
    """M-Pesa Suite Settings: an employee is paid by B2C only to a phone approved on one of their
    Employee M-Pesa Accounts."""
    return bool(cint(_settings().get("b2c_approved_phones_only")))


def payable_states() -> list:
    """The workflow states M-Pesa Suite Settings lists as payable (one per line); [] for any."""
    return [s.strip() for s in cstr(_settings().get("claim_payable_workflow_states")).splitlines() if s.strip()]


def workflow_state_field(doctype: str) -> str | None:
    """The state field of the DocType's active Workflow, or None when it runs none."""
    workflow = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1},
                                   ["name", "workflow_state_field"], as_dict=True)
    if not workflow:
        return None
    return workflow.workflow_state_field or "workflow_state"


def payable_states_of(doctype: str) -> list:
    """The listed payable workflow states (payable_states) that apply to ``doctype``. The setting is
    the Expense Claim's (its label says so): the whole list applies to a claim. An Employee
    Advance under a Workflow of its own takes only the listed states its Workflow has: a site that
    lists its claim states ('Approved', 'Pending Attachment') does not refuse every advance in its
    own final state ('Approved by HOD'), and one that lists an advance state holds advances to it."""
    states = payable_states()
    if doctype != ADVANCE or not states:
        return states
    workflow = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name")
    if not workflow:
        return []
    own = set(frappe.get_all("Workflow Document State", filters={"parent": workflow, "parenttype": "Workflow"},
                             pluck="state"))
    return [s for s in states if s in own]


def _check_doctype(doctype: str) -> None:
    if doctype not in DOCTYPES:
        frappe.throw("Pick an Expense Claim or an Employee Advance.")


def money(value) -> str:
    return owed.money(value)


def whole_shillings(left) -> int:
    """What M-Pesa can pay of ``left``: whole shillings, never more than is left."""
    return int(flt(left, 2) + 0.001)


# ---------------------------------------------------------------- the document
def read_document(dt: str, dn: str, for_update: bool = False):
    """The claim's or advance's fields the checks need, its workflow state among them, or None."""
    meta = frappe.get_meta(dt)
    fields = [f for f in READ[dt] if f in ("name", "docstatus") or meta.has_field(f)]
    state = workflow_state_field(dt)
    if state and state not in fields and meta.has_field(state):
        fields.append(state)
    values = frappe.db.get_value(dt, dn, fields, as_dict=True, for_update=for_update) if dn else None
    if values:
        values.doctype = dt
        values.state_field = state
    return values


def _values(doc, doctype=None):
    """``doc`` as read_document reads it: what it read already, or a document read now."""
    if doc is None:
        return None
    if isinstance(doc, dict) and "state_field" in doc:
        return doc
    dt = doctype or _doctype_of(doc)
    return read_document(dt, getattr(doc, "name", None) or doc.get("name")) if dt in DOCTYPES else None


def _doctype_of(doc) -> str | None:
    return (getattr(doc, "doctype", None) or doc.get("doctype")) if doc is not None else None


def party_account(dt: str, values) -> str | None:
    """The account HRMS pays the document on (employee_payment_entry.get_party_account, v15 and
    v16): a claim's Payable Account, an advance's Advance Account. ERPNext refuses any other on a
    Payment Entry that references the claim."""
    return values.get("payable_account") if dt == EXPENSE_CLAIM else values.get("advance_account")


def outstanding(dt: str, values) -> float:
    """What HRMS says the document still owes (utils.payouts.owed)."""
    from ..utils.payouts import owed as owed_by

    return flt(owed_by(dt, values), 2) if values and cint(values.get("docstatus")) == 1 else 0.0


def _currency(values) -> str:
    """The document's currency: v16's own field, else its company's (a v15 claim has none)."""
    return (values.get("currency")
            or (values.get("company") and frappe.db.get_value("Company", values.company, "default_currency"))
            or "KES")


def payable_problems(dt: str, values, for_update: bool = False) -> list:
    """Why the claim or advance may not be paid by M-Pesa now, as plain messages."""
    if dt not in DOCTYPES:
        return [f"{dt} is not an Expense Claim or Employee Advance."]
    if not values:
        return [f"{dt} not found"]
    name = values.name
    problems = []
    if cint(values.docstatus) == 2:
        problems.append(f"{name} is cancelled.")
    elif cint(values.docstatus) != 1:
        problems.append(f"{name} is not submitted.")
    if dt == EXPENSE_CLAIM:
        if values.get("approval_status") != "Approved":
            problems.append(f"{name} is not approved (Approval Status: {values.get('approval_status') or 'Draft'}).")
        if cint(values.get("is_paid")):
            problems.append(f"{name} was paid when it was entered (Is Paid).")
    if not party_account(dt, values):
        problems.append(f"{name} has no {'Payable Account' if dt == EXPENSE_CLAIM else 'Advance Account'}.")
    states = payable_states_of(dt)
    if values.get("state_field") and states:
        state = cstr(values.get(values.state_field)).strip()
        if state not in states:
            problems.append(f"{name} is in workflow state {state or '(none)'}, which M-Pesa Suite Settings does not "
                            "list among the workflow states that may be paid.")
    currency = _currency(values)
    if currency != "KES":
        problems.append(f"{name} is in {currency}. M-Pesa pays KES only.")
    elif dt == ADVANCE and values.get("exchange_rate") not in (None, "") and flt(values.exchange_rate) not in (0, 1):
        problems.append(f"{name} has an exchange rate of {values.exchange_rate}. M-Pesa pays KES only.")
    account = party_account(dt, values)
    account_currency = frappe.db.get_value("Account", account, "account_currency") if account else None
    if account_currency and account_currency != "KES":
        problems.append(f"{account} is in {account_currency}. M-Pesa pays KES only.")
    gap = reimbursed_problem(dt, values, for_update)
    if gap:
        problems.append(gap)
    if cint(values.docstatus) == 1 and outstanding(dt, values) <= owed.MIN:
        problems.append(f"{name} has nothing left to pay.")
    return problems


def reimbursed_problem(dt: str, values, for_update: bool = False) -> str | None:
    """A submitted claim whose Total Amount Reimbursed disagrees with the entries booked against it
    (claim_payees.reimbursed_gap): what it still owes is read from that total, so nothing more is
    prepared or sent for it, to the claimant or a payee, until it is set right (check_paid_total, or
    an M-Pesa Manager's Repair Reimbursed Total). ``for_update``: a send or submit under the claim's
    lock, both sides read as they are now (claim_payees.reimbursed_gap)."""
    if dt != EXPENSE_CLAIM or not values or cint(values.get("docstatus")) != 1:
        return None
    from .claim_payees import reimbursed_gap

    try:
        gap = reimbursed_gap(values, for_update=for_update)
    except ImportError:
        return None  # no HRMS: no Expense Claim to pay
    if not gap:
        return None
    return (f"{values.name}'s Total Amount Reimbursed shows KES {money(gap[0])}, but the entries booked against it add "
            f"up to KES {money(gap[1])}: an M-Pesa Manager repairs it (Repair Reimbursed Total) before it is paid "
            "again.")


# ---------------------------------------------------------------- the paid total HRMS keeps
def check_paid_total_later(doc, method=None) -> None:
    """Payment Entry and Journal Entry on_submit and on_cancel (hooks.py): each Expense Claim or
    Employee Advance the entry pays is checked again by check_paid_total, in a job of its own once
    this commits. Never raises: the save goes on."""
    try:
        for dt, dn in _paid_documents(doc):
            frappe.enqueue(f"{__name__}.check_paid_total", queue="short", timeout=300, enqueue_after_commit=True,
                           doctype=dt, name=dn)
    except Exception:
        frappe.log_error(title="M-Pesa paid total check not queued", message=f"{doc.name}\n{frappe.get_traceback()}")


def _paid_documents(doc) -> list:
    if doc.doctype == "Payment Entry":
        found = [(r.get("reference_doctype"), r.get("reference_name")) for r in doc.get("references") or []]
    else:
        found = [(r.get("reference_type"), r.get("reference_name")) for r in doc.get("accounts") or []]
    return sorted({(dt, cstr(dn).strip()) for dt, dn in found if dt in DOCTYPES and cstr(dn).strip()})


def check_paid_total(doctype: str, name: str) -> None:
    """A job, after a payment of an Expense Claim or Employee Advance was submitted or cancelled:
    what HRMS wrote back as paid (a claim's Total Amount Reimbursed, an advance's Paid Amount) is
    counted again from the entries, under the document's lock taken first (owed.booking_lock), and
    set again when it differs, with a Comment. Two bookings that each wrote a total from a snapshot
    without the other's entry are left right. Idempotent."""
    if doctype not in DOCTYPES:
        return
    with owed.booking_lock(doctype, name):
        if not frappe.db.exists(doctype, name):
            return
        doc = frappe.get_doc(doctype, name)
        if cint(doc.docstatus) != 1:
            return
        if doctype == EXPENSE_CLAIM:
            from hrms.hr.doctype.expense_claim.expense_claim import update_reimbursed_amount

            from .claim_payees import reimbursed_gap

            gap = reimbursed_gap(doc)
            if not gap:
                return
            update_reimbursed_amount(doc)
            label, before, after = "Total Amount Reimbursed", gap[0], gap[1]
        else:
            before = flt(doc.get("paid_amount"), 2)
            doc.set_total_advance_paid()
            after = flt(frappe.db.get_value(doctype, name, "paid_amount"), 2)
            if abs(after - before) < 0.005:
                return
            label = "Paid Amount"
        doc.add_comment("Comment", f"{label} set from KES {money(before)} to KES {money(after)}, the entries booked "
                                   "against it: two payments were booked at the same moment.")


def payable_problem(doc, doctype: str | None = None, skip_gap: bool = False, for_update: bool = False) -> str | None:
    """Why a submitted Expense Claim or Employee Advance may not be paid now, or None. ``doc``: the
    document, or what read_document read of it. ``skip_gap``: not the reimbursed total's gap
    (reimbursed_problem), for a form that says it in words of its own. ``for_update``: a send or
    submit under the document's lock (the reimbursed total read as it is now)."""
    values = _values(doc, doctype)
    dt = doctype or (values or {}).get("doctype") or _doctype_of(doc)
    problems = payable_problems(dt, values, for_update)
    if skip_gap and problems:
        gap = reimbursed_problem(dt, values, for_update)
        problems = [p for p in problems if p != gap]
    return problems[0] if problems else None


def booking_party(dt: str | None, dn: str | None):
    """Who a paid payout of an Expense Claim or Employee Advance is booked to: _dict(employee,
    account), whoever M-Pesa paid; None for any other document."""
    if dt not in DOCTYPES or not dn:
        return None
    values = read_document(dt, dn)
    if not values:
        return None
    return frappe._dict(employee=values.employee, account=party_account(dt, values))


# ---------------------------------------------------------------- B2C rows and requests
def b2c_phone(employee) -> tuple:
    """(phone, account) a B2C payout to ``employee`` goes to: the phone of their default (or only)
    approved phone account that can be paid now; ("", None) when they have approved phones and
    none can be picked now (a default on hold or blocked is never passed over for a number nobody
    approved, as supplier_b2c_phone). While M-Pesa Suite Settings lets B2C pay phones nobody
    approved, the Employee's Cell Number when they have no approved phone; ("", None) otherwise."""
    if not employee:
        return "", None
    if party_accounts.accounts_of(EMPLOYEE_ACCOUNT, employee, (PHONE,)):
        account = party_accounts.pick_account(EMPLOYEE_ACCOUNT, employee, (PHONE,))
        if not account:
            return "", None
        return cstr(party_accounts.get_account(EMPLOYEE_ACCOUNT, account).phone).strip(), account
    if approved_phones_only():
        return "", None
    # as 2547... / 2541... when B2C can pay it; one it cannot as typed (cell_number_problem says why)
    cell = cstr(frappe.db.get_value("Employee", employee, "cell_number")).strip()
    return payable_msisdn(cell) or cell, None


def cell_number_problem(employee, phone) -> str | None:
    """Why the Cell Number b2c_phone found cannot be paid by B2C (an approved phone always can), or
    None: the Pay employee (phone) refusal, before any payout is made."""
    if not phone or payable_msisdn(phone):
        return None
    return (f"The Cell Number on {employee} ({shown(phone)}) is not a Kenyan mobile number M-Pesa can pay. HR "
            f"corrects it on the Employee: {FORMATS}.")


def _approved_phone(employee, phone):
    """The employee's approved phone account for ``phone`` (one that can be paid now first), or None:
    the same number however it is written (07..., 2547..., +254...)."""
    phone = normalize_msisdn(phone)
    matches = [a for a in party_accounts.accounts_of(EMPLOYEE_ACCOUNT, employee, (PHONE,))
               if phone and normalize_msisdn(a.phone) == phone]
    usable = [a for a in matches if not party_accounts.usable_problem(a, employee, EMPLOYEE_ACCOUNT)]
    return (usable or matches or [None])[0]


def phone_lock_problem(employee, phone) -> str | None:
    """Why a B2C payout to ``employee`` may not go to ``phone`` while M-Pesa Suite Settings pays
    employees only to approved phones (None when it may). With the setting off, a number typed in
    that is an approved account's is still paid only while that account can be paid (not on hold or
    blocked); another number is not checked."""
    acc = _approved_phone(employee, phone)
    if not approved_phones_only():
        return party_accounts.usable_problem(acc, employee, EMPLOYEE_ACCOUNT) if acc else None
    if not acc:
        return (f"{cstr(phone).strip() or 'No number'} is not a phone approved for {employee}. M-Pesa Suite Settings pays "
                "employees by B2C only to an approved phone: add it under Employee > M-Pesa Accounts and have a manager "
                "approve it.")
    return party_accounts.usable_problem(acc, employee, EMPLOYEE_ACCOUNT)


def _sent_already(row) -> bool:
    """A row an amend copied from a disbursement that sent it: it is not sent again."""
    request = getattr(row, "b2c_disbursement_request", None)
    status = frappe.db.get_value(B2C_REQUEST, request, "status") if request else None
    return bool(status) and status != "Failed"


def not_its_employee(dt: str, dn: str, employee, party_type, party) -> str | None:
    """Why a payout of the claim or advance may not pay ``party``: it pays the document's own
    employee (Party Type Employee). A merchant or another person is paid on a claim as a payee of
    the claim (claim_payees), which books it to the claimant all the same."""
    if party_type == "Employee" and party == employee:
        return None
    who = party if party_type == "Employee" else f"{party_type or 'no party type'} {party or ''}".strip()
    message = f"{dn} belongs to {employee}, not {who}: an {dt} is paid to its own employee."
    if dt == EXPENSE_CLAIM:
        message += " Pay a merchant or another person as a payee of the claim."
    return message


def check_b2c_row(row) -> None:
    """A B2C Payment Disbursement row (its validate, also at submit). A row paying a claim or
    advance pays one that may be paid, to its own employee (Party Type Employee, whatever party
    type the row was given). A row paying an employee goes, while M-Pesa Suite Settings says so,
    to a phone approved on one of the employee's accounts, and names the approved account it pays
    (for the name check after payment). A payment schedule's row is its line's
    (schedule.check_b2c_row); a payee of a claim is not called here (claim_payees)."""
    if _sent_already(row):
        return
    dt, dn = getattr(row, "reference_doctype", None), cstr(getattr(row, "reference_name", None)).strip()
    party_type, party = getattr(row, "party_type", None), getattr(row, "party", None)
    if dt in DOCTYPES and dn:
        values = read_document(dt, dn)
        problem = payable_problem(values, dt)
        if problem:
            frappe.throw(f"Row #{row.idx}: {problem}")
        # booked to the document's employee whoever is paid (payment_entry_service): so paid to them
        problem = not_its_employee(dt, dn, values.employee, party_type, party)
        if problem:
            frappe.throw(f"Row #{row.idx}: {problem}")
    if party_type == "Supplier" and party:
        check_supplier_b2c_row(row)
        return
    if party_type != "Employee" or not party or getattr(row, "mpesa_schedule_line", None):
        return
    acc = _approved_phone(party, getattr(row, "partyb", None))
    row.payee_account_doctype = EMPLOYEE_ACCOUNT if acc else None
    row.payee_account = acc.name if acc else None
    problem = phone_lock_problem(party, getattr(row, "partyb", None))
    if problem:
        frappe.throw(f"Row #{row.idx}: {problem}")


def supplier_b2c_phone(supplier):
    """The phone a B2C payout to ``supplier`` goes to when it has approved Phone (B2C) Supplier M-Pesa
    Accounts: its default (or only) one that can be paid now, or "" when none can be paid now. None
    when it has none: the Supplier's Contact phone is used, as before the payee accounts."""
    if not supplier or not party_accounts.accounts_of(SUPPLIER_ACCOUNT, supplier, (PHONE,)):
        return None
    account = party_accounts.pick_account(SUPPLIER_ACCOUNT, supplier, (PHONE,))
    return cstr(party_accounts.get_account(SUPPLIER_ACCOUNT, account).phone).strip() if account else ""


def check_supplier_b2c_row(row) -> None:
    """A B2C Payment Disbursement row paying a supplier that has approved Phone (B2C) Supplier
    M-Pesa Accounts (approved by a second person with proof and a hold period): it pays one of them
    that can be paid now, never a Contact number nobody approved, and names it (the name M-Pesa paid
    is checked against it, and maker-checker watches it). On every save and at the send. A supplier
    with no approved phone account is paid to its Contact's phone, as before."""
    acc, problem = supplier_phone(getattr(row, "party", None), getattr(row, "partyb", None))
    row.payee_account_doctype = SUPPLIER_ACCOUNT if acc else None
    row.payee_account = acc.name if acc else None
    if problem:
        frappe.throw(f"Row #{row.idx}: {problem}")


def supplier_phone(supplier, phone) -> tuple:
    """(account, problem) of a B2C payout to ``supplier`` at ``phone``: its approved Phone (B2C)
    Supplier M-Pesa Account for the number (one that can be paid now first), and why the payout may
    not go there (None when it may). A supplier with approved phone accounts is paid only to one of
    them that can be paid now; one with none, to any number."""
    phones = party_accounts.accounts_of(SUPPLIER_ACCOUNT, supplier, (PHONE,))
    number = normalize_msisdn(phone)
    matches = [a for a in phones if number and normalize_msisdn(a.phone) == number]
    usable = [a for a in matches if not party_accounts.usable_problem(a, supplier, SUPPLIER_ACCOUNT)]
    acc = (usable or matches or [None])[0]
    if not phones:
        return acc, None
    if not acc:
        return None, (f"{cstr(phone).strip() or 'No number'} is not a phone approved for {supplier}. {supplier} has "
                      "approved M-Pesa phone accounts, so B2C pays only one of them (Supplier > M-Pesa Accounts).")
    return acc, party_accounts.usable_problem(acc, supplier, SUPPLIER_ACCOUNT)


def check_b2c_request(doc) -> None:
    """A B2C Disbursement Request made by hand (not by its disbursement's job or a payment
    schedule, whose rows were checked). It never pays an Expense Claim, Employee Advance or Salary
    Slip: no amount check or lock guards it, and nothing counts it against the document while it is
    on its way (owed.b2c_open and b2c_slip_open list disbursement rows), so a second payout of the
    document could pass, or it could pay more than is owed. They are paid by a disbursement (Pay
    employee (phone)) or, a salary, from a payment schedule line. An employee is paid to an approved
    phone (as a disbursement row is), nobody sends a payout to themselves, and a payment to an
    employee that no document caps is sent by an M-Pesa Manager only (as a B2B request or a
    schedule line with none). A supplier with approved phone accounts is paid only to one of them
    that can be paid now (supplier_phone, as a disbursement row is)."""
    flags = getattr(doc, "flags", None) or frappe._dict()
    if flags.get("mpesa_from_disbursement") or flags.get("mpesa_from_schedule"):
        return
    dt = doc.get("reference_doctype")
    if dt in DOCTYPES:
        frappe.throw(f"An {dt} is paid by a B2C Payment Disbursement (Pay employee (phone) on the {dt}), not by a "
                     "B2C Disbursement Request made by hand.")
    if dt == owed.SLIP:
        frappe.throw("A Salary Slip is paid from an M-Pesa Payment Schedule line (Pay by M-Pesa on the slip or its "
                     "Payroll Entry) or a B2C Payment Disbursement row, not by a B2C Disbursement Request made by "
                     "hand.")
    if doc.get("party_type") == "Supplier" and doc.get("party"):
        problem = supplier_phone(doc.party, doc.get("phone_number"))[1]
        if problem:
            frappe.throw(problem)
    if doc.get("party_type") == "Employee" and doc.get("party"):
        problem = phone_lock_problem(doc.party, doc.get("phone_number"))
        if problem:
            frappe.throw(problem)
        if cint(doc.get("docstatus")) == 1:  # being sent
            problem = employee_accounts.own_payment_problem(doc.party)
            if problem:
                frappe.throw(problem, frappe.PermissionError)
            if not employee_accounts.is_manager():
                from .b2b import NO_DOCUMENT

                frappe.throw(NO_DOCUMENT, frappe.PermissionError)
    problem = row_mismatch(doc)
    if problem:
        frappe.throw(problem)


def row_mismatch(doc) -> str | None:
    """Why a B2C request does not pay what the disbursement row it names says: a row of another
    disbursement, or another party, number, amount or document. The disbursement's job copies them
    from the row, so only a request made by hand differs, and the row's checks (approved phones,
    claims, payees) would then not be its checks: one that leaves its party out, say, skips them."""
    ref = cstr(getattr(doc, "b2c_payment_disbursement_reference", None)).strip()
    row = ref and frappe.db.get_value(DISB_ROW, ref, ["parent", "party_type", "party", "partyb", "allocated_amount",
                                                      "reference_doctype", "reference_name"], as_dict=True)
    disbursement = cstr(getattr(doc, "b2c_payment_disbursement", None)).strip()
    if not row or row.parent != disbursement:
        return f"{ref or 'No row'} is not a row of {disbursement or 'its disbursement'}: a B2C request pays a row of its disbursement."
    differs = [label for label, mine, its in (
        ("party type", getattr(doc, "party_type", None), row.party_type),
        ("party", getattr(doc, "party", None), row.party),
        ("document", f"{getattr(doc, 'reference_doctype', None) or ''} {getattr(doc, 'reference_name', None) or ''}",
         f"{row.reference_doctype or ''} {row.reference_name or ''}"),
    ) if cstr(mine).strip() != cstr(its).strip()]
    number = getattr(doc, "phone_number", None)
    if (normalize_msisdn(number) or cstr(number).strip()) != (normalize_msisdn(row.partyb) or cstr(row.partyb).strip()):
        differs.append("phone number")
    if abs(flt(getattr(doc, "amount", None)) - flt(row.allocated_amount)) >= 0.01:
        differs.append("amount")
    if differs or not cstr(row.party).strip():
        return (f"This B2C request's {', '.join(differs) or 'party'} is not that of {disbursement} row {ref}: a B2C "
                "request pays exactly what its disbursement row says.")
    return None


def _row_claim_payee(doc) -> str | None:
    """The claim payee (claim_payees) of the disbursement row a B2C request pays, or None."""
    ref = cstr(getattr(doc, "b2c_payment_disbursement_reference", None)).strip()
    if not ref or not frappe.get_meta(DISB_ROW).has_field("claim_payee"):
        return None
    return cstr(frappe.db.get_value(DISB_ROW, ref, "claim_payee")).strip() or None


def _supplier_retry_problem(doc) -> str | None:
    """Why a failed B2C payout to a supplier may not be sent again: the Supplier M-Pesa Account its
    disbursement row named was since cancelled (a stolen number goes at once), put on hold or
    blocked, or is not this number. A payout whose row named none pays only one of the supplier's
    approved phones that can be paid now, should it have any since (supplier_phone)."""
    supplier, phone = doc.party, getattr(doc, "phone_number", None)
    ref = cstr(getattr(doc, "b2c_payment_disbursement_reference", None)).strip()
    row = None
    if ref and frappe.get_meta(DISB_ROW).has_field("payee_account"):
        row = frappe.db.get_value(DISB_ROW, ref, ["payee_account_doctype", "payee_account"], as_dict=True)
    if row and row.payee_account_doctype == SUPPLIER_ACCOUNT and row.payee_account:
        acc = party_accounts.get_account(SUPPLIER_ACCOUNT, row.payee_account)
        problem = party_accounts.usable_problem(acc, supplier, SUPPLIER_ACCOUNT)
        if problem:
            return problem
        if normalize_msisdn(acc.phone) != normalize_msisdn(phone):
            return (f"{cstr(phone).strip() or 'No number'} is not the phone of {acc.name}, the approved account of "
                    f"{supplier} this payout was made for.")
        return None
    return supplier_phone(supplier, phone)[1]


def check_b2c_retry(doc) -> None:
    """A failed B2C payout sent again (Retry on the request or on its disbursement), under the
    document's lock: what its send checked is checked again, since the claim, the phone account
    or the payer may have changed since. The claim or advance it pays may still be paid; an
    employee is paid only to an approved phone that can be paid now while M-Pesa Suite Settings
    says so (a payee of the claim is claim_payees'); a supplier only to the approved account its
    row named, or one of its approved phones (_supplier_retry_problem); nobody pays themselves."""
    dt, dn = getattr(doc, "reference_doctype", None), cstr(getattr(doc, "reference_name", None)).strip()
    party = getattr(doc, "party", None) if getattr(doc, "party_type", None) == "Employee" else None
    employees = set()
    if dt in DOCTYPES and dn:
        values = read_document(dt, dn)
        problem = payable_problem(values, dt)
        if problem:
            frappe.throw(problem)
        employees.add(values.employee)
    if getattr(doc, "party_type", None) == "Supplier" and getattr(doc, "party", None):
        problem = _supplier_retry_problem(doc)
        if problem:
            frappe.throw(problem)
    problem = row_mismatch(doc)  # a request made by hand before row_mismatch was checked at its save
    if problem:
        frappe.throw(problem)
    if party:
        employees.add(party)
        if not _row_claim_payee(doc):
            problem = phone_lock_problem(party, getattr(doc, "phone_number", None))
            if problem:
                frappe.throw(problem)
    for employee in sorted(e for e in employees if e):
        problem = employee_accounts.own_payment_problem(employee)
        if problem:
            frappe.throw(problem, frappe.PermissionError)


def check_not_own_b2c(doc) -> None:
    """Nobody pays themselves (a B2C Payment Disbursement's submit): not a row's employee, nor the
    employee of a claim or advance a row pays. A payment schedule's rows were checked at Pay
    Selected."""
    if (getattr(doc, "flags", None) or {}).get("mpesa_from_schedule"):
        return
    for row in doc.get("references") or []:
        dt, dn = getattr(row, "reference_doctype", None), getattr(row, "reference_name", None)
        employees = {getattr(row, "party", None)} if getattr(row, "party_type", None) == "Employee" else set()
        if dt in DOCTYPES and dn:
            employees.add(frappe.db.get_value(dt, dn, "employee"))
        for employee in sorted(e for e in employees if e):
            problem = employee_accounts.own_payment_problem(employee)
            if problem:
                frappe.throw(f"Row #{row.idx}: {problem}", frappe.PermissionError)


def payable_entries(doctype: str, entries: list) -> list:
    """Get References on a disbursement: the claims or advances that may be paid now."""
    if doctype not in DOCTYPES:
        return entries
    return [e for e in entries if not payable_problem(read_document(doctype, e.get("name")), doctype)]


# ---------------------------------------------------------------- the form: headline and buttons
def can_prepare(user: str | None = None) -> bool:
    return not set(PREPARE_ROLES).isdisjoint(frappe.get_roles(user))


def paid_by_mpesa(dt: str, dn: str) -> float:
    """What M-Pesa paid for the document: its Paid B2C and B2B payouts, booked or not."""
    total = flt(frappe.db.sql(
        f"""SELECT COALESCE(SUM(amount), 0) FROM `tab{B2C_REQUEST}`
        WHERE reference_doctype = %s AND reference_name = %s AND docstatus IN (1, 2) AND status = 'Paid'""",
        (dt, dn))[0][0])
    field = owed.EMPLOYEE_DOC_FIELDS[dt]
    if frappe.db.has_column(B2B, field):
        total += flt(frappe.db.sql(
            f"""SELECT COALESCE(SUM(amount), 0) FROM `tab{B2B}`
            WHERE `{field}` = %s AND docstatus IN (1, 2) AND status = 'Paid'""", (dn,))[0][0])
    return flt(total, 2)


def draft_b2c(dt: str, dn: str, payees: bool = True) -> list:
    """Draft B2C Payment Disbursements with a row for the document (they count as paying it only
    once they are sent). ``payees`` False: only rows that pay the employee, not an Expense Claim's
    casual payees (money_out.claim_payees: their unpaid lines are held back from the employee's
    share already)."""
    payee_rows = "" if payees or not frappe.db.has_column(DISB_ROW, "claim_payee") else (
        " AND COALESCE(ref.claim_payee, '') = ''")
    return [r[0] for r in frappe.db.sql(
        f"""SELECT DISTINCT ref.parent FROM `tab{DISB_ROW}` ref JOIN `tab{DISB}` d ON d.name = ref.parent
        WHERE ref.reference_doctype = %s AND ref.reference_name = %s AND d.docstatus = 0{payee_rows}
        ORDER BY ref.parent""",
        (dt, dn))]


def draft_payouts(dt: str, dn: str) -> list:
    """Draft B2C Payment Disbursements and B2B requests that would pay the document, as
    {doctype, name}."""
    found = [{"doctype": DISB, "name": name} for name in draft_b2c(dt, dn)]
    field = owed.EMPLOYEE_DOC_FIELDS[dt]
    if frappe.db.has_column(B2B, field):
        found += [{"doctype": B2B, "name": name}
                  for name in frappe.get_all(B2B, filters={field: dn, "docstatus": 0}, pluck="name",
                                             order_by="creation asc")]
    return found


def _masked_phone(phone) -> str:
    phone = cstr(phone).strip()
    return f"{phone[:4]}****{phone[-4:]}" if len(phone) > 8 else phone


def _masked(number) -> str:
    number = cstr(number).strip()
    return f"****{number[-4:]}" if len(number) > 4 else number


def _account_label(acc) -> str:
    """'Till (Buy Goods) 835811', 'Paybill 888880, account ****AC-1 (default)': what the dialog shows."""
    label = f"{acc.account_type} {cstr(acc.shortcode).strip()}"
    if acc.get("bank"):
        label += f" ({acc.bank})"
    if cstr(acc.get("account_number")).strip():
        label += f", account {_masked(acc.account_number)}"
    return label + (" (default)" if cint(acc.get("is_default")) else "")


def shortcodes(company: str, flag: str) -> list:
    """Mpesa Settings of the company that pay by B2C (``use_b2c``) or B2B (``use_b2b``)."""
    filters = {flag: 1}
    if company and frappe.get_meta("Mpesa Settings").has_field("company"):
        filters["company"] = company
    return [s.name for s in frappe.get_all("Mpesa Settings", filters=filters, fields=["name", "initiator_name"],
                                            order_by="creation asc") if s.initiator_name]


def _pay_options(dt: str, values) -> dict:
    employee = values.employee
    phone, phone_account = b2c_phone(employee)
    accounts = [a for a in party_accounts.accounts_of(EMPLOYEE_ACCOUNT, employee, B2B_TYPES)
                if not party_accounts.usable_problem(a, employee, EMPLOYEE_ACCOUNT)]
    return {
        "phone": _masked_phone(phone),
        "phone_problem": cell_number_problem(employee, phone),
        "phone_approved": bool(phone_account),
        "approved_phones_only": approved_phones_only(),
        "b2b_accounts": [{"name": a.name, "label": _account_label(a), "is_default": cint(a.is_default)}
                         for a in accounts],
        "b2b_default": party_accounts.pick_account(EMPLOYEE_ACCOUNT, employee, B2B_TYPES),
        "b2c_settings": shortcodes(values.company, "use_b2c"),
        "b2b_settings": shortcodes(values.company, "use_b2b"),
        "drafts": draft_payouts(dt, values.name),
    }


@frappe.whitelist()
def document_status(doctype: str, name: str) -> dict:
    """What the Expense Claim or Employee Advance form shows, in one call: the M-Pesa headline
    (paid, in flight, left), why it cannot be paid now, and for preparers what the pay buttons
    offer (the employee's approved destinations, masked, and the shortcodes)."""
    _check_doctype(doctype)
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    values = read_document(doctype, name)
    if not values:
        frappe.throw(f"{doctype} {name} not found")
    result = owed.employee_doc_left_to_pay(doctype, name, for_update=False)  # a display: no lock
    sent = [p for p in result.parts if p.kind in ("b2b", "b2c")]
    status = {
        "owed": result.owed,
        "left": result.left,
        "amount": whole_shillings(result.left),
        "paid": paid_by_mpesa(doctype, name),
        "in_flight": flt(sum(flt(p.amount) for p in sent if p.get("status") != "Paid"), 2),
        "unbooked": flt(sum(flt(p.amount) for p in sent if p.get("status") == "Paid"), 2),
        "drafts_planned": flt(sum(flt(p.amount) for p in result.parts if p.kind in ("draft", "planned")), 2),
        # a claim's merchants and casual payees not paid yet: theirs, not the employee's (claim_payees)
        "payees": flt(sum(flt(p.amount) for p in result.parts if p.kind == "payee"), 2),
        "parts": [p.label for p in result.parts],
        "problem": payable_problem(values, doctype),
        "can_prepare": can_prepare(),
    }
    if status["can_prepare"]:
        status.update(_pay_options(doctype, values))
    return status


# ---------------------------------------------------------------- preparing payouts (drafts only)
def _payable(doctype: str, name: str) -> tuple:
    """(values, whole shillings left): the document may be paid now and has something left (read
    under its lock, held until the draft is saved)."""
    values = read_document(doctype, name)
    problem = payable_problem(values, doctype)
    if problem:
        frappe.throw(problem)
    result = owed.employee_doc_left_to_pay(doctype, name, for_update=True)
    amount = whole_shillings(result.left)
    if amount < 1:
        frappe.throw(f"Nothing is left to pay by M-Pesa. {owed.explain(name, result)}")
    return values, amount


def _shortcode(company: str, flag: str, chosen: str | None = None) -> str:
    """The Mpesa Settings to pay from: the one picked, or the company's only one."""
    kind = "B2C" if flag == "use_b2c" else "B2B"
    options = shortcodes(company, flag)
    if chosen:
        if chosen not in options:
            frappe.throw(f"Mpesa Settings {chosen} does not pay {company} by {kind}.")
        return chosen
    if len(options) == 1:
        return options[0]
    if not options:
        frappe.throw(f"No Mpesa Settings of {company} pays by {kind} (Use {kind} and an initiator).")
    frappe.throw(f"Pick the shortcode to pay from: {', '.join(options)}.")


def _new_b2c_disbursement(company: str, settings: str, doctype: str, paid_to: str):
    """A draft B2C Payment Disbursement paying ``doctype`` documents from the shortcode's Utility
    account (as the payment schedule makes them)."""
    from ..wallet import accounts as wallet_accounts

    bank_account = wallet_accounts.bank_account(settings, wallet_accounts.UTILITY)
    paid_from = frappe.db.get_value("Bank Account", bank_account, "account") if bank_account else None
    if not paid_from:
        frappe.throw(f"Map a Bank Account on Mpesa Settings {settings} (Accounting Mapping).")
    doc = frappe.new_doc(DISB)
    doc.update({
        "naming_series": "MPESA-B2C-.YYYY.-",
        "company": company,
        "posting_date": nowdate(),
        "payment_type": "Mpesa Disbursement",
        "party_type": "Employee",
        "transaction_to_pay_against": doctype,
        "mode_of_payment": money_in_common.mode_of_payment_for(settings),
        "paid_from": paid_from,
        "paid_from_account_currency": "KES",
        # each row is booked on its own document's account (payment_entry_service)
        "paid_to": paid_to,
        "paid_to_account_currency": "KES",
        "paid_amount": 0,
        "source_exchange_rate": 1,
        "target_exchange_rate": 1,
    })
    return doc


def _b2c_row(doctype: str, values, amount: int, phone: str, account: str | None) -> dict:
    return {"reference_doctype": doctype, "reference_name": values.name, "party_type": "Employee",
            "party": values.employee, "allocated_amount": amount, "partyb": phone, "currency": "KES",
            "exchange_rate": 1, "payee_account_doctype": EMPLOYEE_ACCOUNT if account else None,
            "payee_account": account}


def _b2c_amount_problem(employee, amount) -> str | None:
    if amount > B2C_MAX_AMOUNT:
        return (f"KES {money(amount)} is more than M-Pesa B2C pays at once (KES {B2C_MAX_AMOUNT:,}): pay it to a bank, "
                "paybill or till account.")
    if amount < B2C_MIN_AMOUNT:
        return f"KES {money(amount)} is less than M-Pesa B2C pays (KES {B2C_MIN_AMOUNT})."
    return None


def _no_phone(employee) -> str:
    """Why b2c_phone found no phone: the approved phone that cannot be paid now first."""
    if party_accounts.accounts_of(EMPLOYEE_ACCOUNT, employee, (PHONE,)):
        return party_accounts.pick_problem(EMPLOYEE_ACCOUNT, employee, (PHONE,)) or (
            f"{employee} has several approved phones and none is the default: make one the default.")
    if approved_phones_only():
        return (f"{employee} has no approved phone that can be paid now. Add one under Employee > M-Pesa Accounts and "
                "have a manager approve it.")
    return f"{employee} has no approved phone and no Cell Number."


@frappe.whitelist(methods=["POST"])
def prepare_b2c(doctype: str, name: str, mpesa_settings: str | None = None) -> dict:
    """Pay employee (phone): a draft B2C Payment Disbursement of one row, what the document has
    left in whole shillings, to the employee's approved phone (or their Cell Number while M-Pesa
    Suite Settings allows it). Nothing is sent: an M-Pesa Payer sends it with Send via M-Pesa."""
    require(PREPARE_ROLES)
    _check_doctype(doctype)
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    frappe.has_permission(DISB, "create", throw=True)
    values, amount = _payable(doctype, name)
    drafts = draft_b2c(doctype, name, payees=False)  # a draft batch of the claim's payees is theirs
    if drafts:
        frappe.throw(f"Draft B2C Payment Disbursement {', '.join(drafts)} already pays {name}: open it, or delete it first.")
    settings = _shortcode(values.company, "use_b2c", mpesa_settings)
    phone, account = b2c_phone(values.employee)
    if not phone:
        frappe.throw(_no_phone(values.employee))
    problem = cell_number_problem(values.employee, phone) or _b2c_amount_problem(values.employee, amount)
    if problem:
        frappe.throw(problem)
    doc = _new_b2c_disbursement(values.company, settings, doctype, party_account(doctype, values))
    doc.paid_amount = amount
    doc.append("references", _b2c_row(doctype, values, amount, phone, account))
    doc.insert()
    return {"doctype": DISB, "name": doc.name, "amount": amount}


def _new_b2b(doctype: str, values, amount: int, settings: str, account: str | None = None):
    """A draft B2B request paying the document to one of its employee's approved bank, paybill or
    till accounts: the request's own checks run on insert (validate_request)."""
    account = account or party_accounts.pick_account(EMPLOYEE_ACCOUNT, values.employee, B2B_TYPES)
    if not account:
        frappe.throw(f"{values.employee} has no approved bank, paybill or till account that can be paid now, or several "
                     "and none is the default: pick one.")
    acc = party_accounts.get_account(EMPLOYEE_ACCOUNT, account)
    if not acc or acc.employee != values.employee or acc.account_type not in B2B_TYPES:
        frappe.throw(f"{account} is not a bank, paybill or till account of {values.employee}.")
    doc = frappe.new_doc(B2B)
    doc.update({
        "mpesa_settings": settings,
        "company": values.company,
        "employee": values.employee,
        "employee_mpesa_account": account,
        owed.EMPLOYEE_DOC_FIELDS[doctype]: values.name,
        "amount": amount,
        "remarks": f"{doctype} {values.name}"[:100],
        "entry_mode": AUTO,
    })
    doc.insert()
    return doc


@frappe.whitelist(methods=["POST"])
def prepare_b2b(doctype: str, name: str, employee_mpesa_account: str | None = None,
                mpesa_settings: str | None = None) -> dict:
    """Pay employee (bank / paybill / till): a draft B2B request for what the document has left in
    whole shillings, to the employee's approved account picked (the default when none is). Nothing
    is sent: an M-Pesa Payer sends it with Send via M-Pesa."""
    require(PREPARE_ROLES)
    _check_doctype(doctype)
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    frappe.has_permission(B2B, "create", throw=True)
    values, amount = _payable(doctype, name)
    settings = _shortcode(values.company, "use_b2b", mpesa_settings)
    doc = _new_b2b(doctype, values, amount, settings, employee_mpesa_account)
    return {"doctype": B2B, "name": doc.name, "amount": amount}


def _route(values) -> str:
    """How the list's Pay by M-Pesa pays the employee: "B2B" when their default approved account is
    a bank account, paybill or till, else "B2C" (an approved phone, or the Cell Number while
    M-Pesa Suite Settings allows it)."""
    default = party_accounts.default_account(EMPLOYEE_ACCOUNT, values.employee)
    acc = party_accounts.get_account(EMPLOYEE_ACCOUNT, default) if default else None
    return "B2B" if acc and acc.account_type in B2B_TYPES else "B2C"


@frappe.whitelist(methods=["POST"])
def prepare_bulk(doctype: str, names, b2c_settings: str | None = None, b2b_settings: str | None = None) -> dict:
    """Pay by M-Pesa on the list: ONE draft B2C Payment Disbursement with a row per ticked document
    whose employee is paid by phone, and a draft B2B request per document whose employee's default
    account is a bank account, paybill or till, each for what the document has left. Documents
    that cannot be paid are listed with the reason. Nothing is sent: each payout is sent on its own
    with Send via M-Pesa."""
    require(PREPARE_ROLES)
    _check_doctype(doctype)
    if isinstance(names, str):
        names = frappe.parse_json(names)
    # in name order: each document's lock is taken in turn and held to the end (as a disbursement's submit)
    names = sorted({cstr(n).strip() for n in names or [] if cstr(n).strip()})
    if not names:
        frappe.throw(f"Tick the {doctype}s to pay.")
    if len(names) > BULK_LIMIT:
        frappe.throw(f"Tick at most {BULK_LIMIT} at a time.")
    skipped, phone_rows, b2b_docs, company = [], [], [], None
    for name in names:
        if not frappe.has_permission(doctype, "read", doc=name):
            skipped.append({"name": name, "reason": "You cannot read it."})
            continue
        try:
            values, amount = _payable(doctype, name)
        except (frappe.ValidationError, frappe.PermissionError) as e:
            skipped.append({"name": name, "reason": _refusal(e)})
            continue
        company = company or values.company
        if values.company != company:
            skipped.append({"name": name, "reason": f"It is {values.company}'s: tick one company's at a time."})
        elif _route(values) == "B2B":
            b2b_docs.append((values, amount))
        else:
            phone, account = b2c_phone(values.employee)
            problem = (cell_number_problem(values.employee, phone) or _b2c_amount_problem(values.employee, amount)
                       if phone else _no_phone(values.employee))
            if problem:
                skipped.append({"name": name, "reason": problem})
            else:
                phone_rows.append((values, amount, phone, account))

    made_b2c = None
    if phone_rows:
        problem = _caught(frappe.has_permission, DISB, "create", throw=True) or _caught(
            _shortcode, company, "use_b2c", b2c_settings)
        if not problem:
            doc = _new_b2c_disbursement(company, _shortcode(company, "use_b2c", b2c_settings), doctype,
                                        party_account(doctype, phone_rows[0][0]))
            doc.paid_amount = sum(r[1] for r in phone_rows)
            for values, amount, phone, account in phone_rows:
                doc.append("references", _b2c_row(doctype, values, amount, phone, account))
            problem = _inserted(doc, "mpsa_bulk_b2c")
            made_b2c = None if problem else doc.name
        if problem:
            skipped += [{"name": r[0].name, "reason": problem} for r in phone_rows]
    made_b2b = []
    if b2b_docs:
        problem = _caught(frappe.has_permission, B2B, "create", throw=True) or _caught(
            _shortcode, company, "use_b2b", b2b_settings)
        for values, amount in b2b_docs:
            if problem:
                skipped.append({"name": values.name, "reason": problem})
                continue
            frappe.db.savepoint("mpsa_bulk_b2b")
            try:
                made_b2b.append(_new_b2b(doctype, values, amount, _shortcode(company, "use_b2b", b2b_settings)).name)
            except (frappe.ValidationError, frappe.PermissionError) as e:
                frappe.db.rollback(save_point="mpsa_bulk_b2b")
                skipped.append({"name": values.name, "reason": _refusal(e)})
    return {"b2c": made_b2c, "b2c_rows": len(phone_rows) if made_b2c else 0, "b2b": made_b2b, "skipped": skipped}


def _refusal(e) -> str:
    """What a check threw, as a plain message, without showing it to the user as an error."""
    from .schedule import _forget_message, _plain

    _forget_message()
    return _plain(e) or type(e).__name__


def _caught(fn, *args, **kwargs) -> str | None:
    try:
        fn(*args, **kwargs)
    except (frappe.ValidationError, frappe.PermissionError) as e:
        return _refusal(e)
    return None


def _inserted(doc, savepoint: str) -> str | None:
    """Insert ``doc``; why it was refused (everything it wrote undone), or None."""
    frappe.db.savepoint(savepoint)
    try:
        doc.insert()
    except (frappe.ValidationError, frappe.PermissionError) as e:
        frappe.db.rollback(save_point=savepoint)
        return _refusal(e)
    return None


# ---------------------------------------------------------------- the document's cancel
# Payouts whose submitted records name a claim or advance (a Link, or a Dynamic Link of the row or
# request): Frappe's back-link check would refuse the document's cancel for a failed one too
PAYOUT_LINKS = (B2B, DISB, DISB_ROW, B2C_REQUEST)


def paid_or_on_their_way(dt: str, dn: str) -> list:
    """M-Pesa payouts of the document that are Paid (also once cancelled: the money left) or still
    on their way: submitted B2B requests naming it (a draft is not: once the document is cancelled
    it can no longer be sent, payable_problem), B2C requests paying it, rows of a submitted
    disbursement not sent yet, and payment schedule lines sent for it."""
    found = []
    field = owed.EMPLOYEE_DOC_FIELDS[dt]
    if frappe.db.has_column(B2B, field):
        found += [r[0] for r in frappe.db.sql(
            f"""
            SELECT name FROM `tab{B2B}`
            WHERE `{field}` = %(dn)s
              AND ((docstatus = 1 AND COALESCE(status, '') IN %(live)s) OR (docstatus IN (1, 2) AND status = 'Paid'))
            ORDER BY creation
            """,
            {"dn": dn, "live": ("", *B2B_LIVE)},
        )]
    found += [r[0] for r in frappe.db.sql(
        f"""
        SELECT name FROM `tab{B2C_REQUEST}`
        WHERE reference_doctype = %(dt)s AND reference_name = %(dn)s
          AND ((docstatus = 1 AND COALESCE(status, '') IN %(live)s) OR (docstatus IN (1, 2) AND status = 'Paid'))
        ORDER BY creation
        """,
        {"dt": dt, "dn": dn, "live": ("", *B2C_LIVE)},
    )]
    # rows of a submitted disbursement whose payout is not made yet
    found += [r[0] for r in frappe.db.sql(
        f"""
        SELECT DISTINCT ref.parent FROM `tab{DISB_ROW}` ref
        JOIN `tab{DISB}` d ON d.name = ref.parent
        WHERE ref.reference_doctype = %(dt)s AND ref.reference_name = %(dn)s AND d.docstatus = 1
          AND COALESCE(ref.b2c_disbursement_request, '') = ''
          AND COALESCE(ref.payment_status, '') NOT IN ('Failed', 'Paid')
        """,
        {"dt": dt, "dn": dn},
    )]
    if frappe.db.table_exists(LINE):
        found += [f"{r[0]} row {r[1]}" for r in frappe.db.sql(
            f"""
            SELECT parent, idx FROM `tab{LINE}`
            WHERE reference_doctype = %(dt)s AND reference_name = %(dn)s AND status IN %(sent)s
            ORDER BY parent, idx
            """,
            {"dt": dt, "dn": dn, "sent": LINE_SENT},
        )]
    return list(dict.fromkeys(found))


def before_cancel(doc, method=None):
    """Expense Claim and Employee Advance before_cancel (hooks.py doc_events): refused while an
    M-Pesa payout of the document is paid or on its way. Payouts are found by the document's name,
    so its amended copy would show the whole amount as unpaid and could be paid again. A claim's
    payee lines read their payouts as claim_payees does: one a manager cancelled while it may still
    have paid (Unknown) holds the cancel too, until M-Pesa's answer settles it."""
    name = cstr(doc.get("name")).strip()
    if doc.get("doctype") not in DOCTYPES or not name:
        return
    paying = paid_or_on_their_way(doc.doctype, name)
    if doc.doctype == EXPENSE_CLAIM:
        from .claim_payees import lines_paid_or_on_their_way

        paying = list(dict.fromkeys(paying + lines_paid_or_on_their_way(name)))
    if paying:
        frappe.throw(f"{name} cannot be cancelled while M-Pesa payouts of it are paid or on their way: "
                     f"{', '.join(paying[:10])}. Its amended copy would show the whole amount as unpaid again.")


def ignore_payout_links(doc) -> None:
    """After before_cancel found no payout of the document paid or on its way, its failed payouts
    (and drafts) do not hold the cancel: Frappe's back-link check, which runs after on_cancel,
    would refuse it for any submitted B2B request, B2C disbursement or B2C request naming it, a
    failed one too, and the cancel and amend is how a wrong phone, till or account is corrected.
    HRMS's on_cancel sets the ledger DocTypes it ignores; these are added to them."""
    current = tuple(doc.get("ignore_linked_doctypes") or ())
    doc.ignore_linked_doctypes = current + tuple(d for d in PAYOUT_LINKS if d not in current)


def on_cancel(doc, method=None) -> None:
    """Employee Advance on_cancel (hooks.py doc_events, after HRMS's): ignore_payout_links. An
    Expense Claim's runs from money_out.claim_payees.on_cancel_claim."""
    ignore_payout_links(doc)
