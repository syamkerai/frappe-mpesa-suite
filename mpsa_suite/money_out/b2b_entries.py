"""Accounting entries for Paid B2B requests: booked automatically, or made later by staff.

When M-Pesa confirms a payment, the booking job makes its Payment Entry: against the linked
Purchase Invoice or Purchase Order, or as an advance to the supplier. "Make the entry later"
books nothing; staff press Make Entry once they know what the money paid for. Either way:
- one live entry per receipt, looked for under the receipt's lock before anything is made (R5);
- the entry is posted on the payout's own Bank Account GL, on the M-Pesa completion date, with
  the receipt as its reference and the payout amount without the charge (the charge keeps its
  CHG-<receipt> Journal Entry), so it reconciles with the payout's statement line (R6);
- only a submitted entry allocated to the document lowers what the document owes (R8);
- the automatic job books as Administrator; Make Entry runs with the user's own permissions (R9).
The request's Accounting Entry section says where each payout stands. A 15-minute sweep keeps
it current, retries failed bookings for 30 days and reminds notify_role once. Requests Paid
before this release are only classified, never booked automatically.
"""

import contextlib
import re

import frappe
from frappe.utils import add_days, cint, cstr, flt, getdate, now_datetime, nowdate

from ..money_in import common as money_in_common
from ..permissions import ADMIN_ROLES, FINANCE_ROLES, require
from . import owed, statement

B2B = statement.B2B
PE, JE = "Payment Entry", "Journal Entry"
AUTO, LATER = "Book automatically", "Make the entry later"
NOT_MADE, DRAFT, CHECK, MADE = "Entry Not Made", "Draft Entry", "Needs Checking", "Entry Made"
KINDS = ("Purchase Invoice", "Purchase Order", "Supplier Advance", "Journal Entry")
# Failed automatic bookings are tried again this long; older ones are left to Make Entry.
RETRY_WINDOW_DAYS = 30
MIN = owed.MIN
money = owed.money


# ---------------------------------------------------------------- reading
def payout_account(name: str) -> tuple:
    """(Bank Account, GL, date) of the payout: the very account and date its statement line uses."""
    p = statement.load(B2B, name)
    if not p:
        return None, None, getdate(nowdate())
    bank_account = statement.payout_bank_account(p)
    gl = frappe.db.get_value("Bank Account", bank_account, "account") if bank_account else None
    return bank_account, gl, p.date


def receipt_entries(receipt, for_update: bool = False) -> list:
    """Live Payment Entries (Pay, receipt as Reference No) and Journal Entries (receipt as Cheque
    No) carrying the receipt, with what they credit and what they are allocated to."""
    receipt = cstr(receipt).strip()
    if not receipt:
        return []
    locking = owed._locking(for_update)
    advance = frappe.db.has_column("Payment Entry Reference", "advance_voucher_no")
    entries = []
    for pe in frappe.db.sql(
        f"""
        SELECT name, docstatus, party_type, party, paid_from, paid_amount, unallocated_amount
        FROM `tabPayment Entry`
        WHERE reference_no = %s AND payment_type = 'Pay' AND docstatus < 2
        ORDER BY creation
        {locking}
        """,
        (receipt,),
        as_dict=True,
    ):
        rows = frappe.db.sql(
            f"""
            SELECT reference_doctype, reference_name, allocated_amount
                {", advance_voucher_type, advance_voucher_no" if advance else ""}
            FROM `tabPayment Entry Reference` WHERE parent = %s ORDER BY idx
            """,
            (pe.name,),
            as_dict=True,
        )
        entries.append(frappe._dict(
            doctype=PE, name=pe.name, docstatus=cint(pe.docstatus), party_type=pe.party_type, party=pe.party,
            account=pe.paid_from, credit=flt(pe.paid_amount, 2), credits={pe.paid_from: flt(pe.paid_amount, 2)},
            unallocated=flt(pe.unallocated_amount, 2),
            allocations=[r for r in rows if r.reference_name and flt(r.allocated_amount) > MIN],
        ))
    for je in frappe.db.sql(
        f"""
        SELECT name, docstatus FROM `tabJournal Entry`
        WHERE cheque_no = %s AND docstatus < 2
        ORDER BY creation
        {locking}
        """,
        (receipt,),
        as_dict=True,
    ):
        rows = frappe.db.sql(
            """
            SELECT account, party_type, party, reference_type, reference_name,
                debit_in_account_currency AS debit, credit_in_account_currency AS credit
            FROM `tabJournal Entry Account` WHERE parent = %s ORDER BY idx
            """,
            (je.name,),
            as_dict=True,
        )
        credits = {}
        for r in rows:
            if flt(r.credit) - flt(r.debit) > MIN:
                credits[r.account] = flt(credits.get(r.account, 0) + flt(r.credit) - flt(r.debit), 2)
        account = max(credits, key=credits.get) if credits else None
        allocations = [
            frappe._dict(reference_doctype=r.reference_type, reference_name=r.reference_name,
                         allocated_amount=flt(flt(r.debit) - flt(r.credit), 2))
            for r in rows if r.reference_name and flt(r.debit) - flt(r.credit) > MIN
        ]
        supplier = next((r.party for r in rows if r.party_type == "Supplier" and r.party), None)
        credit = credits.get(account, 0.0) if account else 0.0
        entries.append(frappe._dict(
            doctype=JE, name=je.name, docstatus=cint(je.docstatus), party_type="Supplier" if supplier else None,
            party=supplier, account=account, credit=credit, credits=credits, allocations=allocations,
            unallocated=max(0.0, flt(credit - sum(a.allocated_amount for a in allocations), 2)),
        ))
    return entries


def _on_target(doc, entry, dt, dn) -> bool:
    if dt:
        return any(
            (a.reference_doctype, a.reference_name) == (dt, dn)
            or (a.get("advance_voucher_type"), a.get("advance_voucher_no")) == (dt, dn)
            for a in entry.allocations
        )
    if entry.doctype == PE:
        return entry.party_type == "Supplier" and bool(doc.supplier) and entry.party == doc.supplier
    return True  # a Journal Entry for a payout with no document (an expense, a transfer)


def _where(entry) -> str:
    on = sorted({a.reference_name for a in entry.allocations})
    where = f"on {', '.join(on)}" if on else "not allocated"
    return f"a draft, {where}" if entry.docstatus == 0 else where


def classify(doc, entries, gl=None) -> tuple:
    """(state, entry, note) for the request's Accounting Entry. ``note`` None: keep the reason
    already on the request (no entry yet)."""
    if doc.status != "Paid":
        return "", None, ""
    receipt = cstr(doc.transaction_id).strip()
    dt, dn = owed.document_of(doc)
    if not entries:
        if cint(doc.docstatus) == 2:
            what = dn or doc.supplier or "the supplier"
            return CHECK, None, (
                f"M-Pesa paid this after the request was cancelled. Check the statement and that {what} "
                "was not paid again, then make the entry (managers only)."
            )
        return NOT_MADE, None, None
    if len(entries) > 1:
        listed = ", ".join(f"{e.doctype} {e.name}" for e in entries)
        return CHECK, None, f"Receipt {receipt} is on {len(entries)} entries ({listed}). Keep one and cancel the others."
    entry = entries[0]
    label = f"{entry.doctype} {entry.name}"
    if not _on_target(doc, entry, dt, dn):
        target = dn or doc.supplier or "the right supplier"
        return CHECK, entry, f"Receipt {receipt} is on {label} ({_where(entry)}): allocate it to {target} or correct it."
    if entry.docstatus == 0:
        return DRAFT, entry, f"Submit {label}."
    amount = flt(doc.amount, 2)
    if gl and abs(flt(entry.credits.get(gl, 0)) - amount) > MIN:
        return CHECK, entry, (
            f"{label} pays from {entry.account or '-'} for {money(entry.credit)}; the M-Pesa line is "
            f"{money(amount)} on {gl}, so it will not reconcile."
        )
    if entry.unallocated > MIN:
        if dt and owed.outstanding(dt, dn) > MIN:
            return CHECK, entry, (
                f"KES {money(entry.unallocated)} is not allocated: allocate it to {dn} "
                "(Get Advances or Payment Reconciliation)."
            )
        if entry.doctype == PE:
            return MADE, entry, f"KES {money(entry.unallocated)} is an unallocated supplier advance ({label})."
    return MADE, entry, f"Booked on {label}."


def set_entry(doc, state: str, note, entry=None, log: bool = True) -> bool:
    """Write the Accounting Entry state; True when the state changed. A note that needs checking
    goes to the Error Log once, when it changes."""
    values = {"entry_status": state, "entry_note": note or None}
    if entry:
        values["payment_entry" if entry.doctype == PE else "journal_entry"] = entry.name
    changed = {k: v for k, v in values.items() if (doc.get(k) or None) != (v or None)}
    if not changed:
        return False
    if log and state == CHECK and "entry_note" in changed:
        frappe.log_error(title="M-Pesa B2B entry needs checking", message=f"{doc.name}: {note}",
                         reference_doctype=B2B, reference_name=doc.name)
    doc.db_set(changed, update_modified=False)
    return "entry_status" in changed


def _default_note(doc) -> str:
    if (doc.get("entry_mode") or AUTO) == LATER:
        return f"Paid on {_paid_on(doc)}. Press Make Entry when you know what this paid for."
    return f"No accounting entry carries receipt {doc.transaction_id}. Press Make Entry."


def _paid_on(doc):
    return getdate(doc.trans_completed_time) if doc.get("trans_completed_time") else getdate(doc.creation or nowdate())


def refresh_entry(doc, entries=None, feed: bool = True, log: bool = True) -> str:
    """Look again at the entries carrying the request's receipt and record what they show."""
    for field, doctype in (("payment_entry", PE), ("journal_entry", JE)):
        linked = doc.get(field)
        if linked and frappe.db.get_value(doctype, linked, "docstatus") not in (0, 1):
            doc.db_set(field, None, update_modified=False)
            doc.add_comment("Comment", f"{doctype} {linked} was cancelled.")
    if entries is None:
        entries = receipt_entries(doc.transaction_id)
    _, gl, _ = payout_account(doc.name)
    before = doc.get("entry_status")
    state, entry, note = classify(doc, entries, gl)
    if note is None:
        note = doc.get("entry_note") if before == NOT_MADE and doc.get("entry_note") else _default_note(doc)
    set_entry(doc, state, note, entry, log=log)
    if feed and state != before:
        statement.after_payout(B2B, doc.name)
    return state


@contextlib.contextmanager
def _locks(name: str, dt, dn, receipt):
    """R4 order: the document's lock, the receipt's lock, then the request's row; reloaded."""
    with contextlib.ExitStack() as stack:
        if dt and dn:
            stack.enter_context(owed.doc_lock(dt, dn))
        if receipt:
            stack.enter_context(money_in_common.named_lock(f"book:{receipt}"))
        frappe.db.get_value(B2B, name, "name", for_update=True)
        # a locking read: the plain one would see this transaction's snapshot, taken before the lock
        yield frappe.get_doc(B2B, name, for_update=True)


def _peek(name: str):
    """The request's document and receipt, read without a lock (to know which locks to take)."""
    values = frappe.db.get_value(B2B, name, ["purchase_invoice", "purchase_order", "transaction_id"], as_dict=True)
    if not values:
        frappe.throw(f"{B2B} {name} not found")
    dt, dn = owed.document_of(values)
    return dt, dn, cstr(values.transaction_id).strip()


def _alert_once(doc, message: str) -> None:
    if doc.get("entry_alerted_on"):
        return
    from .supplier_accounts import _alert

    _alert(message, doc)
    doc.db_set("entry_alerted_on", now_datetime(), update_modified=False)


# ---------------------------------------------------------------- automatic booking
def book_automatically(name: str) -> str | None:
    """Book the Payment Entry of a Paid request (the Result's job and the sweep). Idempotent:
    an entry already carrying the receipt is only looked at. Returns the entry, or None."""
    dt, dn, receipt = _peek(name)
    with _locks(name, dt, dn, receipt) as doc:
        if doc.status != "Paid" or not receipt or cstr(doc.transaction_id).strip() != receipt:
            return None
        doc.db_set("entry_attempted_on", now_datetime(), update_modified=False)
        entries = receipt_entries(receipt, for_update=True)
        if entries:
            refresh_entry(doc, entries, feed=False)
            return doc.get("payment_entry") or doc.get("journal_entry")
        if cint(doc.docstatus) == 2:
            # A Result after the request was cancelled: the money left, but nobody chose to pay it now.
            state, _, note = classify(doc, [])
            set_entry(doc, state, note)
            _alert_once(doc, f"{doc.name}: {note}")
            return None
        reason = _not_automatic(doc)
        if reason:
            set_entry(doc, NOT_MADE, reason)
            return None
        return _book(doc, dt, dn)


def _not_automatic(doc) -> str | None:
    if (doc.get("entry_mode") or AUTO) == LATER:
        return f"Paid on {_paid_on(doc)}. Press Make Entry when you know what this paid for."
    if not doc.supplier:
        return "Paid to a number not saved on a supplier: press Make Entry."
    if owed.supplier_hold(doc.supplier):
        return (
            f"{doc.supplier} is on hold for payments in ERPNext. Release the hold; the entry is tried again "
            "every 15 minutes."
        )
    return None


def _book(doc, dt, dn) -> str | None:
    savepoint = "mpesa_b2b_pe"
    frappe.db.savepoint(savepoint)
    try:
        with money_in_common.as_user("Administrator"):
            pe, problem = build_payment_entry(doc, dt, dn, doc.supplier, flt(doc.amount))
            pe.flags.ignore_permissions = True
            pe.insert(ignore_permissions=True)
            if cint(frappe.db.get_value("Mpesa Settings", doc.mpesa_settings, "submit_b2c_accounting_entries")):
                pe.submit()
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        traceback = frappe.get_traceback()
        last = (traceback.strip().splitlines() or ["Unknown error"])[-1]
        last = re.sub(r"^[\w.]*(Error|Exception)\w*: ", "", last).strip()[:300] or "Unknown error"
        note = (
            f"Could not book automatically: {last}. It is tried again every 15 minutes for {RETRY_WINDOW_DAYS} "
            "days, or press Make Entry. Do not make a Payment Entry for it by hand."
        )
        if doc.get("entry_note") != note:  # the sweep retries every run; log each new reason once
            frappe.log_error(title="M-Pesa B2B booking failed", message=f"{doc.name}\n{traceback}",
                             reference_doctype=B2B, reference_name=doc.name)
        set_entry(doc, NOT_MADE, note)
        _alert_once(doc, f"{doc.name} was paid but could not be booked automatically: {last}")
        return None
    refresh_entry(doc, receipt_entries(doc.transaction_id), feed=False, log=not problem)
    if problem:
        # The document took the money elsewhere since: a possible double payment.
        set_entry(doc, CHECK, problem)
        doc.add_comment("Comment", problem)
        _alert_once(doc, f"{doc.name}: {problem}")
    return pe.name


def remark_text(doc, dn=None, extra=None) -> str:
    text = f"M-Pesa B2B {doc.command_id} to {doc.receiver_shortcode} ({doc.name}), receipt {doc.transaction_id}"
    if dn:
        text += f", for {dn}"
    if extra:
        text += f". {cstr(extra).strip()}"
    return text


def _settings_value(settings: str, field: str):
    """A value of an Accounting Mapping custom field of Mpesa Settings (None before migrate)."""
    if not settings or not frappe.get_meta("Mpesa Settings").has_field(field):
        return None
    return frappe.db.get_value("Mpesa Settings", settings, field)


def _company(doc, bank_account=None) -> str:
    return (
        doc.company
        or (bank_account and frappe.db.get_value("Bank Account", bank_account, "company"))
        or _settings_value(doc.mpesa_settings, "company")
    )


def _require_kes(account: str) -> None:
    currency = frappe.db.get_value("Account", account, "account_currency")
    if currency and currency != "KES":
        frappe.throw(f"Account {account} is in {currency}. M-Pesa pays KES only.")


def build_payment_entry(doc, dt, dn, supplier: str, amount: float, cap=None, remark=None) -> tuple:
    """(Payment Entry, problem): built by hand, so the Mode of Payment's account never replaces
    the payout's GL (F5). ``problem``: why the document could not take the money."""
    import erpnext
    from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
    from erpnext.accounts.party import get_party_account

    bank_account, gl, date = payout_account(doc.name)
    if not gl:
        frappe.throw(f"Map a Bank Account on Mpesa Settings {doc.mpesa_settings} (Accounting Mapping)")
    company = _company(doc, bank_account)
    values = owed.read_document(dt, dn) if dt else frappe._dict()

    pe = frappe.new_doc(PE)
    pe.payment_type = "Pay"
    pe.company = company
    pe.party_type = "Supplier"
    pe.party = supplier
    pe.paid_from = gl
    if dt == "Purchase Invoice" and values.get("credit_to"):
        pe.paid_to = values.credit_to  # the invoice's own payable account, which ERPNext checks
    else:
        # an order or no document: ERPNext moves it to the advance account when the company books
        # advances separately (set_liability_account)
        pe.paid_to = get_party_account("Supplier", supplier, company)
    for account in (pe.paid_from, pe.paid_to):
        _require_kes(account)
    pe.paid_amount = pe.received_amount = amount
    pe.source_exchange_rate = pe.target_exchange_rate = 1
    pe.bank_account = bank_account
    pe.mode_of_payment = money_in_common.mode_of_payment_for(doc.mpesa_settings)  # a label only
    pe.reference_no = doc.transaction_id
    pe.reference_date = pe.posting_date = date
    pe.cost_center = (
        values.get("cost_center")
        or _settings_value(doc.mpesa_settings, "cost_center")
        or erpnext.get_default_cost_center(company)
    )
    if dt:
        dimensions = [d for d in get_accounting_dimensions() if frappe.get_meta(dt).has_field(d)]
        header = frappe.db.get_value(dt, dn, dimensions, as_dict=True) if dimensions else None
        for field in dimensions:
            if header and header.get(field) and pe.meta.has_field(field):
                pe.set(field, header.get(field))
    pe.custom_remarks = 1
    pe.remarks = remark_text(doc, dn, remark)
    rows, problem = reference_rows(dt, dn, amount, cap)
    for row in rows:
        pe.append("references", row)
    return pe, problem


def reference_rows(dt, dn, amount: float, cap=None) -> tuple:
    """(rows, problem): how much of the payout the document takes, as Payment Entry references.
    What it cannot take stays on the same entry, unallocated (an advance to the supplier)."""
    if not dt:
        return [], None
    values = owed.read_document(dt, dn, for_update=True)  # the document row, last (R4)
    reason, limit = None, 0.0
    if not values or cint(values.docstatus) != 1:
        reason = "it is cancelled" if values and cint(values.docstatus) == 2 else "it is not submitted"
    elif dt == "Purchase Invoice":
        if cint(values.get("on_hold")):
            reason = "it is on hold"
        elif cint(values.get("is_return")):
            reason = "it is a debit note"
        limit = flt(values.get("outstanding_amount"))
    else:
        if values.get("status") in owed.ORDER_CLOSED:
            reason = f"it is {values.status}"
        elif flt(values.get("per_billed")) >= 100:
            reason = "it is fully billed"
        limit = (flt(values.get("rounded_total")) or flt(values.get("grand_total"))) - flt(values.get("advance_paid"))
    if not reason and limit <= MIN:
        reason = "nothing is left to pay on it"
    if reason:
        return [], (
            f"{dn} could no longer take this payment ({reason}), so KES {money(amount)} is an unallocated "
            "supplier advance. Check that the supplier was not paid twice."
        )
    if cap is not None:
        limit = min(limit, flt(cap))
    allocate = flt(min(flt(amount), limit), 2)
    if allocate <= MIN:
        return [], None
    template = values.get("payment_terms_template")
    if template and cint(frappe.db.get_value("Payment Terms Template", template, "allocate_payment_based_on_payment_terms")):
        rows, remaining = [], allocate
        for term in frappe.db.sql(
            """
            SELECT payment_term, payment_amount, paid_amount, outstanding FROM `tabPayment Schedule`
            WHERE parent = %s AND parenttype = %s ORDER BY due_date, idx
            """,
            (dn, dt),
            as_dict=True,
        ):
            if remaining <= MIN:
                break
            open_amount = min(flt(flt(term.payment_amount) - flt(term.paid_amount), 2), flt(term.outstanding, 2))
            if open_amount <= 0.1:
                continue  # ERPNext skips terms of 0.1 or less (F3)
            take = flt(min(open_amount, remaining), 2)
            rows.append({"reference_doctype": dt, "reference_name": dn, "allocated_amount": take,
                         "payment_term": term.payment_term})
            remaining = flt(remaining - take, 2)
        return rows, None
    return [{"reference_doctype": dt, "reference_name": dn, "allocated_amount": allocate}], None


def build_journal_entry(doc, account: str, cost_center, remark: str):
    """Bank Entry: Dr ``account``, Cr the payout's GL, the receipt as Cheque No (R6)."""
    bank_account, gl, date = payout_account(doc.name)
    if not gl:
        frappe.throw(f"Map a Bank Account on Mpesa Settings {doc.mpesa_settings} (Accounting Mapping)")
    amount = flt(doc.amount, 2)
    je = frappe.new_doc(JE)
    je.voucher_type = "Bank Entry"
    je.company = _company(doc, bank_account)
    je.posting_date = je.cheque_date = date
    je.cheque_no = doc.transaction_id
    je.user_remark = remark
    if je.meta.has_field("custom_remark"):  # v16 hides user_remark and builds remark itself unless told
        je.custom_remark = 1
        je.remark = remark
    je.append("accounts", {"account": account, "debit_in_account_currency": amount, "cost_center": cost_center,
                           "user_remark": remark})
    je.append("accounts", {"account": gl, "credit_in_account_currency": amount, "bank_account": bank_account,
                           "user_remark": remark})
    return je


# ---------------------------------------------------------------- Make Entry and Check Entry Again
@frappe.whitelist(methods=["POST"])
def make_entry(name, kind, supplier=None, purchase_invoice=None, purchase_order=None, account=None,
               cost_center=None, submit=1, remark=None):
    """Make the entry of a Paid request now: against an invoice or order, as a supplier advance,
    or as a Journal Entry. Runs with the user's own ERPNext permissions."""
    require(FINANCE_ROLES)
    if kind not in KINDS:
        frappe.throw(f"Entry Type must be one of: {', '.join(KINDS)}")
    entry_doctype = JE if kind == "Journal Entry" else PE
    frappe.has_permission(entry_doctype, "create", throw=True)
    frappe.get_doc(B2B, name).check_permission("read")
    dt = kind if kind in owed.DOC_FIELDS else None
    dn = cstr({"Purchase Invoice": purchase_invoice, "Purchase Order": purchase_order}.get(kind)).strip() or None
    if dt and not dn:
        frappe.throw(f"Pick the {dt}.")
    _, _, receipt = _peek(name)
    with _locks(name, dt, dn, receipt) as doc:
        if doc.status != "Paid" or not receipt:
            frappe.throw("Only a Paid request with an M-Pesa receipt gets an entry.")
        if cint(doc.docstatus) == 2:
            require(ADMIN_ROLES)
        entries = receipt_entries(receipt, for_update=True)
        if entries:
            e = entries[0]
            frappe.throw(f"Receipt {receipt} is already on {e.doctype} {e.name} ({_where(e)}): open it, "
                         "or press Check Entry Again.")
        amount = flt(doc.amount, 2)
        if kind == "Journal Entry":
            text = remark_text(doc, None, remark)
            entry, problem = build_journal_entry(doc, _journal_account(doc, account, cost_center), cost_center, text), None
            supplier = None
        else:
            if dt:
                values = owed.read_document(dt, dn)
                problems = owed.document_problems(dt, values)
                if problems:
                    frappe.throw(problems[0])
                if supplier and supplier != values.supplier:
                    frappe.throw(f"{dn} belongs to {values.supplier}, not {supplier}.")
                supplier = values.supplier
            supplier = supplier or doc.supplier
            if not supplier:
                frappe.throw("Pick the supplier.")
            hold = owed.supplier_hold(supplier)
            if hold:
                frappe.throw(hold)
            cap = None
            if dt:
                left = owed.left_to_pay(dt, dn, exclude_request=name, for_update=True)
                if left.left <= MIN:
                    frappe.throw(f"Nothing is left to pay on {dn}. {owed.explain(dn, left)}")
                cap = left.left
            entry, problem = build_payment_entry(doc, dt, dn, supplier, amount, cap=cap, remark=remark)
        _move_request(doc, dt, dn, supplier)  # checked before the entry is made
        entry.insert()
        if cint(submit) and frappe.has_permission(entry_doctype, "submit"):
            entry.submit()
        refresh_entry(doc, receipt_entries(receipt), feed=False)
        if problem:
            set_entry(doc, CHECK, problem)
    statement.after_payout(B2B, name)  # the payout line is reconciled with the entry now
    return {"doctype": entry_doctype, "name": entry.name, "docstatus": cint(entry.docstatus),
            "entry_status": doc.get("entry_status")}


def _journal_account(doc, account, cost_center) -> str:
    if not account:
        frappe.throw("Pick the account this payment is for.")
    bank_account, gl, _ = payout_account(doc.name)
    company = _company(doc, bank_account)
    acc = frappe.db.get_value("Account", account, ["name", "company", "is_group", "account_type", "report_type",
                                                   "account_currency"], as_dict=True)
    if not acc:
        frappe.throw(f"Account {account} not found.")
    if acc.company != company:
        frappe.throw(f"Account {account} is not in {company}.")
    if cint(acc.is_group):
        frappe.throw(f"{account} is a group account. Pick an account under it.")
    if acc.account_type in ("Receivable", "Payable"):
        frappe.throw(f"{account} is a {acc.account_type} account: use Purchase Invoice, Purchase Order or "
                     "Supplier Advance instead.")
    if account == gl:
        frappe.throw(f"{account} is the M-Pesa account this payment left. Pick the account it paid for.")
    if acc.report_type == "Profit and Loss" and not cost_center:
        frappe.throw(f"Pick a Cost Center for {account}.")
    _require_kes(account)
    return account


def _move_request(doc, dt, dn, supplier) -> None:
    """Fill the request's supplier and document where blank. Changing one already set moves the
    payout to another document or supplier: managers only, and it is noted on the request."""
    old_dt, old_dn = owed.document_of(doc)
    values, moved = {}, []
    if (old_dt, old_dn) != (dt, dn):
        if old_dt:
            moved.append(f"{old_dt} {old_dn} to {f'{dt} {dn}' if dt else 'no document'}")
        values.update({"purchase_invoice": dn if dt == "Purchase Invoice" else None,
                       "purchase_order": dn if dt == "Purchase Order" else None})
    if supplier and doc.supplier != supplier:
        if doc.supplier:
            moved.append(f"supplier {doc.supplier} to {supplier}")
        values["supplier"] = supplier
    if moved:
        require(ADMIN_ROLES)
        doc.add_comment("Comment", f"Make Entry by {frappe.session.user} moved this payment from {'; '.join(moved)}.")
    if values:
        doc.db_set(values, update_modified=False)


@frappe.whitelist(methods=["POST"])
def refresh_entry_now(name):
    """Check Entry Again."""
    require(FINANCE_ROLES)
    frappe.get_doc(B2B, name).check_permission("read")
    dt, dn, receipt = _peek(name)
    with _locks(name, dt, dn, receipt) as doc:
        if doc.status != "Paid":
            frappe.throw("Only a Paid request has an accounting entry to check.")
        state = refresh_entry(doc, receipt_entries(receipt, for_update=True))
    return {"entry_status": state, "entry_note": doc.get("entry_note")}


@frappe.whitelist()
def entry_facts(name):
    """What the Make Entry dialog shows before anything is chosen."""
    require(FINANCE_ROLES)
    doc = frappe.get_doc(B2B, name)
    doc.check_permission("read")
    bank_account, gl, date = payout_account(name)
    receipt = cstr(doc.transaction_id).strip()
    charge = frappe.db.get_value(JE, {"cheque_no": f"{statement.CHARGE_PREFIX}{receipt}", "docstatus": ["<", 2]},
                                 "name", order_by="creation asc") if receipt else None
    return {
        "amount": flt(doc.amount), "date": date, "receipt": receipt, "company": _company(doc, bank_account),
        "paid_to": doc.receiver_public_name or doc.receiver_shortcode, "bank_account": bank_account, "gl": gl,
        "charge_entry": charge, "supplier": doc.supplier, "purchase_invoice": doc.purchase_invoice,
        "purchase_order": doc.get("purchase_order"),
        "cost_center": _settings_value(doc.mpesa_settings, "cost_center"),
    }


# ---------------------------------------------------------------- entries staff cancel or delete
def allow_cancel(doc, method=None) -> None:
    """Payment / Journal Entry on_cancel: the B2B request's link to it does not stop the cancel
    (Frappe refuses to cancel what a submitted document links). The next check clears the link,
    and the payout counts against its document again (R8)."""
    linked = tuple(getattr(doc, "ignore_linked_doctypes", None) or ())
    if B2B not in linked:
        doc.ignore_linked_doctypes = (*linked, B2B)


def unlink_deleted(doc, method=None) -> None:
    """Payment / Journal Entry on_trash: a deleted draft stops being the request's entry."""
    field = "payment_entry" if doc.doctype == PE else "journal_entry"
    if not frappe.get_meta(B2B).has_field(field):
        return
    for name in frappe.get_all(B2B, filters={field: doc.name}, pluck="name", order_by="creation asc"):
        frappe.db.set_value(B2B, name, field, None, update_modified=False)
        frappe.get_doc(B2B, name).add_comment("Comment", f"{doc.doctype} {doc.name} was deleted.")


# ---------------------------------------------------------------- the sweep and the upgrade
def _ready() -> bool:
    """ERPNext is there and the site has the Accounting Entry fields (migrated)."""
    return money_in_common.erpnext_installed() and frappe.get_meta(B2B).has_field("entry_status")


def refresh_entries(limit: int = 100) -> None:
    """Every 15 minutes: Paid requests whose entry is not made, not submitted or changed since.

    Each is looked at again (an entry staff made by hand, or one cancelled since, is found by its
    receipt); a failed automatic booking is tried again for 30 days; notify_role is reminded once
    when one still has no entry after Entry Reminder (Days). Requests Paid before this release
    (never tried) are only looked at, never booked.
    """
    if not _ready():
        return
    rows = frappe.db.sql(
        f"""
        SELECT r.name
        FROM `tab{B2B}` r
        LEFT JOIN `tabPayment Entry` pe ON pe.name = r.payment_entry
        LEFT JOIN `tabJournal Entry` je ON je.name = r.journal_entry
        WHERE r.docstatus IN (1, 2) AND r.status = 'Paid' AND COALESCE(r.transaction_id, '') != ''
          AND (COALESCE(r.entry_status, '') != 'Entry Made' OR COALESCE(pe.docstatus, je.docstatus, -1) != 1)
        ORDER BY r.entry_attempted_on IS NULL, r.entry_attempted_on, r.creation
        LIMIT %s
        """,
        (cint(limit),),
        as_dict=True,
    )
    cutoff = getdate(add_days(nowdate(), -RETRY_WINDOW_DAYS))
    remind_after = _reminder_days()
    for row in rows:
        frappe.db.savepoint("mpesa_b2b_entry")
        try:
            _refresh_one(row.name, cutoff)
            _remind(row.name, remind_after)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback(save_point="mpesa_b2b_entry")
            frappe.log_error(title="M-Pesa B2B entry check failed", message=f"{row.name}\n{frappe.get_traceback()}",
                             reference_doctype=B2B, reference_name=row.name)
            frappe.db.commit()


def _refresh_one(name: str, cutoff) -> None:
    dt, dn, receipt = _peek(name)
    booked = False
    with _locks(name, dt, dn, receipt) as doc:
        if doc.status != "Paid" or not receipt:
            return
        entries = receipt_entries(receipt, for_update=True)
        refresh_entry(doc, entries)
        if (
            not entries
            and (doc.get("entry_mode") or AUTO) == AUTO
            and cint(doc.docstatus) == 1
            and doc.get("entry_attempted_on")
            and getdate(doc.creation) >= cutoff
        ):
            booked = bool(book_automatically(name))  # takes the same locks again: they are held
        elif doc.get("entry_attempted_on"):
            # looked at again: to the back of the queue, so one stuck request cannot hold up the rest
            doc.db_set("entry_attempted_on", now_datetime(), update_modified=False)
    if booked:
        statement.after_payout(B2B, name)


def _reminder_days() -> int:
    try:
        value = money_in_common.suite().get("entry_reminder_days")
    except frappe.DoesNotExistError:
        return 0
    return cint(value)


def _remind(name: str, days: int) -> None:
    """One alert to notify_role when a payout still has no entry after ``days`` (0: never)."""
    if days <= 0:
        return
    doc = frappe.get_doc(B2B, name)
    if doc.status != "Paid" or doc.get("entry_status") == MADE or doc.get("entry_alerted_on"):
        return
    paid_on = _paid_on(doc)
    if (getdate(nowdate()) - paid_on).days < days:
        return
    _alert_once(doc, f"{name} was paid on {paid_on} and still has no accounting entry: {doc.get('entry_note') or ''}")


def backfill_entry_status() -> None:
    """after_migrate, idempotent: give Paid requests from before this release their Accounting
    Entry state. They are only looked at: nothing is booked, fed or alerted."""
    if not _ready():
        return
    names = frappe.get_all(
        B2B,
        filters={"status": "Paid", "docstatus": ["in", [1, 2]], "entry_status": ["is", "not set"]},
        pluck="name",
        order_by="creation asc",
    )
    for name in names:
        try:
            doc = frappe.get_doc(B2B, name)
            refresh_entry(doc, feed=False, log=False)
        except Exception:
            frappe.log_error(title="M-Pesa B2B entry backfill failed", message=f"{name}\n{frappe.get_traceback()}")
