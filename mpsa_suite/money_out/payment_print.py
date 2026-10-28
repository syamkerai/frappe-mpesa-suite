"""80 mm thermal prints of what M-Pesa paid (round 4.1): a payment advice for each document the
suite pays (Purchase Invoice, Purchase Order, Expense Claim with its payees, Employee Advance,
Salary Slip) and a proof of payment for each payout (B2B request, B2C batch, B2C payout) and for a
Payment Entry, to see after a payment and to send to whoever it concerns (Print, PDF, Email).

The print formats (mpsa_suite/mpsa_suite/print_format/m_pesa_80mm_*) read everything through the
two Jinja methods here (hooks.py ``jinja``), never frappe.get_all in a template:
- the reader's read permission on the document is checked first, and a salary's amounts are shown
  only to who may see salaries (money_out.salary_access);
- a phone is masked as the send summary shows it (2547****0111), a long account number too;
- the name M-Pesa paid is split on its first " - " only (the number before it, the name after);
- a Payment Entry carrying a payout's receipt is that payout's, listed once;
- what was paid and what is left come from the entries booked (HRMS's count for a claim, the
  Payment Ledger for an invoice), never a stored total a race could leave short (round 4.1, item 1).
"""

import datetime
import json

import frappe
from frappe.utils import cint, cstr, flt, get_datetime, now_datetime

from ..utils.phone import mask, normalize_msisdn
from . import owed

B2B = owed.B2B
B2C = "B2C Payment Disbursement"
B2C_ROW = "B2C Payment Disbursement Reference"
B2C_REQUEST = "B2C Disbursement Request"
PE = "Payment Entry"
CLAIM, ADVANCE, SLIP = "Expense Claim", "Employee Advance", "Salary Slip"
DOCUMENTS = ("Purchase Invoice", "Purchase Order", CLAIM, ADVANCE, SLIP)
PAYOUTS = (B2B, B2C, B2C_REQUEST, PE)
# The documents a B2B request names, by its field
PAID_BY_B2B = {"purchase_invoice": "Purchase Invoice", "purchase_order": "Purchase Order",
               "expense_claim": CLAIM, "employee_advance": ADVANCE, "salary_slip": SLIP}
# The suite's 80 mm print format of each DocType
PRINT_FORMAT = {dt: f"M-Pesa 80mm {dt}" for dt in DOCUMENTS + PAYOUTS}
TITLE = {"Purchase Invoice": "Payment Advice", "Purchase Order": "Payment Advice (Order)",
         CLAIM: "Expense Claim Payment", ADVANCE: "Advance Payment", SLIP: "Salary Payment",
         B2B: "M-Pesa Payment Proof", B2C: "M-Pesa Payment Proof", B2C_REQUEST: "M-Pesa Payment Proof",
         PE: "Proof of Payment"}


# ---------------------------------------------------------------- pure helpers
def split_receiver(value) -> tuple:
    """(number, name) of the name M-Pesa paid: "254722000111 - JANE DOE" gives ("254722000111",
    "JANE DOE"), split on the first " - " only, so a hyphenated name stays whole; no number: (None,
    the name)."""
    text = cstr(value).strip()
    if " - " not in text:
        return None, text
    number, name = text.split(" - ", 1)
    return number.strip() or None, name.strip()


def masked(number) -> str:
    """A phone masked as the send summary shows it (2547****0111, a number M-Pesa masked stays as it
    is); an account number longer than six characters as ****6789; a till or paybill as it is."""
    number = cstr(number).strip()
    if not number:
        return ""
    if "*" in number:
        return number
    if number.lstrip("+").isdigit() and normalize_msisdn(number):
        return mask(number)
    return f"****{number[-4:]}" if len(number) > 6 else number


def masked_account(number) -> str:
    """A paybill's or bank's account number as ****6789 (one that looks like a phone too: it is an
    account here); a short one (six characters or fewer) as it is."""
    number = cstr(number).strip()
    return f"****{number[-4:]}" if len(number) > 6 else number


def sms(receipt, amount, name, number, when, sent: bool) -> str:
    """The payout as M-Pesa's confirmation SMS reads: "UJSSN5D5CE Confirmed. KES 50.00 paid to
    ASTRAL INDUSTRIES LTD HQ on 28/10/26 at 1:57 PM." (B2C: "sent to")."""
    to = " ".join(filter(None, [cstr(name).strip(), cstr(number).strip()])) or "the receiver"
    text = f"{receipt} Confirmed. KES {flt(amount):,.2f} {'sent' if sent else 'paid'} to {to}"
    when = as_datetime(when)
    if when:
        hour = when.hour % 12 or 12
        text += f" on {when:%d/%m/%y} at {hour}:{when:%M} {'AM' if when.hour < 12 else 'PM'}"
    return text + "."


def as_datetime(value):
    """A date, datetime or text as a datetime (a date at midnight); None for nothing."""
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time.min)
    return get_datetime(value)


def when_text(value) -> str:
    """28/10/26 13:57 (a date alone: 28/10/26)."""
    when = as_datetime(value)
    if not when:
        return ""
    return f"{when:%d/%m/%y}" if not (when.hour or when.minute) else f"{when:%d/%m/%y %H:%M}"


def receipt_key(value) -> str:
    return cstr(value).strip().upper()


def without_covered(entries, receipts) -> list:
    """The Payment Entries whose Reference No is no listed payout's receipt (pure): an entry the
    suite booked for a payout carries its receipt and is that payout's, printed once."""
    keys = {receipt_key(r) for r in receipts if receipt_key(r)}
    return [e for e in entries if not receipt_key(e.get("reference_no")) or receipt_key(e.get("reference_no")) not in keys]


# ---------------------------------------------------------------- reading
def _check(doc) -> None:
    frappe.has_permission(doc.doctype, "read", doc=doc.name, throw=True)


def _salary_hidden(doctype: str, values=None) -> bool:
    """A salary's amounts are not shown to who may not see salaries (decision D4)."""
    from .salary_access import can_see_salaries

    salary = doctype == SLIP or bool(values and cstr(values.get("salary_slip")).strip()) or (
        bool(values) and values.get("reference_doctype") == SLIP)
    return salary and not can_see_salaries()


def _settings_shortcode(name) -> str:
    if not name:
        return ""
    return cstr(frappe.db.get_value("Mpesa Settings", name, "business_shortcode")).strip()


def _charge(doctype: str, values) -> tuple:
    """(charge, how): the charge Journal Entry booked for the receipt, else what Safaricom stated (B2B),
    else the B2C Charge Bands' figure (an estimate); (None, "") when unknown."""
    from . import statement

    receipt = cstr(values.get("transaction_id")).strip()
    if receipt:
        booked = frappe.db.get_value("Journal Entry", {"cheque_no": f"{statement.CHARGE_PREFIX}{receipt}",
                                                       "docstatus": 1}, "total_debit")
        if booked:
            return flt(booked, 2), ""
    if doctype == B2B and cstr(values.get("debit_party_charges")).strip():
        return statement.parse_charges(values.debit_party_charges), ""
    if doctype == B2C_REQUEST and values.get("amount"):
        bands = statement.charge_bands()
        charge = statement.band_charge(bands, values.amount) if bands else None
        if charge is not None:
            return flt(charge, 2), "est."
    return None, ""


def _sent_by(doctype: str, name: str) -> str | None:
    """Who submitted (sent) it: the Version that moved it from draft to submitted."""
    for version in frappe.get_all("Version", filters={"ref_doctype": doctype, "docname": name},
                                  fields=["owner", "data"], order_by="creation asc", limit=50):
        try:
            changed = json.loads(version.data or "{}").get("changed") or []
        except ValueError:
            continue
        if any(c[:3] == ["docstatus", 0, 1] for c in changed if isinstance(c, list)):
            return version.owner
    return None


def _who(user) -> str:
    """A user's full name (the login when it has none)."""
    return cstr(frappe.db.get_value("User", user, "full_name")).strip() or user if user else ""


def _letter_head(doc) -> frappe._dict:
    """The document's Letter Head (else the default one): its header and footer images, if set."""
    name = doc.get("letter_head") or frappe.db.get_value("Letter Head", {"is_default": 1, "disabled": 0}, "name")
    values = frappe.db.get_value("Letter Head", name, ["image", "footer_image"], as_dict=True) if name else None
    return values or frappe._dict()


def _common(doc) -> dict:
    return {"company": doc.get("company") or "", "letter_head": _letter_head(doc),
            "printed": f"{now_datetime():%d-%m-%Y %H:%M}"}


def _b2b_requests(field: str, name: str) -> list:
    if not frappe.db.has_column(B2B, field):
        return []
    fields = ["name", "docstatus", "status", "transaction_id", "receiver_public_name", "receiver_shortcode",
              "account_reference", "command_id", "amount", "trans_completed_time", "error_message", "result_desc",
              "creation"] + (["claim_payee"] if frappe.db.has_column(B2B, "claim_payee") else [])
    rows = frappe.get_all(B2B, filters={field: name, "docstatus": ["in", [1, 2]]}, fields=fields,
                          order_by="creation asc")
    return [r for r in rows if r.docstatus == 1 or r.status == "Paid"]


def _b2c_requests(doctype: str, name: str) -> list:
    rows = frappe.get_all(B2C_REQUEST, filters={"reference_doctype": doctype, "reference_name": name,
                                                "docstatus": ["in", [1, 2]]},
                          fields=["name", "docstatus", "status", "transaction_id", "receiver_public_name",
                                  "phone_number", "amount", "transaction_completed_datetime", "error_message",
                                  "result_desc", "b2c_payment_disbursement_reference", "creation"],
                          order_by="creation asc")
    rows = [r for r in rows if r.docstatus == 1 or r.status == "Paid"]
    if rows and frappe.db.has_column(B2C_ROW, "claim_payee"):
        refs = [r.b2c_payment_disbursement_reference for r in rows if r.b2c_payment_disbursement_reference]
        payees = {r.name: r.claim_payee for r in frappe.get_all(B2C_ROW, filters={"name": ["in", refs]},
                                                                fields=["name", "claim_payee"])} if refs else {}
        for r in rows:
            r.claim_payee = payees.get(r.b2c_payment_disbursement_reference)
    return rows


def _b2b_row(r) -> frappe._dict:
    number, name = split_receiver(r.receiver_public_name)
    to = cstr(r.receiver_shortcode).strip()
    if cstr(r.account_reference).strip():
        to += f" acc {masked_account(r.account_reference)}"
    return frappe._dict(
        via="M-Pesa B2B", payout=r.name, receipt=cstr(r.transaction_id).strip(), amount=flt(r.amount, 2),
        status=r.status or "Not Initiated", when=r.trans_completed_time or r.creation, name=name or to,
        number=masked(number) if number else to, payee=r.get("claim_payee"),
        note=(r.result_desc or r.error_message) if r.status == "Failed" else None)


def _b2c_row(r) -> frappe._dict:
    number, name = split_receiver(r.receiver_public_name)
    return frappe._dict(
        via="M-Pesa B2C", payout=r.name, receipt=cstr(r.transaction_id).strip(), amount=flt(r.amount, 2),
        status=r.status or "Not Initiated", when=r.transaction_completed_datetime or r.creation,
        name=name or masked(r.phone_number), number=masked(number or r.phone_number), payee=r.get("claim_payee"),
        note=(r.result_desc or r.error_message) if r.status == "Failed" else None)


def _entries(doctype: str, name: str) -> list:
    """Submitted Payment Entries allocated to the document, with what each allocated to it. An
    order's advance that Get Advances moved onto an invoice keeps the order as its advance voucher
    (as owed.allocated_on reads it): still the order's payment."""
    filters = {"parenttype": PE, "docstatus": 1}
    refs = frappe.get_all("Payment Entry Reference", filters={**filters, "reference_doctype": doctype,
                                                              "reference_name": name},
                          fields=["name", "parent", "allocated_amount"])
    if doctype == "Purchase Order" and frappe.db.has_column("Payment Entry Reference", "advance_voucher_no"):
        moved = frappe.get_all("Payment Entry Reference", filters={**filters, "advance_voucher_type": doctype,
                                                                   "advance_voucher_no": name},
                               fields=["name", "parent", "allocated_amount"])
        refs = list({r.name: r for r in [*refs, *moved]}.values())
    allocated = {}
    for r in refs:
        allocated[r.parent] = flt(allocated.get(r.parent, 0) + flt(r.allocated_amount), 2)
    if not allocated:
        return []
    entries = frappe.get_all(PE, filters={"name": ["in", list(allocated)]},
                             fields=["name", "payment_type", "mode_of_payment", "reference_no", "reference_date",
                                     "posting_date", "party_name", "creation"],
                             order_by="posting_date asc, creation asc")
    for e in entries:
        e.allocated = allocated[e.name]
    return entries


def payment_rows(doctype: str, name: str) -> list:
    """Every payment of the document, oldest first: its M-Pesa payouts (B2B and B2C, the ones that
    failed too) and the Payment Entries allocated to it that carry no listed payout's receipt."""
    field = owed.B2B_DOC_FIELDS.get(doctype) or ("salary_slip" if doctype == SLIP else None)
    rows = [_b2b_row(r) for r in (_b2b_requests(field, name) if field else [])]
    rows += [_b2c_row(r) for r in _b2c_requests(doctype, name)]
    for e in without_covered(_entries(doctype, name), [r.receipt for r in rows]):
        rows.append(frappe._dict(
            via=e.mode_of_payment or PE, payout=e.name, receipt=cstr(e.reference_no).strip(), amount=e.allocated,
            status="Paid", when=e.posting_date, name=e.party_name or "", number="", payee=None, note=None))
    rows = sorted(rows, key=lambda r: as_datetime(r.when) or datetime.datetime.min)
    for r in rows:
        r.when_text = when_text(r.when)
    return rows


def _ledger_outstanding(doctype: str, name: str) -> float | None:
    """What the Payment Ledger says the invoice still owes (ERPNext's own reading), None without one."""
    rows = frappe.db.sql(
        """SELECT SUM(amount_in_account_currency) FROM `tabPayment Ledger Entry`
        WHERE against_voucher_type = %s AND against_voucher_no = %s AND delinked = 0""",
        (doctype, name),
    )
    value = rows[0][0] if rows else None
    return None if value is None else flt(value, 2)


def _allocated(doctype: str, name: str) -> tuple:
    """(paid, returned) by submitted entries: Payment Entries that pay it (an order's moved advance
    too) and Journal Entry debits; returned: Payment Entries that receive and Journal Entry credits
    (HRMS's Return of an advance), counted apart as HRMS does, never netted."""
    entries = _entries(doctype, name)
    paid = sum(e.allocated for e in entries if e.get("payment_type") != "Receive")
    returned = sum(e.allocated for e in entries if e.get("payment_type") == "Receive")
    je = frappe.db.sql(
        """SELECT COALESCE(SUM(debit_in_account_currency), 0), COALESCE(SUM(credit_in_account_currency), 0)
        FROM `tabJournal Entry Account` WHERE reference_type = %s AND reference_name = %s AND docstatus = 1""",
        (doctype, name))[0]
    return flt(paid + flt(je[0]), 2), flt(returned + flt(je[1]), 2)


def document_total(doctype: str, doc) -> float:
    if doctype == CLAIM:
        return flt(doc.get("grand_total"), 2)
    if doctype == ADVANCE:
        return flt(doc.get("advance_amount"), 2)
    if doctype == SLIP:
        return flt(doc.get("rounded_total") or doc.get("net_pay"), 2)
    return flt(doc.get("rounded_total") or doc.get("grand_total"), 2)


def paid_and_left(doctype: str, doc, rows=()) -> tuple:
    """(total, paid, left) of the document from the entries booked, not a stored total: a claim by
    HRMS's own count of its entries, an invoice by the Payment Ledger, an order or advance by what
    entries paid to it (returns apart), a salary slip as the suite reads it
    (b2c_payment_disbursement.salary_slip_paid: its Paid payouts and the payroll's bank entry)."""
    total = document_total(doctype, doc)
    if doctype == "Purchase Invoice":
        left = _ledger_outstanding(doctype, doc.name)
        if left is None:
            left = flt(doc.get("outstanding_amount"), 2)
        return total, flt(total - left, 2), max(0.0, flt(left, 2))
    if doctype == CLAIM:
        from hrms.hr.doctype.expense_claim.expense_claim import get_total_reimbursed_amount

        paid = flt(get_total_reimbursed_amount(doc), 2)
    elif doctype == SLIP:
        from ..mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement import salary_slip_paid

        paid = flt(salary_slip_paid(doc.name, doc.get("employee"), doc.get("payroll_entry")), 2)
    else:
        paid = _allocated(doctype, doc.name)[0]
    return total, paid, max(0.0, flt(total - paid, 2))


def _party(doctype: str, doc) -> tuple:
    """(label, name, id) of who the document pays."""
    if doctype in ("Purchase Invoice", "Purchase Order"):
        return "Supplier", doc.get("supplier_name") or doc.get("supplier"), doc.get("supplier")
    return "Employee", doc.get("employee_name") or doc.get("employee"), doc.get("employee")


def _line_to(line, claim_payees) -> str:
    """Where a payee line pays, masked: a phone as the send summary shows it, a paybill's or bank's
    account number as ****6789."""
    if line.payee_type == claim_payees.PHONE:
        return claim_payees.describe(line, masked=True)
    code, account = cstr(line.shortcode).strip(), cstr(line.account_number).strip()
    return f"{code} / {masked_account(account)}" if account and line.payee_type != claim_payees.TILL else code


def _claim_payees(claim: str) -> list:
    """An Expense Claim's merchant and casual payee lines as printed: a phone masked, the name M-Pesa paid
    masked too."""
    from . import claim_payees

    if not claim_payees.paid_in_parts(CLAIM, claim):
        return []
    out = []
    for line in claim_payees.read_lines(claim):
        out.append(frappe._dict(
            idx=line.idx, name=line.payee_name, type=line.payee_type, to=_line_to(line, claim_payees),
            amount=flt(line.amount, 2), status=line.status or "Draft", receipt=line.receipt,
            paid_to=claim_payees.masked_name(line.receiver_name) if line.receiver_name else "",
            verdict=line.get("name_verdict") or "", staff=line.get("staff_check") or ""))
    return out


# ---------------------------------------------------------------- the Jinja methods
def mpsa_payment_advice(doc) -> frappe._dict:
    """What the payment advice of a Purchase Invoice, Purchase Order, Expense Claim, Employee Advance or
    Salary Slip prints: the party, the totals from the entries booked, every payment and, for a claim,
    its payee lines and the claimant's own share. Amounts are None for a salary the reader may not
    see."""
    _check(doc)
    doctype = doc.doctype
    if doctype not in DOCUMENTS:
        frappe.throw(f"{doctype} has no M-Pesa payment advice.")
    rows = payment_rows(doctype, doc.name)
    hidden = _salary_hidden(doctype)
    total, paid, left = paid_and_left(doctype, doc, rows)
    label, party, party_id = _party(doctype, doc)
    out = frappe._dict(title=TITLE[doctype], doctype=doctype, name=doc.name, party_label=label, party=party,
                       party_id=party_id, total=total, paid=paid, left=left, rows=rows, hidden=hidden,
                       currency=doc.get("currency") or "KES", payees=[], claimant_share=None, **_common(doc))
    if doctype == ADVANCE:
        out.returned = _allocated(doctype, doc.name)[1]
    if doctype == CLAIM:
        out.payees = _claim_payees(doc.name)
        payee_total = sum(p.amount for p in out.payees if p.status != "Cancelled")
        out.claimant_share = max(0.0, flt(total - payee_total, 2))
        out.claimant_paid = flt(sum(r.amount for r in rows if not r.payee and r.status == "Paid"), 2)
    if hidden:
        out.update(total=None, paid=None, left=None, claimant_share=None)
        for r in rows:
            r.amount = None
    return out


def mpsa_payout_proof(doc) -> frappe._dict:
    """What the proof of payment of a B2B request, a B2C Payment Disbursement (a block per row), a B2C
    Disbursement Request or a Payment Entry prints: each payout as M-Pesa's confirmation SMS reads, its
    receiver (a phone masked), the shortcode it left from (Mpesa Settings), its charge, the document it
    paid, and who prepared and who sent it."""
    _check(doc)
    doctype = doc.doctype
    if doctype not in PAYOUTS:
        frappe.throw(f"{doctype} has no M-Pesa proof of payment.")
    blocks, prepared, sent = [], doc.get("owner"), None
    if doctype == B2B:
        blocks = [_b2b_block(doc)]
        sent = _sent_by(B2B, doc.name)
    elif doctype == B2C_REQUEST:
        blocks = [_b2c_block(doc)]
        parent = doc.get("b2c_payment_disbursement")
        prepared = frappe.db.get_value(B2C, parent, "owner") if parent else doc.get("owner")
        sent = (_sent_by(B2C, parent) if parent else None) or _sent_by(B2C_REQUEST, doc.name) or doc.get("owner")
    elif doctype == B2C:
        for row in doc.get("references") or []:
            request = cstr(row.get("b2c_disbursement_request")).strip()
            if request and frappe.db.exists(B2C_REQUEST, request):
                blocks.append(_b2c_block(frappe.get_doc(B2C_REQUEST, request), row))
            else:
                blocks.append(frappe._dict(
                    status=row.get("payment_status") or "Not Initiated", receipt="", sms="", name=row.get("party"),
                    number=masked(row.get("partyb")), amount=flt(row.get("allocated_amount"), 2), charge=None,
                    charge_how="", paid_from="", when=None,
                    document=" ".join(filter(None, [row.get("reference_doctype"), row.get("reference_name")])),
                    hidden=_salary_hidden(B2C_REQUEST, row)))
        sent = _sent_by(B2C, doc.name)
    else:
        blocks = _entry_blocks(doc)
        sent = _sent_by(PE, doc.name)
    for b in blocks:
        b.when_text = when_text(b.when)
        if b.hidden:  # a salary the reader may not see: who and the receipt, never how much
            b.update(amount=None, charge=None, sms="")
    return frappe._dict(title=TITLE[doctype], doctype=doctype, name=doc.name, blocks=blocks,
                        prepared_by=_who(prepared), sent_by=_who(sent),
                        # what left the shortcode: Paid payouts only (a failed or unsent row moved no money)
                        total=flt(sum(flt(b.amount) for b in blocks if not b.hidden and b.status == "Paid"), 2),
                        **_common(doc))


def _b2b_block(r) -> frappe._dict:
    number, name = split_receiver(r.get("receiver_public_name"))
    to = cstr(r.get("receiver_shortcode")).strip()
    if cstr(r.get("account_reference")).strip():
        to += f" acc {masked_account(r.account_reference)}"
    receipt = cstr(r.get("transaction_id")).strip()
    charge, how = _charge(B2B, r)
    field = next((f for f in PAID_BY_B2B if cstr(r.get(f)).strip()), None)
    payee = cstr(r.get("claim_payee")).strip()
    party = (r.get("supplier") or r.get("employee") or "")
    if payee:
        party = cstr(frappe.db.get_value("M-Pesa Claim Payee", payee, "payee_name")) or party
    paid = r.get("status") == "Paid" and receipt
    return frappe._dict(
        status=r.get("status") or "Not Initiated", receipt=receipt, name=name or party, number=masked(number) or to,
        to=to, amount=flt(r.get("amount"), 2), charge=charge, charge_how=how,
        paid_from=_settings_shortcode(r.get("mpesa_settings")), when=r.get("trans_completed_time"),
        party=party, document=f"{PAID_BY_B2B[field]} {r.get(field)}" if field else "",
        sms=sms(receipt, r.get("amount"), name or party, to, r.get("trans_completed_time"), False) if paid else "",
        note=(r.get("result_desc") or r.get("error_message")) if r.get("status") == "Failed" else None,
        hidden=_salary_hidden(B2B, r))


def _b2c_block(r, row=None) -> frappe._dict:
    number, name = split_receiver(r.get("receiver_public_name"))
    receipt = cstr(r.get("transaction_id")).strip()
    phone = masked(number or r.get("phone_number"))
    charge, how = _charge(B2C_REQUEST, r)
    payee = cstr((row or {}).get("claim_payee")).strip()
    party = r.get("party") or ""
    if payee:
        party = cstr(frappe.db.get_value("M-Pesa Claim Payee", payee, "payee_name")) or party
    paid = r.get("status") == "Paid" and receipt
    document = " ".join(filter(None, [r.get("reference_doctype"), r.get("reference_name")]))
    return frappe._dict(
        status=r.get("status") or "Not Initiated", receipt=receipt, name=name or party, number=phone, to=phone,
        amount=flt(r.get("amount"), 2), charge=charge, charge_how=how,
        paid_from=_settings_shortcode(r.get("mpesa_settings")), when=r.get("transaction_completed_datetime"),
        party=party, document=document,
        sms=sms(receipt, r.get("amount"), name or party, phone, r.get("transaction_completed_datetime"), True)
        if paid else "",
        note=(r.get("result_desc") or r.get("error_message")) if r.get("status") == "Failed" else None,
        hidden=_salary_hidden(B2C_REQUEST, r))


def _entry_blocks(doc) -> list:
    """A Payment Entry: the M-Pesa payout whose receipt it carries, else the entry itself."""
    receipt = cstr(doc.get("reference_no")).strip()
    if receipt:
        request = frappe.db.get_value(B2C_REQUEST, {"transaction_id": receipt, "docstatus": ["in", [1, 2]]}, "name")
        if request:
            return [_b2c_block(frappe.get_doc(B2C_REQUEST, request))]
        request = frappe.db.get_value(B2B, {"transaction_id": receipt, "docstatus": ["in", [1, 2]]}, "name")
        if request:
            return [_b2b_block(frappe.get_doc(B2B, request))]
    documents = ", ".join(f"{r.reference_doctype} {r.reference_name}" for r in doc.get("references") or [])
    return [frappe._dict(
        status="Paid" if cint(doc.docstatus) == 1 else ("Cancelled" if cint(doc.docstatus) == 2 else "Draft"),
        receipt=receipt, name=doc.get("party_name") or doc.get("party"), number="", to=doc.get("mode_of_payment"),
        amount=flt(doc.get("paid_amount"), 2), charge=None, charge_how="", paid_from=doc.get("paid_from") or "",
        when=doc.get("reference_date") or doc.get("posting_date"), party=doc.get("party_name") or doc.get("party"),
        document=documents, sms="", note=None, hidden=False)]


# ---------------------------------------------------------------- Send Payment Proof
@frappe.whitelist()
def proof_recipients(doctype: str, name: str, row: str | None = None) -> dict:
    """Send Payment Proof on a paid payout: what Frappe's email composer is filled with, for ONE
    party only. Nothing is sent: the user reviews the email and presses Send.

    - A payout of one party (a B2B request, a B2C payout, a Payment Entry, a B2C batch whose rows
      all pay one party): its own proof, to that party's email.
    - A B2C batch of several parties: never the batch's proof, which lists every party's payout.
      ``{"rows": [...]}``, its paid rows to choose from; called again with ``row``, that row's
      payout (its B2C Disbursement Request) and that payout's proof, to that row's party only.
    - Never a salary payout: the employee's Salary Slip is its record, and a proof would put net
      pay into an email.
    For who may read the payout and send email on what is attached (Frappe's email right)."""
    if doctype not in PAYOUTS:
        frappe.throw(f"{doctype} has no M-Pesa proof of payment.")
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    doc = frappe.get_doc(doctype, name)
    if is_salary_payout(doctype, doc):
        frappe.throw("A salary payout gets no payment proof by email: the employee's Salary Slip is its record.")
    target = doc
    if doctype == B2C:
        rows = doc.get("references") or []
        if len({_row_party(doc, r) for r in rows}) > 1:
            paid = [r for r in rows if r.get("payment_status") == "Paid"
                    and cstr(r.get("b2c_disbursement_request")).strip()]
            if not row:
                return {"rows": [{"row": r.name, "label": f"#{r.idx} {r.get('party') or ''} {masked(r.get('partyb'))} "
                                                         f"KES {flt(r.get('allocated_amount')):,.2f}"} for r in paid]}
            chosen = next((r for r in paid if r.name == row), None)
            if not chosen:
                frappe.throw(f"Row {row} of {name} is no paid payout of it.")
            frappe.has_permission(B2C_REQUEST, "read", doc=chosen.b2c_disbursement_request, throw=True)
            target = frappe.get_doc(B2C_REQUEST, chosen.b2c_disbursement_request)
    frappe.has_permission(target.doctype, "email", doc=target.name, throw=True)
    email = _email_of(*_party_of(target))
    return {"doctype": target.doctype, "name": target.name, "recipients": [email] if email else [],
            "print_format": PRINT_FORMAT[target.doctype], "subject": f"Proof of payment: {target.doctype} {target.name}"}


def is_salary_payout(doctype: str, doc) -> bool:
    """A payout of a salary: a B2B request for a Salary Slip, a B2C payout or batch row paying one, or
    a Payment Entry allocated to one."""
    if doctype == B2B:
        return bool(cstr(doc.get("salary_slip")).strip())
    if doctype == B2C_REQUEST:
        return doc.get("reference_doctype") == SLIP
    if doctype == B2C:
        return doc.get("transaction_to_pay_against") == SLIP or any(
            r.get("reference_doctype") == SLIP for r in doc.get("references") or [])
    return any(r.get("reference_doctype") == SLIP for r in doc.get("references") or [])


def _row_party(doc, row) -> tuple:
    return (row.get("party_type") or doc.get("party_type"), row.get("party"))


def _party_of(doc) -> tuple:
    """The one party a payout concerns: (party type, party)."""
    if doc.doctype == B2B:
        return ("Supplier", doc.supplier) if cstr(doc.get("supplier")).strip() else ("Employee", doc.get("employee"))
    if doc.doctype == B2C:
        rows = doc.get("references") or []
        return _row_party(doc, rows[0]) if rows else (None, None)
    return doc.get("party_type"), doc.get("party")


def _email_of(party_type, party) -> str | None:
    if not party_type or not party:
        return None
    if party_type == "Employee":
        meta = frappe.get_meta("Employee")
        fields = [f for f in ("prefered_email", "company_email", "personal_email") if meta.has_field(f)]
        values = frappe.db.get_value("Employee", party, fields, as_dict=True) or {}
        return next((cstr(values.get(f)).strip() for f in fields if cstr(values.get(f)).strip()), None)
    if party_type in ("Supplier", "Customer") and frappe.get_meta(party_type).has_field("email_id"):
        return cstr(frappe.db.get_value(party_type, party, "email_id")).strip() or None
    return None
