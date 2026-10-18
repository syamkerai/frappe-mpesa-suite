"""Find who paid and for what, for receipts the C2B flow could not place.

Ordered resolvers, first unambiguous hit wins:

1. Existing Payment Entry  – booked by hand with the receipt as reference -> just link
2. Reference               – account reference is a Payment Request / Sales Order / Sales Invoice
3. Invoice Number Repair   – "SINV2600123" or "sinv 26-00123" -> one open invoice
4. Customer Name           – "JOHNKAMAU" -> exactly one enabled customer (spaces/case ignored)
5. Payer Phone             – Safaricom's SHA-256 MSISDN equals the phone of exactly one open
                             order whose balance equals the amount
6. Orphan STK Prompt       – exactly one unanswered STK prompt for this amount sent shortly
                             before the payment (customer paid but callback never arrived)

Nothing is booked on an ambiguous result; every decision is written to Mpesa Match Log.
"""

import hashlib

import frappe
from frappe.utils import add_days, add_to_date, cint, cstr, flt, get_datetime, now_datetime, nowdate

from ..utils.phone import looks_like_sha256, normalize_msisdn
from .common import (
    EXPRESS,
    REGISTER,
    compact,
    enabled,
    log_match,
    parse_transtime,
    suite_int,
    tolerance,
)

NOT_A_REFERENCE = {"", "ONLINEPAYMENT", "PAYMENT", "NA", "NONE", "0"}


class Match(frappe._dict):
    """customer, doctype, name, method, details"""


# ---------------------------------------------------------------- entry points
def run(limit: int = 100) -> None:
    """Scheduler: retry unplaced draft receipts, least recently tried first, with backoff.

    Rotating by last attempt keeps old unmatchable receipts from starving new ones; the
    backoff (2 min doubling to 6 h) keeps the per-run cost flat as they accumulate.
    """
    if not enabled("enable_matching"):
        return
    since = add_days(nowdate(), -suite_int("matching_lookback_days", 45))
    rows = frappe.get_all(
        REGISTER,
        filters={
            "docstatus": 0,
            "posting_date": [">=", since],
            "payment_entry": ["is", "not set"],
            "matched_reference_name": ["is", "not set"],
        },
        fields=["name", "match_attempts", "last_match_attempt"],
        order_by="last_match_attempt asc, creation desc",
        limit=limit * 5,
    )
    now = now_datetime()
    due = [
        r.name
        for r in rows
        if not r.last_match_attempt
        or get_datetime(r.last_match_attempt) <= add_to_date(now, minutes=-min(2 ** cint(r.match_attempts), 360))
    ][:limit]

    frappe.local.mpesa_customer_index = None  # rebuild once per run
    for name in due:
        frappe.db.savepoint("mpesa_match")
        try:
            match_register(name)
        except Exception:
            frappe.db.rollback(save_point="mpesa_match")
            log_match("Error", register=frappe.get_doc(REGISTER, name), once=True,
                      details=frappe.get_traceback()[-1000:])
        frappe.db.set_value(
            REGISTER,
            name,
            {"match_attempts": cint(frappe.db.get_value(REGISTER, name, "match_attempts")) + 1,
             "last_match_attempt": now},
            update_modified=False,
        )

    book_for_submitted_orders()


def book_for_submitted_orders(limit: int = 100) -> None:
    """Receipts matched to an order that was a draft, once staff submit that order."""
    from .booking import book_register

    rows = frappe.db.sql(
        """
        SELECT reg.name FROM `tabMpesa C2B Payment Register` reg
        JOIN `tabSales Order` so ON so.name = reg.matched_reference_name
        WHERE reg.docstatus = 0 AND reg.matched_reference_doctype = 'Sales Order'
          AND (reg.payment_entry IS NULL OR reg.payment_entry = '')
          AND so.docstatus = 1 AND so.status NOT IN ('Closed', 'Completed')
        ORDER BY reg.creation ASC LIMIT %s
        """,
        (limit,),
        pluck="name",
    )
    for name in rows:
        frappe.db.savepoint("mpesa_so_book")
        try:
            book_register(name, "Sales Order")
        except Exception:
            frappe.db.rollback(save_point="mpesa_so_book")
            log_match("Error", "Sales Order", register=frappe.get_doc(REGISTER, name), once=True,
                      details=frappe.get_traceback()[-1000:])


def match_register(name: str) -> Match | None:
    from . import booking

    register = frappe.get_doc(REGISTER, name)
    if register.docstatus != 0 or register.get("payment_entry"):
        return None

    # 1. already booked by hand
    if enabled("link_manual_payment_entries"):
        pe = existing_payment_entry(register)
        if pe:
            booking.link_existing_payment_entry(register, pe)
            return Match(method="Existing Payment Entry", doctype="Payment Entry", name=pe.name, customer=pe.party)

    match = (
        resolve_reference(register)
        or (enabled("match_invoice_typos") and repair_invoice_number(register))
        or (enabled("match_customer_name") and match_customer_name(register))
        or (enabled("match_payer_phone") and match_payer_phone(register))
        or (enabled("match_orphan_stk") and adopt_orphan_stk(register))
    )
    if not match:
        log_match("Unmatched", register=register, once=True, details=f"Account reference: {register.billrefnumber or '-'}")
        return None

    if match.get("ambiguous"):
        log_match(
            "Ambiguous",
            match.method,
            register=register,
            once=True,
            candidates=match.candidates,
            details=match.details,
        )
        return None

    booking.apply_match(register, match)
    return match


# ---------------------------------------------------------------- resolvers
def existing_payment_entry(register):
    if not register.transid:
        return None
    rows = frappe.get_all(
        "Payment Entry",
        filters={"reference_no": register.transid, "docstatus": 1, "payment_type": "Receive"},
        fields=["name", "party", "party_type", "received_amount", "paid_amount"],
        order_by="creation asc",
    )
    rows = [r for r in rows if abs(flt(r.received_amount or r.paid_amount) - flt(register.transamount)) <= tolerance()]
    return rows[0] if len(rows) == 1 else None


def resolve_reference(register) -> Match | None:
    """Exact document name in the account reference."""
    ref = cstr(register.billrefnumber).strip()
    if compact(ref) in NOT_A_REFERENCE:
        return None

    if frappe.db.exists("Payment Request", ref):
        pr = frappe.db.get_value(
            "Payment Request", ref, ["reference_doctype", "reference_name", "party_type", "party"], as_dict=True
        )
        if pr.reference_doctype in ("Sales Order", "Sales Invoice"):
            customer = frappe.db.get_value(pr.reference_doctype, pr.reference_name, "customer")
            return Match(method="Reference", doctype=pr.reference_doctype, name=pr.reference_name, customer=customer,
                         details=f"Payment Request {ref}")
        if pr.party_type == "Customer" and pr.party:
            return Match(method="Reference", doctype="Customer", name=pr.party, customer=pr.party,
                         details=f"Payment Request {ref}")

    for doctype in ("Sales Invoice", "Sales Order"):
        row = frappe.db.get_value(doctype, {"name": ref, "docstatus": ["<", 2]}, ["name", "customer"], as_dict=True)
        if row:
            return Match(method="Reference", doctype=doctype, name=row.name, customer=row.customer)

    quotation = frappe.db.get_value(
        "Quotation", {"name": ref, "docstatus": 1, "quotation_to": "Customer"}, "party_name"
    )
    if quotation:
        return Match(method="Reference", doctype="Customer", name=quotation, customer=quotation,
                     details=f"Quotation {ref}")

    if frappe.db.exists("Customer", ref):
        return Match(method="Reference", doctype="Customer", name=ref, customer=ref)
    return None


def repair_invoice_number(register) -> Match | None:
    """Mistyped invoice number: compare letters+digits only against open invoices."""
    key = compact(register.billrefnumber)
    if len(key) < 5 or not any(c.isdigit() for c in key):
        return None
    rows = frappe.db.sql(
        """
        SELECT name, customer FROM `tabSales Invoice`
        WHERE docstatus = 1 AND outstanding_amount > 0
          AND company = COALESCE(%s, company)
          AND UPPER(REPLACE(REPLACE(REPLACE(REPLACE(name, '-', ''), '/', ''), ' ', ''), '.', '')) = %s
        """,
        (register.get("company"), key),
        as_dict=True,
    )
    if len(rows) == 1:
        return Match(method="Invoice Number Repair", doctype="Sales Invoice", name=rows[0].name,
                     customer=rows[0].customer, details=f"'{register.billrefnumber}' -> {rows[0].name}")
    if len(rows) > 1:
        return Match(method="Invoice Number Repair", ambiguous=True, candidates=[r.name for r in rows])
    return None


def _customer_index() -> dict:
    """compact(name or customer_name) -> customer names, built once per matcher run."""
    index = getattr(frappe.local, "mpesa_customer_index", None)
    if index is None:
        index = {}
        for c in frappe.get_all("Customer", filters={"disabled": 0}, fields=["name", "customer_name"], order_by="name asc"):
            for key in {compact(c.name).lower(), compact(c.customer_name).lower()} - {""}:
                index.setdefault(key, set()).add(c.name)
        frappe.local.mpesa_customer_index = index
    return index


def match_customer_name(register) -> Match | None:
    key = compact(register.billrefnumber).lower()
    if len(key) < 4 or key.isdigit():
        return None
    rows = sorted(_customer_index().get(key, ()))
    if len(rows) == 1:
        return Match(method="Customer Name", doctype="Customer", name=rows[0], customer=rows[0])
    if len(rows) > 1:
        return Match(method="Customer Name", ambiguous=True, candidates=rows)
    return None


def match_payer_phone(register) -> Match | None:
    """Safaricom sends MSISDN as SHA-256('2547…'). Hash the phones of orders that could be it."""
    payer = cstr(register.msisdn).strip().lower()
    if looks_like_sha256(payer):
        target = payer
    else:
        msisdn = normalize_msisdn(payer)
        target = hashlib.sha256(msisdn.encode()).hexdigest() if msisdn else None
    if not target:
        return None

    amount, tol = flt(register.transamount), tolerance()
    orders = frappe.db.sql(
        """
        SELECT name, customer, contact_mobile, contact_phone, docstatus,
               (COALESCE(NULLIF(rounded_total, 0), grand_total) - advance_paid) AS balance
        FROM `tabSales Order`
        WHERE docstatus < 2 AND status NOT IN ('Closed', 'Completed', 'Cancelled')
          AND ABS((COALESCE(NULLIF(rounded_total, 0), grand_total) - advance_paid) - %(amount)s) <= %(tol)s
          AND company = COALESCE(%(company)s, company)
          AND transaction_date >= %(since)s
        """,
        {"amount": amount, "tol": tol, "company": register.get("company"), "since": add_days(nowdate(), -90)},
        as_dict=True,
    )
    meta = frappe.get_meta("Sales Order")
    extra_phone_fields = [f for f in ("custom_walk_in_customer_phone_number", "custom_end_customer_phone") if meta.has_field(f)]
    skip_unverified_bot = meta.has_field("custom_created_by") and meta.has_field("custom_verified_details")

    hits = []
    for so in orders:
        if skip_unverified_bot:
            created_by, verified = frappe.db.get_value("Sales Order", so.name, ["custom_created_by", "custom_verified_details"])
            if created_by == "whatsapp-bot" and not verified:
                continue
        phones = {so.contact_mobile, so.contact_phone}
        if extra_phone_fields:
            phones.update(frappe.db.get_value("Sales Order", so.name, extra_phone_fields, as_dict=True).values())
        phones.update(
            frappe.get_all(
                "Payment Request",
                filters={"reference_doctype": "Sales Order", "reference_name": so.name},
                pluck="phone_number",
                order_by="creation asc",
            )
        )
        hashes = {hashlib.sha256(m.encode()).hexdigest() for m in map(normalize_msisdn, phones) if m}
        if target in hashes:
            hits.append(so)

    if len(hits) == 1:
        return Match(method="Payer Phone", doctype="Sales Order", name=hits[0].name, customer=hits[0].customer,
                     details="Payer phone matches the order contact; balance equals the amount")
    if len(hits) > 1:
        return Match(method="Payer Phone", ambiguous=True, candidates=[h.name for h in hits])
    return None


def adopt_orphan_stk(register) -> Match | None:
    """An STK prompt for this amount, sent just before the payment, never got its callback."""
    paid_at = parse_transtime(register.transtime)
    if not paid_at:
        return None
    window = suite_int("orphan_stk_window_seconds", 600)
    tol = tolerance()
    prompts = frappe.get_all(
        EXPRESS,
        filters={
            "docstatus": 1,
            "transaction_id": ["is", "not set"],
            "creation": ["between", [add_to_date(paid_at, seconds=-window), paid_at]],
        },
        fields=["name", "amount", "reference_doctype", "reference_name", "creation"],
        order_by="creation asc",
    )
    prompts = [p for p in prompts if abs(flt(p.amount) - flt(register.transamount)) <= tol and get_datetime(p.creation) <= paid_at]
    if len(prompts) > 1:
        return Match(method="Orphan STK Prompt", ambiguous=True, candidates=[p.name for p in prompts])
    if len(prompts) != 1:
        return None

    prompt = prompts[0]
    target = Match(method="Orphan STK Prompt", details=f"STK prompt {prompt.name}", express=prompt.name)
    if prompt.reference_doctype == "Payment Request":
        pr = frappe.db.get_value("Payment Request", prompt.reference_name, ["reference_doctype", "reference_name"], as_dict=True)
        if pr and pr.reference_doctype in ("Sales Order", "Sales Invoice"):
            target.update(doctype=pr.reference_doctype, name=pr.reference_name)
    elif prompt.reference_doctype in ("Sales Order", "Sales Invoice"):
        target.update(doctype=prompt.reference_doctype, name=prompt.reference_name)
    if not target.get("doctype"):
        return None
    target.customer = frappe.db.get_value(target.doctype, target.name, "customer")
    return target
