"""What a Purchase Invoice or Purchase Order still needs paying, with every payout in flight.

ERPNext lowers an invoice's outstanding (and raises an order's advance_paid) only when a
submitted entry is allocated to it. Until then the money is counted here:
- B2B requests in flight, and Paid ones without a submitted entry on the document;
- B2C payouts: the same two lists B2C's own guard uses;
- draft Payment and Journal Entries staff made for the document;
- for an invoice, advances still sitting on the orders it was made from (ERPNext offers them
  under Get Advances, so they are taken off, not refused);
- lines of an M-Pesa Payment Schedule planned for the document and not yet sent (a sent line
  counts as its B2B request).

A Salary Slip (HRMS) has its own reading: its rounded total less what was paid (Paid B2C and B2B
salary payouts and bank entries: salary_slip_paid), less payouts on their way and planned lines.
No M-Pesa payment of a slip passes while its Payroll Entry has a bank or cash entry not made by
M-Pesa, draft or submitted (HRMS's Make Bank Entry pays every slip of the payroll, and its entry
may name no employee or still be a draft, so what it paid cannot be told per slip): slip_problems,
checked on the schedule form, at Pay Selected, at a B2B salary's submit and send, and by B2C's
guards and the job and send of a scheduled B2C payout.

An Expense Claim or Employee Advance (HRMS) reads like an invoice (employee_doc_left_to_pay):
HRMS's own outstanding (a claim's grand_total less what was reimbursed, an advance's
advance_amount less what was paid) less every payout of it on its way, whoever it pays: B2C rows
and requests naming it, B2B requests with its expense_claim or employee_advance, draft entries
and planned lines.

B2B and B2C share the per-document lock "b2c-pay:<doctype>:<name>", so a B2B request and a B2C
disbursement for one invoice cannot both pass. Lock order (R4): the document lock, the payout
rows, then the document row last.
"""

import contextlib

import frappe
from frappe.utils import cint, cstr, flt, getdate, nowdate

B2B = "Mpesa B2B Payment Request"
SCHEDULE_LINE = "M-Pesa Payment Schedule Line"
DOC_FIELDS = {"Purchase Invoice": "purchase_invoice", "Purchase Order": "purchase_order"}
# An employee request may pay one of these instead of a Salary Slip (HRMS custom fields, migrate)
EMPLOYEE_DOC_FIELDS = {"Expense Claim": "expense_claim", "Employee Advance": "employee_advance"}
# Every document a B2B request names by a field of its own
B2B_DOC_FIELDS = {**DOC_FIELDS, **EMPLOYEE_DOC_FIELDS}
SLIP = "Salary Slip"
# Requests that may still take money from M-Pesa: their whole amount counts.
OPEN = ("Not Initiated", "Initiated", "Unknown")
# An order in one of these states takes no more payments.
ORDER_CLOSED = ("On Hold", "Closed", "Completed")
# Amounts at or below this are rounding, not money.
MIN = 0.005
DOC_READ = {
    "Purchase Invoice": (
        "name", "docstatus", "supplier", "company", "currency", "party_account_currency", "is_return",
        "on_hold", "outstanding_amount", "credit_to", "cost_center", "bill_no", "payment_terms_template",
    ),
    "Purchase Order": (
        "name", "docstatus", "supplier", "company", "currency", "party_account_currency", "status",
        "per_billed", "rounded_total", "grand_total", "advance_paid", "cost_center", "payment_terms_template",
    ),
}


def money(value) -> str:
    """2,000 or 2,000.50: how amounts read in messages."""
    value = flt(value, 2)
    return f"{value:,.0f}" if abs(value - round(value)) < MIN else f"{value:,.2f}"


def _locking(for_update: bool) -> str:
    return "FOR UPDATE" if for_update and frappe.db.db_type == "mariadb" else ""


# ---------------------------------------------------------------- the document
def linked_document(doc) -> tuple:
    """(doctype, name) of the invoice or order a request pays, or (None, None)."""
    pi = cstr(doc.get("purchase_invoice")).strip()
    po = cstr(doc.get("purchase_order")).strip()
    if pi and po:
        frappe.throw("Link an invoice or an order, not both.")
    if pi:
        return "Purchase Invoice", pi
    if po:
        return "Purchase Order", po
    return None, None


def document_of(values) -> tuple:
    """Like linked_document, without throwing (for rows already saved)."""
    if cstr(values.get("purchase_invoice")).strip():
        return "Purchase Invoice", values.purchase_invoice
    if cstr(values.get("purchase_order")).strip():
        return "Purchase Order", values.purchase_order
    return None, None


def employee_document_of(values) -> tuple:
    """(doctype, name) of the Expense Claim or Employee Advance a request pays, or (None, None),
    without throwing (for rows already saved)."""
    for dt, field in EMPLOYEE_DOC_FIELDS.items():
        dn = cstr(values.get(field)).strip()
        if dn:
            return dt, dn
    return None, None


def linked_employee_document(doc) -> tuple:
    """Like employee_document_of, refusing a request that names both."""
    found = [(dt, cstr(doc.get(field)).strip()) for dt, field in EMPLOYEE_DOC_FIELDS.items()
             if cstr(doc.get(field)).strip()]
    if len(found) > 1:
        frappe.throw("Link an Expense Claim or an Employee Advance, not both.")
    return found[0] if found else (None, None)


def doc_lock(dt: str, dn: str):
    """The per-document lock B2C's guard takes too, held until commit. A B2B submit holds it
    through the call to M-Pesa, so a second payer waits and may be told to try again."""
    from ..money_in.common import named_lock

    return named_lock(f"b2c-pay:{dt}:{dn}", busy=busy_message(dt, dn))


def busy_message(dt: str, dn: str) -> str:
    """What a second payer (or booking job) is told when the document's lock stays taken."""
    return f"Another payment for {dt} {dn} is being sent or booked. Try again in a minute."


def read_document(dt: str, dn: str, for_update: bool = False):
    meta = frappe.get_meta(dt)
    fields = [f for f in DOC_READ[dt] if f in ("name", "docstatus") or meta.has_field(f)]
    return frappe.db.get_value(dt, dn, fields, as_dict=True, for_update=for_update)


def document_problems(dt: str, values) -> list:
    """Why the document cannot be paid by M-Pesa now, as plain messages."""
    if not values:
        return [f"{dt} not found"]
    name = values.name
    problems = []
    if cint(values.docstatus) != 1:
        problems.append(f"{name} is not submitted.")
    currency = values.get("currency") or "KES"
    party_currency = values.get("party_account_currency") or currency
    if currency != "KES" or party_currency != "KES":
        problems.append(f"{name} is in {currency if currency != 'KES' else party_currency}. M-Pesa pays KES only.")
    if dt == "Purchase Invoice":
        if cint(values.get("is_return")):
            problems.append(f"{name} is a debit note. It cannot be paid.")
        if cint(values.get("on_hold")):
            # ERPNext refuses an invoice on hold in a Payment Entry, whatever its release date (F2).
            problems.append(f"{name} is on hold. Release the hold before paying.")
    else:
        if values.get("status") in ORDER_CLOSED:
            problems.append(f"{name} is {values.status}.")
        if flt(values.get("per_billed"), 2) > 0:
            problems.append(billed_message(name))
    return problems


def billed_message(po: str) -> str:
    return f"{po} already has invoices: pay the Purchase Invoice instead."


def billed_order(po: str, for_update: bool = False) -> str | None:
    """Why B2C may not pay this order: it already has invoices. Paying an invoice does not
    change its order's advance_paid, so the order would still look unpaid (B2B refuses it in
    document_problems)."""
    per_billed = frappe.db.get_value("Purchase Order", po, "per_billed", for_update=for_update)
    return billed_message(po) if flt(per_billed, 2) > 0 else None


def supplier_hold(supplier) -> str | None:
    """Why the supplier cannot be paid now ('ACME is on hold for payments until 30 Oct 2026.'), or None."""
    if not supplier:
        return None
    values = frappe.db.get_value("Supplier", supplier, ["on_hold", "hold_type", "release_date"], as_dict=True)
    if not values or not cint(values.on_hold):
        return None
    check = getattr(frappe.get_doc("Supplier", supplier), "is_blocked_for", None)
    if callable(check):  # ERPNext 15.123+ and 16 (F9)
        blocked = bool(check("Payments"))
    else:
        blocked = values.hold_type in ("All", "Payments") and (
            not values.release_date or getdate(nowdate()) <= getdate(values.release_date)
        )
    if not blocked:
        return None
    until = ""
    if values.release_date:
        day = getdate(values.release_date)
        until = f" until {day.day} {day:%b %Y}"
    return f"{supplier} is on hold for payments{until}."


# ---------------------------------------------------------------- payouts that count
def allocated_on(dt: str, dn: str, receipt: str, for_update: bool = False) -> float:
    """What submitted entries carrying ``receipt`` allocate to the document (R8). An order's
    advance that Get Advances moved onto an invoice keeps the order as its advance voucher."""
    if not receipt:
        return 0.0
    values = {"dt": dt, "dn": dn, "receipt": receipt}
    on_document = "per.reference_doctype = %(dt)s AND per.reference_name = %(dn)s"
    if frappe.db.has_column("Payment Entry Reference", "advance_voucher_no"):
        on_document = f"(({on_document}) OR (per.advance_voucher_type = %(dt)s AND per.advance_voucher_no = %(dn)s))"
    locking = _locking(for_update)
    rows = frappe.db.sql(
        f"""
        SELECT per.allocated_amount
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent
        WHERE pe.docstatus = 1 AND pe.reference_no = %(receipt)s AND {on_document}
        {locking}
        """,
        values,
    )
    rows += frappe.db.sql(
        f"""
        SELECT jea.debit_in_account_currency - jea.credit_in_account_currency
        FROM `tabJournal Entry Account` jea
        JOIN `tabJournal Entry` je ON je.name = jea.parent
        WHERE je.docstatus = 1 AND je.cheque_no = %(receipt)s
          AND jea.reference_type = %(dt)s AND jea.reference_name = %(dn)s
        {locking}
        """,
        values,
    )
    return flt(sum(flt(r[0]) for r in rows), 2)


def b2b_open(dt: str, dn: str, exclude: str | None = None, for_update: bool = False) -> list:
    """B2B requests that have not yet lowered what the document owes.

    In flight (drafts too): the whole amount. Paid, also on a cancelled request (the money
    left): what no submitted entry carrying its receipt allocates to the document. Same shape
    as B2C's open payouts: parent, allocated_amount, request_status (and receipt).
    """
    field = B2B_DOC_FIELDS[dt]
    if dt in EMPLOYEE_DOC_FIELDS and not frappe.db.has_column(B2B, field):
        return []  # before the migrate makes the HRMS link
    rows = frappe.db.sql(
        f"""
        SELECT name, docstatus, status, amount, transaction_id
        FROM `tab{B2B}`
        WHERE `{field}` = %(dn)s AND name != %(exclude)s
          AND ((docstatus < 2 AND COALESCE(status, '') IN %(open)s)
            OR (docstatus IN (1, 2) AND status = 'Paid'))
        ORDER BY creation
        {_locking(for_update)}
        """,
        {"dn": dn, "exclude": exclude or "", "open": ("", *OPEN)},
        as_dict=True,
    )
    found = []
    for row in rows:
        receipt = cstr(row.transaction_id).strip()
        amount = flt(row.amount, 2)
        if row.status == "Paid":
            amount = flt(amount - allocated_on(dt, dn, receipt, for_update), 2)
            if amount <= MIN:
                continue
        found.append(frappe._dict(parent=row.name, allocated_amount=amount,
                                  request_status=row.status or "Not Initiated", receipt=receipt))
    return found


def b2c_open(dt: str, dn: str) -> list:
    """B2C payouts for the document, as B2C's own guard counts them (both are locking reads)."""
    from ..mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request import paid_without_entry
    from ..mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement import open_payouts

    return [*open_payouts(dt, dn), *paid_without_entry(dt, dn)]


def b2c_receipts(dt: str, dn: str) -> set:
    """Receipts of Paid B2C payouts for the document: their draft entries are already counted."""
    rows = frappe.db.sql(
        """
        SELECT transaction_id FROM `tabB2C Disbursement Request`
        WHERE reference_doctype = %s AND reference_name = %s AND docstatus IN (1, 2) AND status = 'Paid'
        """,
        (dt, dn),
    )
    return {cstr(r[0]).strip().upper() for r in rows if cstr(r[0]).strip()}


def draft_entries(dt: str, dn: str, skip_receipts=(), for_update: bool = False) -> list:
    """Draft entries allocated to the document. One carrying a receipt counted already is skipped."""
    values = {"dt": dt, "dn": dn}
    locking = _locking(for_update)
    rows = [
        ("Payment Entry", *r)
        for r in frappe.db.sql(
            f"""
            SELECT pe.name, pe.reference_no, per.allocated_amount
            FROM `tabPayment Entry Reference` per
            JOIN `tabPayment Entry` pe ON pe.name = per.parent
            WHERE pe.docstatus = 0 AND per.reference_doctype = %(dt)s AND per.reference_name = %(dn)s
            {locking}
            """,
            values,
        )
    ]
    rows += [
        ("Journal Entry", *r)
        for r in frappe.db.sql(
            f"""
            SELECT je.name, je.cheque_no, jea.debit_in_account_currency - jea.credit_in_account_currency
            FROM `tabJournal Entry Account` jea
            JOIN `tabJournal Entry` je ON je.name = jea.parent
            WHERE je.docstatus = 0 AND jea.reference_type = %(dt)s AND jea.reference_name = %(dn)s
            {locking}
            """,
            values,
        )
    ]
    skip = {cstr(r).strip().upper() for r in skip_receipts if cstr(r).strip()}
    totals = {}
    for doctype, name, reference, amount in rows:
        if cstr(reference).strip().upper() in skip:
            continue
        totals[(doctype, name)] = totals.get((doctype, name), 0.0) + flt(amount)
    return [
        frappe._dict(doctype=doctype, name=name, amount=flt(amount, 2))
        for (doctype, name), amount in sorted(totals.items())
        if flt(amount, 2) > MIN
    ]


def order_advance(po: str, for_update: bool = False, exclude_request: str | None = None) -> float:
    """The order's advance not yet moved onto an invoice (F4): submitted entries still referencing
    the order, plus its B2B and B2C payouts not yet booked. ``exclude_request``: the B2B request
    being checked, which must not count against itself (a draft moved from the order to its
    invoice, or Make Entry booking an order's payout on the invoice made from it)."""
    locking = _locking(for_update)
    entries = frappe.db.sql(
        f"""
        SELECT per.allocated_amount
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent
        WHERE pe.docstatus = 1 AND per.reference_doctype = 'Purchase Order' AND per.reference_name = %s
        {locking}
        """,
        (po,),
    )
    entries += frappe.db.sql(
        f"""
        SELECT jea.debit_in_account_currency - jea.credit_in_account_currency
        FROM `tabJournal Entry Account` jea
        JOIN `tabJournal Entry` je ON je.name = jea.parent
        WHERE je.docstatus = 1 AND jea.reference_type = 'Purchase Order' AND jea.reference_name = %s
        {locking}
        """,
        (po,),
    )
    total = sum(flt(r[0]) for r in entries)
    total += sum(flt(r.allocated_amount)
                 for r in b2b_open("Purchase Order", po, exclude=exclude_request, for_update=for_update))
    total += sum(flt(r.allocated_amount) for r in b2c_open("Purchase Order", po))
    return flt(total, 2)


def invoice_orders(pi: str) -> list:
    """The orders an invoice's items were made from."""
    rows = frappe.db.sql(
        """
        SELECT purchase_order FROM `tabPurchase Invoice Item`
        WHERE parent = %s AND parenttype = 'Purchase Invoice' AND COALESCE(purchase_order, '') != ''
        """,
        (pi,),
    )
    return sorted({cstr(r[0]).strip() for r in rows} - {""})


def order_advances(pi: str, for_update: bool = False, exclude_request: str | None = None) -> list:
    """For each order on the invoice: its advance not yet moved onto an invoice. Get Advances on
    the invoice moves it over; until then the invoice's outstanding does not show it."""
    parts = []
    for po in invoice_orders(pi):
        total = order_advance(po, for_update, exclude_request)
        if total > MIN:
            parts.append(
                frappe._dict(
                    kind="advance", name=po, amount=total,
                    label=f"advance still on {po} {money(total)} (allocate it with Get Advances first)",
                )
            )
    return parts


def planned_lines(dt: str, dn: str, exclude_line=None, exclude_schedule=None, for_update: bool = False) -> list:
    """Payment schedule lines planned for the document and not sent yet. ``exclude_line``: the
    line being checked; ``exclude_schedule``: a schedule being saved, whose lines are counted
    from the form instead."""
    if not frappe.db.table_exists(SCHEDULE_LINE):
        return []  # before migrate
    rows = frappe.db.sql(
        f"""
        SELECT name, parent, amount, planned_date
        FROM `tab{SCHEDULE_LINE}`
        WHERE reference_doctype = %(dt)s AND reference_name = %(dn)s AND parenttype = 'M-Pesa Payment Schedule'
          AND status = 'Planned' AND COALESCE(request, '') = '' AND COALESCE(b2c_disbursement, '') = ''
          AND name != %(line)s AND parent != %(schedule)s
        ORDER BY planned_date, parent, idx
        {_locking(for_update)}
        """,
        {"dt": dt, "dn": dn, "line": exclude_line or "", "schedule": exclude_schedule or ""},
        as_dict=True,
    )
    found = []
    for row in rows:
        amount = flt(row.amount, 2)
        if amount <= MIN:
            continue
        day = getdate(row.planned_date)
        found.append(frappe._dict(kind="planned", name=row.parent, line=row.name, amount=amount, planned_date=day,
                                  label=f"planned on {row.parent} for {day.day} {day:%b} {money(amount)}"))
    return found


def outstanding(dt: str, dn: str, for_update: bool = False) -> float:
    """What the document itself says it owes (B2C's reading of it)."""
    from ..mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement import current_outstanding

    return flt(current_outstanding(dt, dn, for_update=for_update), 2)


def left_to_pay(dt: str, dn: str, *, exclude_request=None, exclude_line=None, exclude_schedule=None,
                for_update: bool = False):
    """_dict(owed, parts, left): what the document owes, what is already on its way, and the rest.

    ``exclude_request``: the B2B request being checked. ``exclude_line`` / ``exclude_schedule``:
    the payment schedule line being checked, or the schedule being saved (planned_lines).
    Payouts and planned lines are read before the document row (R4). A Salary Slip has its own
    reading (slip_left_to_pay).
    """
    if dt == SLIP:
        return slip_left_to_pay(dn, exclude_request=exclude_request, exclude_line=exclude_line,
                                exclude_schedule=exclude_schedule, for_update=for_update)
    parts = []
    b2b = b2b_open(dt, dn, exclude=exclude_request, for_update=for_update)
    for row in b2b:
        parts.append(frappe._dict(kind="b2b", name=row.parent, amount=row.allocated_amount, status=row.request_status,
                                  label=f"{row.parent} {row.request_status} {money(row.allocated_amount)}"))
    for row in b2c_open(dt, dn):
        status = row.get("request_status") or row.get("payment_status") or "in progress"
        parts.append(frappe._dict(kind="b2c", name=row.parent, amount=flt(row.allocated_amount, 2), status=status,
                                  label=f"B2C {row.parent} {status} {money(row.allocated_amount)}"))
    skip = {r.receipt for r in b2b if r.receipt} | b2c_receipts(dt, dn)
    for entry in draft_entries(dt, dn, skip, for_update):
        parts.append(frappe._dict(kind="draft", name=entry.name, amount=entry.amount,
                                  label=f"draft {entry.doctype} {entry.name} {money(entry.amount)}"))
    parts += planned_lines(dt, dn, exclude_line, exclude_schedule, for_update)
    if dt == "Purchase Invoice":
        parts += order_advances(dn, for_update, exclude_request)
    owed = outstanding(dt, dn, for_update)  # last: the document row (R4)
    left = max(0.0, flt(owed - sum(flt(p.amount) for p in parts), 2))
    return frappe._dict(owed=owed, parts=parts, left=left)


def employee_doc_left_to_pay(dt: str, dn: str, *, exclude_request=None, exclude_line=None, exclude_schedule=None,
                             for_update: bool = False):
    """left_to_pay of an Expense Claim or Employee Advance: HRMS's outstanding (utils.payouts.owed)
    less every payout of it on its way, whoever it pays (a merchant or casual payee of a claim
    too): B2C rows and requests naming it, B2B requests with its link, draft entries and planned
    lines. ``for_update``: under the document's lock, the one B2C, B2B and the schedule take,
    payouts first and the document row last (R4)."""
    if dt not in EMPLOYEE_DOC_FIELDS:
        frappe.throw(f"{dt} is not an Expense Claim or Employee Advance.")
    with doc_lock(dt, dn) if for_update else contextlib.nullcontext():
        return left_to_pay(dt, dn, exclude_request=exclude_request, exclude_line=exclude_line,
                           exclude_schedule=exclude_schedule, for_update=for_update)


def summary(result) -> dict:
    """The sums the request form's headline shows."""
    def total(*kinds):
        return flt(sum(flt(p.amount) for p in result.parts if p.kind in kinds), 2)

    return {
        "outstanding": result.owed,
        "in_progress": total("b2b", "b2c"),
        "drafts": total("draft"),
        "planned": total("planned"),
        "order_advance": total("advance"),
        "left": result.left,
    }


def explain(dn: str, result) -> str:
    """The breakdown for messages: 'PINV-1 owes 10,000, less MB2B-1 Initiated 2,000: 8,000 is left to pay.'"""
    less = "; ".join(p.label for p in result.parts)
    text = f"{dn} owes {money(result.owed)}"
    if less:
        text += f", less {less}"
    return f"{text}: {money(result.left)} is left to pay."


# ---------------------------------------------------------------- Salary Slips (HRMS)
SLIP_READ = ("name", "docstatus", "status", "employee", "employee_name", "company", "currency", "payroll_entry",
             "net_pay", "rounded_total", "start_date", "end_date")


def hrms_installed() -> bool:
    return "hrms" in frappe.get_installed_apps()


def payroll_bank_entries(payroll_entry: str) -> list:
    """Live (draft or submitted) Bank or Cash Journal Entries for the Payroll Entry not made by an
    M-Pesa payout (HRMS's Make Bank Entry, or one made by hand): an M-Pesa salary as well could
    pay twice. M-Pesa's own salary entries are tagged with their payout. While a salary schedule is
    built (payroll._bank_entries_once), each Payroll Entry is read once."""
    if not payroll_entry:
        return []
    once = getattr(frappe.local, "mpesa_bank_entries", None)
    if once is not None and payroll_entry in once:
        return list(once[payroll_entry])
    meta = frappe.get_meta("Journal Entry Account")
    tags = [f for f in ("b2c_payment_disbursement", "mpesa_b2b_payment_request") if meta.has_field(f)]
    mpesa = " OR ".join(f"COALESCE(t.{f}, '') != ''" for f in tags)
    not_mpesa = (f"AND NOT EXISTS (SELECT 1 FROM `tabJournal Entry Account` t WHERE t.parent = je.name AND ({mpesa}))"
                 if tags else "")
    rows = frappe.db.sql(
        f"""
        SELECT DISTINCT je.name FROM `tabJournal Entry` je
        JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
        WHERE je.docstatus < 2 AND je.voucher_type IN ('Bank Entry', 'Cash Entry')
          AND jea.reference_type = 'Payroll Entry' AND jea.reference_name = %s
          {not_mpesa}
        ORDER BY je.name
        """,
        (payroll_entry,),
    )
    found = [r[0] for r in rows]
    if once is not None:
        once[payroll_entry] = found
    return list(found)


def bank_entry_problem(payroll_entry: str) -> str | None:
    """Why no salary of the Payroll Entry is paid by M-Pesa now: it has a bank or cash entry."""
    entries = payroll_bank_entries(payroll_entry)
    if not entries:
        return None
    return (f"{payroll_entry} has a bank or cash entry not made by M-Pesa ({', '.join(entries[:5])}): its salaries may "
            "be paid by bank already. Cancel that entry first, or pay these salaries outside M-Pesa.")


def slip_lock(slip: str):
    """The Salary Slip's per-document lock: the key B2C's guards take for it too."""
    return doc_lock(SLIP, slip)


def read_slip(slip: str, for_update: bool = False):
    meta = frappe.get_meta(SLIP)
    fields = [f for f in SLIP_READ if f in ("name", "docstatus") or meta.has_field(f)]
    return frappe.db.get_value(SLIP, slip, fields, as_dict=True, for_update=for_update)


def slip_problems(values) -> list:
    """Why the Salary Slip's salary cannot be paid by M-Pesa now, as plain messages."""
    if not values:
        return ["Salary Slip not found"]
    name = values.name
    problems = []
    if cint(values.docstatus) != 1:
        problems.append(f"{name} is not submitted.")
    if values.get("status") == "Withheld":
        problems.append(f"{name} is withheld (Salary Withholding): its salary is not paid now.")
    currency = values.get("currency") or "KES"
    if currency != "KES":
        problems.append(f"{name} is in {currency}. M-Pesa pays KES only.")
    if not values.get("payroll_entry"):
        problems.append(f"{name} is not part of a Payroll Entry, so it has no payroll payable to clear.")
    else:
        bank = bank_entry_problem(values.payroll_entry)
        if bank:
            problems.append(bank)
    return problems


def slip_bank_problem(slip: str) -> str | None:
    """bank_entry_problem for the slip's Payroll Entry (B2C's guards ask by slip)."""
    return bank_entry_problem(frappe.db.get_value(SLIP, slip, "payroll_entry"))


def b2b_slip_open(slip: str, exclude: str | None = None, for_update: bool = False) -> list:
    """B2B salary requests for the slip still on their way (drafts too). A Paid one already
    lowers what the slip owes (salary_slip_paid counts it)."""
    if not frappe.db.has_column(B2B, "salary_slip"):
        return []  # a site without HRMS
    rows = frappe.db.sql(
        f"""
        SELECT name, status, amount
        FROM `tab{B2B}`
        WHERE salary_slip = %(slip)s AND name != %(exclude)s
          AND docstatus < 2 AND COALESCE(status, '') IN %(open)s
        ORDER BY creation
        {_locking(for_update)}
        """,
        {"slip": slip, "exclude": exclude or "", "open": ("", *OPEN)},
        as_dict=True,
    )
    return [frappe._dict(parent=r.name, allocated_amount=flt(r.amount, 2), request_status=r.status or "Not Initiated")
            for r in rows]


def b2c_slip_open(slip: str) -> list:
    """B2C payouts for the slip still on their way. A Paid one (booked or not) already lowers what
    the slip owes (salary_slip_paid counts every Paid payout)."""
    from ..mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement import open_payouts

    return [r for r in open_payouts(SLIP, slip) if (r.get("request_status") or "") != "Paid"]


def slip_left_to_pay(slip: str, *, exclude_request=None, exclude_line=None, exclude_schedule=None,
                     for_update: bool = False):
    """_dict(owed, parts, left) for a Salary Slip: what it owes (its rounded total less what was
    paid: Paid B2C and B2B payouts and bank entries), less payouts on their way and lines planned
    on a payment schedule. Payouts first, the slip row last (R4)."""
    parts = []
    for row in b2b_slip_open(slip, exclude_request, for_update):
        parts.append(frappe._dict(kind="b2b", name=row.parent, amount=row.allocated_amount,
                                  label=f"{row.parent} {row.request_status} {money(row.allocated_amount)}"))
    for row in b2c_slip_open(slip):
        status = row.get("request_status") or row.get("payment_status") or "in progress"
        parts.append(frappe._dict(kind="b2c", name=row.parent, amount=flt(row.allocated_amount, 2),
                                  label=f"B2C {row.parent} {status} {money(row.allocated_amount)}"))
    parts += planned_lines(SLIP, slip, exclude_line, exclude_schedule, for_update)
    owed = outstanding(SLIP, slip, for_update)  # last: the slip row (R4)
    left = max(0.0, flt(owed - sum(flt(p.amount) for p in parts), 2))
    return frappe._dict(owed=owed, parts=parts, left=left)


# ---------------------------------------------------------------- money the supplier may already have
def earlier_payouts(supplier: str) -> list:
    """M-Pesa money the supplier already got that is not on any document yet: Paid B2B requests
    with no invoice or order whose entry is not made or is an unallocated advance, and submitted
    Payment Entries with unallocated money that carry a B2B receipt."""
    if not supplier:
        return []
    found, seen = [], set()
    for row in frappe.db.sql(
        f"""
        SELECT r.name, r.amount, r.transaction_id, r.trans_completed_time, r.entry_status,
               pe.unallocated_amount AS unallocated
        FROM `tab{B2B}` r
        LEFT JOIN `tabPayment Entry` pe ON pe.name = r.payment_entry AND pe.docstatus = 1
        WHERE r.supplier = %(supplier)s AND r.status = 'Paid' AND r.docstatus IN (1, 2)
          AND COALESCE(r.purchase_invoice, '') = '' AND COALESCE(r.purchase_order, '') = ''
          AND (COALESCE(r.entry_status, '') != 'Entry Made' OR COALESCE(pe.unallocated_amount, 0) > 0.005)
        ORDER BY r.creation
        """,
        {"supplier": supplier},
        as_dict=True,
    ):
        receipt = cstr(row.transaction_id).strip()
        seen.add(receipt.upper())
        when = getdate(row.trans_completed_time) if row.trans_completed_time else None
        state = row.entry_status or "no entry"
        if row.entry_status == "Entry Made" and flt(row.unallocated) > MIN:
            state = f"KES {money(row.unallocated)} unallocated"
        found.append(frappe._dict(
            doctype=B2B, name=row.name, receipt=receipt, amount=flt(row.amount, 2), date=when,
            label=f"{row.name}: KES {money(row.amount)} paid{f' on {when}' if when else ''}, receipt {receipt} ({state})",
        ))
    for row in frappe.db.sql(
        f"""
        SELECT pe.name, pe.reference_no, pe.unallocated_amount, pe.posting_date
        FROM `tabPayment Entry` pe
        WHERE pe.party_type = 'Supplier' AND pe.party = %(supplier)s AND pe.docstatus = 1
          AND pe.payment_type = 'Pay' AND pe.unallocated_amount > 0.005
          AND pe.reference_no IN (
            SELECT r.transaction_id FROM `tab{B2B}` r
            WHERE r.status = 'Paid' AND COALESCE(r.transaction_id, '') != '')
        ORDER BY pe.posting_date, pe.name
        """,
        {"supplier": supplier},
        as_dict=True,
    ):
        if cstr(row.reference_no).strip().upper() in seen:
            continue
        found.append(frappe._dict(
            doctype="Payment Entry", name=row.name, receipt=row.reference_no, amount=flt(row.unallocated_amount, 2),
            date=row.posting_date,
            label=f"Payment Entry {row.name}: KES {money(row.unallocated_amount)} not allocated, "
            f"M-Pesa receipt {row.reference_no}",
        ))
    return found
