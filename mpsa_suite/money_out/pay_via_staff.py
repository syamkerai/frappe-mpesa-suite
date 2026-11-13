"""Pay via Staff: a supplier's till paid through a staff member's own approved phone (round 4.5, strict).

The petty cash phone is the normal way to pay a till (money_out.petty_cash). Pay via Staff stays for
the few users M-Pesa Suite Settings lists under Who May Pay via Staff (``pay_via_staff_users``);
empty means nobody. The server refuses everyone else, at the advance and at the send:
- ``make_advance`` (Pay via Staff on a submitted Purchase Order or an unpaid Purchase Invoice): a
  submitted Employee Advance for the chosen staff member, its custom fields ``mpesa_purchase_order`` /
  ``mpesa_purchase_invoice`` and ``mpesa_supplier`` naming what it pays, and the draft B2C Payment
  Disbursement that pays it (the Employee Advance B2C path of money_out.employee_documents) to the
  staff member's approved Employee M-Pesa Account phone, never a typed number or a Cell Number. At
  most what the document still owes (owed.left_to_pay) less its other staff advances not settled.
  A listed user still never pays an advance to themselves (employee_accounts.own_payment_problem,
  and employee_documents.check_not_own_b2c at the send);
- the advance's payouts: a B2C row (every save and the send), the send itself and a retry are for
  listed users only and pay an approved phone of the employee only (``check_b2c_row``,
  ``check_send``, ``check_retry``); a B2B request never pays such an advance (``b2b_problem``); an
  Employee Advance carrying these fields is made by ``make_advance`` only (``validate_advance``).

Settlement (``settle_staff_advances``): when the supplier's Purchase Invoice is submitted (hooks.py,
``on_submit_purchase_invoice``), one Journal Entry: debit the supplier against the invoice, credit the
employee advance account against each Employee Advance made for the invoice or its orders, as HRMS's
own return entry does; at most what each advance was paid and not claimed or returned, in all at most
the invoice's outstanding. Settle Staff Advances (``settle_now``) does the same by hand. What an
advance has left after that (paid more than the invoice) is the HRMS Return or Claim's.

The daily "needs attention" alert (``attention_section``) lists the staff advances paid more than
``staff_advance_settle_days`` (default 3) ago and not settled.
"""

import frappe
from frappe.utils import cint, cstr, flt, getdate, now_datetime, nowdate

from ..daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT
from ..permissions import require_named_user
from . import owed

SUITE = "M-Pesa Suite Settings"
EA, PO, PI = "Employee Advance", "Purchase Order", "Purchase Invoice"
DOCUMENTS = (PO, PI)
JE = "Journal Entry"
DISB, ROW, REQUEST = "B2C Payment Disbursement", "B2C Payment Disbursement Reference", "B2C Disbursement Request"
FIELDS = ("mpesa_purchase_order", "mpesa_purchase_invoice", "mpesa_supplier")
DOC_FIELD = {PO: "mpesa_purchase_order", PI: "mpesa_purchase_invoice"}
# doc.flags key: the advance is made by make_advance
FLAG = "mpsa_pay_via_staff"
SETTLE_DAYS = 3
# who may settle by hand besides the listed users: it moves no money, it books the invoice
SETTLERS = ("Accounts Manager",)
SAVEPOINT = "mpesa_pay_via_staff_settle"


def money(value) -> str:
    return owed.money(value)


# ---------------------------------------------------------------- who may
def listed_users() -> list:
    """The users M-Pesa Suite Settings lists under Who May Pay via Staff ([] : nobody)."""
    try:
        rows = frappe.get_all("M-Pesa Pay via Staff User", filters={"parent": SUITE, "parenttype": SUITE},
                              pluck="user")
    except Exception:
        return []
    return sorted({cstr(u).strip() for u in rows if cstr(u).strip()})


def is_listed(user: str | None = None) -> bool:
    user = cstr(user or frappe.session.user).strip()
    return bool(user) and user not in ("Guest",) and user in listed_users()


def require_listed(action: str = "use Pay via Staff") -> None:
    """A named login listed under Who May Pay via Staff, else PermissionError."""
    require_named_user()
    if is_listed():
        return
    who = ", ".join(listed_users()) or "nobody is listed"
    frappe.throw(f"Only the users M-Pesa Suite Settings lists under Who May Pay via Staff may {action} ({who}).",
                 frappe.PermissionError)


def _require_settler() -> None:
    """Settle Staff Advances: the listed users and the Accounts Managers (it books, it pays nothing)."""
    require_named_user()
    if is_listed():
        return
    from ..permissions import require

    require(("Accounts Manager",))


# ---------------------------------------------------------------- the advances
def _ready() -> bool:
    try:
        return bool(frappe.get_meta(EA).has_field("mpesa_supplier"))
    except Exception:
        return False


def staff_advance(name) -> frappe._dict | None:
    """The Pay via Staff fields of an Employee Advance, or None for any other advance."""
    if not cstr(name).strip() or not _ready():
        return None
    values = frappe.db.get_value(EA, name, ["name", *FIELDS], as_dict=True)
    if not values or not any(cstr(values.get(f)).strip() for f in FIELDS):
        return None
    return values


def settled_amounts(names) -> dict:
    """{advance: what submitted Journal Entries credit against it}: the settlements, and HRMS's own
    return entries. HRMS writes them back as the advance's Return Amount (set_total_advance_paid);
    read here too, so a settlement is never made twice whatever HRMS's version writes back."""
    names = sorted({cstr(n).strip() for n in names or [] if cstr(n).strip()})
    if not names:
        return {}
    rows = frappe.db.sql(
        f"""SELECT jea.reference_name, COALESCE(SUM(jea.credit_in_account_currency), 0)
        FROM `tab{JE} Account` jea JOIN `tab{JE}` je ON je.name = jea.parent
        WHERE jea.reference_type = %s AND jea.reference_name IN %s AND je.docstatus = 1
        GROUP BY jea.reference_name""",
        (EA, tuple(names)))
    return {r[0]: flt(r[1], 2) for r in rows}


def _open(advance, settled=None) -> float:
    """What an advance was paid and has not claimed or returned (settled)."""
    returned = max(flt(advance.get("return_amount")), flt((settled or {}).get(advance.get("name"))))
    return flt(flt(advance.get("paid_amount")) - flt(advance.get("claimed_amount")) - returned, 2)


def _not_settled(advance, settled=None) -> float:
    """What a staff advance still stands for on its document: its amount, less what was claimed or
    returned or settled (an advance not paid yet still counts: it will be)."""
    returned = max(flt(advance.get("return_amount")), flt((settled or {}).get(advance.get("name"))))
    return max(flt(flt(advance.get("advance_amount")) - flt(advance.get("claimed_amount")) - returned, 2), 0.0)


ADVANCE_FIELDS = ("name", "employee", "employee_name", "company", "posting_date", "advance_amount", "paid_amount",
                  "claimed_amount", "return_amount", "advance_account", "docstatus", "creation", *FIELDS)


def advances_of(doctype: str, name: str) -> list:
    """The submitted staff advances made for the order or invoice (an invoice: and for its orders)."""
    if not _ready():
        return []
    rows = frappe.get_all(EA, filters={DOC_FIELD[doctype]: name, "docstatus": 1}, fields=list(ADVANCE_FIELDS))
    if doctype == PI:
        orders = _orders_of(name)
        if orders:
            listed = {r.name for r in rows}
            rows += [r for r in frappe.get_all(EA, filters={"mpesa_purchase_order": ["in", orders], "docstatus": 1},
                                               fields=list(ADVANCE_FIELDS))
                     if r.name not in listed and not cstr(r.mpesa_purchase_invoice).strip()]
    return sorted(rows, key=lambda r: (cstr(r.posting_date), cstr(r.creation), r.name))


def _orders_of(invoice: str) -> list:
    return sorted({cstr(o).strip() for o in frappe.get_all("Purchase Invoice Item", filters={"parent": invoice},
                                                           pluck="purchase_order") if cstr(o).strip()})


def validate_advance(doc, method=None) -> None:
    """Employee Advance validate (hooks.py): one carrying the Pay via Staff fields is made by
    make_advance only, and they never change."""
    if not _ready():
        return
    flags = getattr(doc, "flags", None) or {}
    if flags.get(FLAG):
        return
    if doc.is_new():
        if any(cstr(doc.get(f)).strip() for f in FIELDS):
            frappe.throw("An Employee Advance for a supplier is made with Pay via Staff on the Purchase Order or "
                         "Purchase Invoice, by the users M-Pesa Suite Settings lists.", frappe.PermissionError)
        return
    before = doc.get_doc_before_save() if callable(getattr(doc, "get_doc_before_save", None)) else None
    if before and any(cstr(before.get(f)) != cstr(doc.get(f)) for f in FIELDS):
        frappe.throw("The supplier, order and invoice of a Pay via Staff advance never change.", frappe.PermissionError)


# ---------------------------------------------------------------- the staff member's approved phone
def staff_phone(employee: str) -> tuple:
    """(phone, account) of the employee's approved Employee M-Pesa Account phone that can be paid now:
    the default (or only) one. Raises when there is none: Pay via Staff never pays a typed number or
    a Cell Number nobody approved."""
    from . import employee_documents as ed
    from . import party_accounts as pa

    if not pa.accounts_of(pa.EMPLOYEE_ACCOUNT, employee, (pa.PHONE,)):
        frappe.throw(f"{employee} has no approved phone (Employee > M-Pesa Accounts, approved by a manager): Pay via "
                     "Staff pays an approved phone only.")
    account = pa.pick_account(pa.EMPLOYEE_ACCOUNT, employee, (pa.PHONE,))
    if not account:
        frappe.throw(ed._no_phone(employee))
    acc = pa.get_account(pa.EMPLOYEE_ACCOUNT, account)
    problem = pa.usable_problem(acc, employee, pa.EMPLOYEE_ACCOUNT)
    if problem:
        frappe.throw(problem)
    return cstr(acc.phone).strip(), acc.name


def _row_account_problem(employee, account, phone) -> str | None:
    """Why a payout of a staff advance may not go to ``phone``: it must be the approved phone the row
    names, of the advance's employee, usable now."""
    from . import party_accounts as pa
    from ..utils.phone import normalize_msisdn

    if not account:
        return (f"A Pay via Staff advance is paid only to {employee}'s approved phone (Employee M-Pesa Account), "
                f"not to {cstr(phone).strip() or 'a number'} nobody approved.")
    acc = pa.get_account(pa.EMPLOYEE_ACCOUNT, account)
    problem = pa.usable_problem(acc, employee, pa.EMPLOYEE_ACCOUNT)
    if problem:
        return problem
    if acc.account_type != pa.PHONE or normalize_msisdn(acc.phone) != normalize_msisdn(phone):
        return f"{cstr(phone).strip() or 'No number'} is not the phone of {acc.name}, {employee}'s approved phone."
    return None


def check_b2c_row(row, advance: str, account) -> None:
    """employee_documents.check_b2c_row, for a row paying an Employee Advance (on every save and at the
    send): a staff advance's row is made and sent by a listed user only, to the employee's approved
    phone only (``account``: the approved account check_b2c_row found for the row's number)."""
    if not staff_advance(advance):
        return
    require_listed("pay a Pay via Staff advance")
    employee = frappe.db.get_value(EA, advance, "employee")
    problem = _row_account_problem(employee, account, getattr(row, "partyb", None))
    if problem:
        frappe.throw(f"Row #{row.idx}: {problem}")


def check_send(doc) -> None:
    """B2C Payment Disbursement before_submit (employee_documents.check_not_own_b2c): a row paying a
    staff advance is sent by a listed user only."""
    for row in doc.get("references") or []:
        if getattr(row, "reference_doctype", None) == EA and staff_advance(getattr(row, "reference_name", None)):
            require_listed("send a Pay via Staff advance")
            return


def check_retry(doc) -> None:
    """A failed payout of a staff advance sent again (employee_documents.check_b2c_retry): by a listed
    user, to the approved phone its row named, still usable."""
    if getattr(doc, "reference_doctype", None) != EA or not staff_advance(getattr(doc, "reference_name", None)):
        return
    require_listed("send a Pay via Staff advance again")
    ref = cstr(getattr(doc, "b2c_payment_disbursement_reference", None)).strip()
    account = frappe.db.get_value(ROW, ref, "payee_account") if ref else None
    employee = frappe.db.get_value(EA, doc.reference_name, "employee")
    problem = _row_account_problem(employee, account, getattr(doc, "phone_number", None))
    if problem:
        frappe.throw(problem)


def b2b_problem(dt: str, dn: str) -> str | None:
    """A B2B request never pays a staff advance: it is paid to the staff member's approved phone."""
    if dt == EA and staff_advance(dn):
        return (f"{dn} is a Pay via Staff advance: it is paid by B2C to the staff member's approved phone only, never "
                "by a B2B request.")
    return None


def form_problem(advance: str) -> str | None:
    """What the Employee Advance's form says to who may not pay a staff advance (employee_documents.
    document_status), or None."""
    values = staff_advance(advance)
    if not values or is_listed():
        return None
    what = values.mpesa_purchase_invoice or values.mpesa_purchase_order or values.mpesa_supplier
    return (f"{advance} is a Pay via Staff advance (for {what}): only the users M-Pesa Suite Settings lists under Who "
            "May Pay via Staff pay it.")


# ---------------------------------------------------------------- Pay via Staff on the order or invoice
def _check_document(doctype: str) -> None:
    if doctype not in DOCUMENTS:
        frappe.throw("Pick a Purchase Order or a Purchase Invoice.")


def _left(doctype: str, name: str, for_update: bool = False) -> tuple:
    """(what the document still owes, less its staff advances not settled; the advances)."""
    result = owed.left_to_pay(doctype, name, for_update=for_update)
    advances = advances_of(doctype, name)
    settled = settled_amounts(a.name for a in advances)
    taken = flt(sum(_not_settled(a, settled) for a in advances), 2)
    return max(flt(result.left - taken, 2), 0.0), advances, result


def _advance_account(company: str) -> str:
    account = frappe.db.get_value("Company", company, "default_employee_advance_account")
    if not account:
        frappe.throw(f"Set the Default Employee Advance Account on Company {company} (HR settings of the company).")
    return account


@frappe.whitelist(methods=["POST"])
def make_advance(doctype: str, name: str, employee: str, amount=None, purpose: str | None = None,
                 mpesa_settings: str | None = None) -> dict:
    """Pay via Staff: a submitted Employee Advance for ``employee`` to pay the order's or invoice's
    supplier, and the draft B2C Payment Disbursement that pays it to their approved phone. For the
    listed users only; nothing is sent (Send via M-Pesa on the disbursement, a listed user again)."""
    require_listed("make a Pay via Staff advance")
    _check_document(doctype)
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    from . import employee_accounts
    from . import employee_documents as ed

    with owed.doc_lock(doctype, name):
        values = owed.read_document(doctype, name)  # the locking read of the row comes last (_left: R4)
        if not values:
            frappe.throw(f"{doctype} {name} not found.")
        problems = owed.document_problems(doctype, values)
        if problems:
            frappe.throw(problems[0])
        staff = frappe.db.get_value("Employee", employee, ["name", "company", "status", "employee_name"], as_dict=True)
        if not staff:
            frappe.throw(f"Employee {employee} not found.")
        if staff.company != values.company:
            frappe.throw(f"{employee} is an employee of {staff.company}, not {values.company}.")
        if staff.status and staff.status != "Active":
            frappe.throw(f"{employee} is {staff.status}.")
        own = employee_accounts.own_payment_problem(employee)
        if own:
            frappe.throw(f"You may not pay an advance to yourself: {own}", frappe.PermissionError)
        phone, account = staff_phone(employee)
        left, _advances, result = _left(doctype, name, for_update=True)
        amount = flt(amount, 2) if flt(amount) else int(left + 0.001)
        if amount != int(amount):
            frappe.throw(f"M-Pesa B2C pays whole shillings only: KES {money(amount)}.")
        if amount < B2C_MIN_AMOUNT or amount > B2C_MAX_AMOUNT:
            frappe.throw(f"M-Pesa B2C pays KES {B2C_MIN_AMOUNT} to {B2C_MAX_AMOUNT:,} at once: KES {money(amount)}.")
        if amount > left + 0.005:
            frappe.throw(f"KES {money(amount)} is more than {name} has left for a staff advance (KES {money(left)}). "
                         f"{owed.explain(name, result)}")
        settings = ed._shortcode(values.company, "use_b2c", mpesa_settings)
        supplier_name = cstr(frappe.db.get_value("Supplier", values.supplier, "supplier_name") or values.supplier)
        advance = frappe.new_doc(EA)
        advance.update({
            "employee": employee, "company": values.company, "posting_date": nowdate(),
            "purpose": (cstr(purpose).strip() or f"Pay {supplier_name} for {doctype} {name} (Pay via Staff)")[:140],
            "advance_amount": int(amount), "exchange_rate": 1, "advance_account": _advance_account(values.company),
            "mpesa_supplier": values.supplier, DOC_FIELD[doctype]: name,
        })
        if advance.meta.has_field("currency"):
            advance.currency = "KES"
        advance.flags[FLAG] = True
        advance.flags.ignore_permissions = True  # the listed user's gate is this one; HRMS's roles are HR's
        advance.insert()
        advance.submit()
        adv_values = ed.read_document(EA, advance.name)
        problem = ed.payable_problem(adv_values, EA)
        if problem:
            frappe.throw(problem)
        disb = ed._new_b2c_disbursement(values.company, settings, EA, ed.party_account(EA, adv_values))
        disb.paid_amount = int(amount)
        disb.append("references", ed._b2c_row(EA, adv_values, int(amount), phone, account))
        disb.flags.ignore_permissions = True
        disb.insert()
        advance.add_comment("Comment", f"Pay via Staff by {frappe.session.user}: KES {money(amount)} to {employee} "
                                       f"({staff.employee_name}) to pay {supplier_name} for {doctype} {name}; paid by "
                                       f"{disb.name}.")
    return {"employee_advance": advance.name, "disbursement": disb.name, "amount": int(amount)}


@frappe.whitelist()
def document_state(doctype: str, name: str) -> dict:
    """What a Purchase Order or Invoice shows of Pay via Staff: whether this user may use it (the
    button shows to the listed users only), what is left for an advance, the staff advances made for
    it, and (an invoice) whether Settle Staff Advances has something to settle."""
    _check_document(doctype)
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    values = owed.read_document(doctype, name)
    if not values:
        frappe.throw(f"{doctype} {name} not found.")
    listed = is_listed()
    out = {"allowed": False, "left": 0, "advances": [], "can_settle": False, "to_settle": 0}
    advances = advances_of(doctype, name)
    settled = settled_amounts(a.name for a in advances)
    out["advances"] = [{"name": a.name, "employee": a.employee, "employee_name": a.employee_name,
                        "amount": flt(a.advance_amount, 2), "paid": flt(a.paid_amount, 2), "open": _open(a, settled),
                        "posting_date": cstr(a.posting_date)} for a in advances]
    if listed and not owed.document_problems(doctype, values):
        left, _advances, _result = _left(doctype, name)
        out.update(allowed=left >= B2C_MIN_AMOUNT, left=int(left + 0.001))
    if doctype == PI and cint(values.docstatus) == 1:
        to_settle = flt(min(sum(max(_open(a, settled), 0) for a in advances), flt(values.outstanding_amount)), 2)
        settler = listed or not set(SETTLERS).isdisjoint(frappe.get_roles())
        out.update(to_settle=to_settle, can_settle=bool(to_settle > owed.MIN and settler))
    return out


# ---------------------------------------------------------------- settlement against the supplier's invoice
INVOICE_FIELDS = ("name", "docstatus", "supplier", "company", "credit_to", "outstanding_amount", "is_return",
                  "currency", "cost_center")


def settle_staff_advances(invoice: str) -> dict:
    """One Journal Entry settling the invoice's staff advances: debit the supplier against the
    invoice, credit each advance's account against it (HRMS counts it returned to the advance),
    each at most what it has open, in all at most the invoice's outstanding. Under the invoice's
    lock. {journal_entry (None when nothing is settled), settled: [(advance, amount)], left}."""
    with owed.doc_lock(PI, invoice):
        values = frappe.db.get_value(PI, invoice, list(INVOICE_FIELDS), as_dict=True, for_update=True)
        if not values or cint(values.docstatus) != 1 or cint(values.is_return):
            return {"journal_entry": None, "settled": [], "left": 0}
        left = flt(values.outstanding_amount, 2)
        lines = []
        advances = advances_of(PI, invoice)
        settled = settled_amounts(a.name for a in advances)
        for advance in advances:
            if advance.mpesa_supplier != values.supplier or advance.company != values.company:
                continue
            take = flt(min(_open(advance, settled), left), 2)
            if take <= owed.MIN:
                continue
            lines.append((advance, take))
            left = flt(left - take, 2)
        if not lines:
            return {"journal_entry": None, "settled": [], "left": left}
        je = build_settle_entry(values, lines)
        from ..accounting.posting import insert_entry

        insert_entry(je, True, ignore_permissions=True)  # posted at once (accounting.posting)
        total = flt(sum(t for _a, t in lines), 2)
        text = (f"Pay via Staff settled: {je.name} books KES {money(total)} the staff paid {values.supplier} for "
                f"{invoice} "
                f"({', '.join(f'{a.name} KES {money(t)}' for a, t in lines)}).")
        frappe.get_doc(PI, invoice).add_comment("Comment", text)
        for advance, take in lines:
            frappe.get_doc(EA, advance.name).add_comment("Comment", text)
    return {"journal_entry": je.name, "settled": [(a.name, t) for a, t in lines], "left": left}


def build_settle_entry(values, lines):
    """The settlement's Journal Entry (``values``: the invoice; ``lines``: (advance, amount))."""
    from .common import payout_cost_center

    total = flt(sum(t for _a, t in lines), 2)
    cost_center = values.get("cost_center") or payout_cost_center(None, values.company, PI, values.name)
    remark = (f"Pay via Staff: {', '.join(a.name for a, _t in lines)} paid {values.supplier} for {values.name}; the "
              "advances are settled against the invoice.")
    je = frappe.new_doc(JE)
    je.voucher_type = "Journal Entry"
    je.company = values.company
    je.posting_date = nowdate()
    je.user_remark = remark
    if je.meta.has_field("custom_remark"):  # v16 builds the remark itself unless told
        je.custom_remark = 1
        je.remark = remark
    je.append("accounts", {"account": values.credit_to, "party_type": "Supplier", "party": values.supplier,
                           "debit_in_account_currency": total, "reference_type": PI, "reference_name": values.name,
                           "cost_center": cost_center, "user_remark": remark})
    for advance, take in lines:
        je.append("accounts", {"account": advance.advance_account, "party_type": "Employee", "party": advance.employee,
                               "credit_in_account_currency": take, "reference_type": EA, "reference_name": advance.name,
                               "is_advance": "Yes", "cost_center": cost_center, "user_remark": remark})
    return je


def on_submit_purchase_invoice(doc, method=None) -> None:
    """Purchase Invoice on_submit (hooks.py): its staff advances settled at once. Under a savepoint:
    an error is rolled back, logged and alerted, and never stops the invoice (Settle Staff Advances
    on the invoice, or the daily alert, takes it up)."""
    if not _ready() or cint(doc.get("is_return")):
        return
    orders = sorted({cstr(r.get("purchase_order")).strip() for r in doc.get("items") or []
                     if cstr(r.get("purchase_order")).strip()})
    filters = [{"mpesa_purchase_invoice": doc.name, "docstatus": 1}]
    if orders:
        filters.append({"mpesa_purchase_order": ["in", orders], "docstatus": 1})
    if not any(frappe.db.exists(EA, f) for f in filters):
        return
    frappe.db.savepoint(SAVEPOINT)
    try:
        settle_staff_advances(doc.name)
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        raise
    except Exception:
        frappe.db.rollback(save_point=SAVEPOINT)
        frappe.log_error(title="M-Pesa Pay via Staff advances not settled", message=f"{doc.name}\n"
                                                                                    f"{frappe.get_traceback()}")
        from .party_accounts import alert

        alert(f"{doc.name}'s staff advances (Pay via Staff) could not be settled: press Settle Staff Advances on it.",
              frappe._dict(doctype=PI, name=doc.name))


@frappe.whitelist(methods=["POST"])
def settle_now(purchase_invoice: str) -> dict:
    """Settle Staff Advances on a submitted Purchase Invoice: settle_staff_advances, by hand."""
    _require_settler()
    frappe.has_permission(PI, "read", doc=purchase_invoice, throw=True)
    out = settle_staff_advances(purchase_invoice)
    if not out["journal_entry"]:
        frappe.throw(f"{purchase_invoice} has no staff advance to settle, or nothing left to pay.")
    return out


# ---------------------------------------------------------------- the daily "needs attention" section
def _paid_on(advance: str):
    """When M-Pesa first paid the advance (its Paid B2C payouts), else None."""
    times = frappe.get_all(REQUEST, filters={"reference_doctype": EA, "reference_name": advance, "status": "Paid"},
                           pluck="transaction_completed_datetime")
    times = [t for t in times if t]
    return min(times) if times else None


def attention_section():
    """money_out.attention: staff advances paid more than staff_advance_settle_days (default 3) ago and
    not settled (paid, less claimed and returned). Linked to the Employee Advance list of them."""
    if not _ready():
        return None
    try:
        days = cint(frappe.db.get_single_value(SUITE, "staff_advance_settle_days")) or SETTLE_DAYS
    except Exception:
        days = SETTLE_DAYS
    now = getdate(now_datetime())
    lines = []
    advances = frappe.get_all(EA, filters={"docstatus": 1, "mpesa_supplier": ["is", "set"]},
                              fields=list(ADVANCE_FIELDS))
    settled = settled_amounts(a.name for a in advances)
    for advance in advances:
        left = _open(advance, settled)
        if left <= owed.MIN:
            continue
        paid = _paid_on(advance.name) or advance.posting_date
        age = (now - getdate(paid)).days if paid else None
        if age is None or age < days:
            continue
        what = advance.mpesa_purchase_invoice or advance.mpesa_purchase_order
        lines.append(f"{advance.name}: {advance.employee_name or advance.employee} was paid KES "
                     f"{money(advance.paid_amount)} {age} days ago to pay {advance.mpesa_supplier}"
                     f"{f' ({what})' if what else ''}; KES {money(left)} is not settled")
    route = "/app/employee-advance?mpesa_supplier=%5B%22is%22%2C%22set%22%5D"
    return "Staff advances for suppliers (Pay via Staff)", lines, route
