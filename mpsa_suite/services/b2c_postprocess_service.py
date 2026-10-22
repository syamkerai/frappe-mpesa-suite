import frappe
from frappe.utils import flt

from mpsa_suite.accounting.journal_service import create_journal_entry
from mpsa_suite.accounting.payment_entry_service import (
    create_payment_entry_for_doc,
)
from mpsa_suite.utils.doctype_names import (
    B2C_REQUEST_DOCTYPE,
    MPESA_SETTINGS_DOCTYPE,
)


def handle_successful_payment(disbursement_name: str, reference_name: str) -> bool:
    """
    After reference row is updated, create accounting entries:
    - Salary Slip → Journal Entry
    - Others → Payment Entry

    Returns True once the payout is booked. A failure is rolled back to a savepoint and
    recorded on the B2C request (``accounting_error``); ``retry_unbooked_payouts`` tries
    again, so a paid payout cannot silently stay out of the books.
    """
    # Lock the row so a re-sent Result callback cannot book the same payout twice.
    frappe.db.get_value(
        "B2C Payment Disbursement Reference", reference_name, "name", for_update=True
    )
    ref = frappe.get_doc("B2C Payment Disbursement Reference", reference_name)
    receipt = payout_receipt(ref)
    if receipt and not (ref.reference_no or "").strip():
        # a row paid before the suite: give it its request's receipt, which the entry carries
        ref.db_set("reference_no", receipt, update_modified=False)
    if not receipt:
        # The receipt is what tells this payout's entry apart from another row's for the
        # same document; without it an entry could not be found again, so every retry
        # would book once more. Recorded, and left to staff.
        _record_booking_error(
            ref.b2c_disbursement_request,
            f"Paid payout has no M-Pesa receipt (Reference No on row {ref.name}); "
            "not booked. Book it by hand.",
        )
        return False
    if not already_booked(disbursement_name, ref):
        elsewhere = entry_carrying_receipt(ref)
        if elsewhere:
            _mark_booked_elsewhere(ref, *elsewhere)
            return False
        frappe.db.savepoint("mpesa_b2c_booking")
        try:
            _book(disbursement_name, ref)
        except Exception:
            frappe.db.rollback(save_point="mpesa_b2c_booking")
            _record_booking_error(ref.b2c_disbursement_request, frappe.get_traceback())
            return False
    return _mark_booked(disbursement_name, ref)


def _book(disbursement_name: str, ref) -> None:
    b2c_req = frappe.get_doc(B2C_REQUEST_DOCTYPE, ref.b2c_disbursement_request)
    b2c_disb = frappe.get_doc(ref.parenttype, disbursement_name)

    if not b2c_req.mpesa_settings:
        frappe.throw(f"No Mpesa Settings on B2C Request {b2c_req.name}")

    settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, b2c_req.mpesa_settings)
    submit = settings.submit_b2c_accounting_entries

    doctype = ref.reference_doctype
    if doctype == "Salary Slip":
        create_journal_entry(b2c_disb, ref, submit)
    elif doctype in (
        "Employee Advance",
        "Expense Claim",
        "Purchase Invoice",
        "Purchase Order",
    ):
        create_payment_entry_for_doc(b2c_disb, ref, submit)
    else:
        frappe.throw(f"No accounting entry is defined for B2C payouts against {doctype}")


def _has_field(fieldname: str) -> bool:
    return frappe.get_meta(B2C_REQUEST_DOCTYPE).has_field(fieldname)


def _mark_booked(disbursement_name: str, ref) -> bool:
    """Booked means a *submitted* entry: a draft does not reduce what the document owes,
    so the payout keeps blocking a second payout (and keeps being re-checked) until then."""
    b2c_request = ref.b2c_disbursement_request
    submitted = booked_docstatus(disbursement_name, ref) == 1
    if b2c_request and _has_field("accounting_booked"):
        frappe.db.set_value(
            B2C_REQUEST_DOCTYPE,
            b2c_request,
            {
                "accounting_booked": 1 if submitted else 0,
                "accounting_error": None if submitted else "Accounting entry is a draft; submit it.",
            },
            update_modified=False,
        )
    return submitted


def _record_booking_error(b2c_request: str | None, traceback: str) -> None:
    first_failure = not (
        b2c_request
        and _has_field("accounting_error")
        and frappe.db.get_value(B2C_REQUEST_DOCTYPE, b2c_request, "accounting_error")
    )
    if first_failure:  # the sweep retries every run; log the traceback once
        frappe.log_error(title=f"B2C accounting failed for {b2c_request}"[:140], message=traceback)
    if b2c_request and _has_field("accounting_error"):
        frappe.db.set_value(
            B2C_REQUEST_DOCTYPE,
            b2c_request,
            "accounting_error",
            traceback.strip().splitlines()[-1][:500] if traceback.strip() else "Unknown error",
            update_modified=False,
        )


# Older paid payouts are left to staff (the Error Log and accounting_error say why).
RETRY_WINDOW_DAYS = 30


def retry_unbooked_payouts(limit: int = 100) -> None:
    """Scheduler: book paid payouts whose Payment / Journal Entry is still missing.

    Covers a booking that failed (rolled back, recorded in ``accounting_error``) and a
    downstream job that never ran. Least recently tried first, so one stuck payout
    cannot starve the rest.
    """
    from frappe.utils import add_days, now_datetime, nowdate

    from .b2c_response_service import update_b2c_reference_status

    if not _has_field("accounting_booked"):
        return
    names = frappe.get_all(
        B2C_REQUEST_DOCTYPE,
        filters={
            "docstatus": 1,
            "status": "Paid",
            "accounting_booked": 0,
            "b2c_payment_disbursement_reference": ["is", "set"],
            "creation": [">=", add_days(nowdate(), -RETRY_WINDOW_DAYS)],
        },
        pluck="name",
        order_by="accounting_attempted_on asc, creation asc",
        limit=limit,
    )
    for name in names:
        try:
            frappe.db.set_value(
                B2C_REQUEST_DOCTYPE, name, "accounting_attempted_on", now_datetime(), update_modified=False
            )
            update_b2c_reference_status(name, enqueue_next=True)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            _record_booking_error(name, frappe.get_traceback())
            frappe.db.commit()


def payout_receipt(ref) -> str:
    """The payout's own M-Pesa receipt, copied to the row from its request when it is Paid.
    A row paid before the suite may lack the copy: then its Paid request's receipt."""
    receipt = (getattr(ref, "reference_no", None) or "").strip()
    request = getattr(ref, "b2c_disbursement_request", None)
    if not receipt and request:
        paid = frappe.db.get_value(B2C_REQUEST_DOCTYPE, request, ["status", "transaction_id"], as_dict=True)
        if paid and paid.status == "Paid":
            receipt = (paid.transaction_id or "").strip()
    return receipt


def entry_carrying_receipt(ref):
    """A live entry that carries this payout's receipt though it does not book this row: made
    before the suite as an unallocated advance, on another document (an order's payout booked
    on the invoice made from it, not as its advance), or as a Journal Entry (Bank
    Reconciliation's Create Voucher copies the receipt into Cheque No). ``(doctype, name,
    docstatus)`` or None. The receipt is in the books then: nothing more may be booked for it."""
    receipt = payout_receipt(ref)
    if not receipt:
        return None
    stores = [("Payment Entry", "reference_no"), ("Journal Entry", "cheque_no")]
    if ref.reference_doctype == "Salary Slip":
        stores.reverse()  # a salary payout is booked by Journal Entry
    for doctype, field in stores:
        entry = frappe.db.get_value(doctype, {field: receipt, "docstatus": ["<", 2]}, ["name", "docstatus"],
                                    as_dict=True, order_by="docstatus desc")
        if entry:
            return (doctype, entry.name, int(entry.docstatus))
    return None


def _entry_documents(doctype: str, name: str) -> list:
    """What the entry is allocated to, as "<doctype> <name>" (empty: an unallocated advance)."""
    if doctype == "Payment Entry":
        rows = frappe.get_all("Payment Entry Reference", filters={"parent": name},
                              fields=["reference_doctype", "reference_name"])
        return [f"{r.reference_doctype} {r.reference_name}" for r in rows if r.reference_name]
    rows = frappe.get_all("Journal Entry Account", filters={"parent": name}, fields=["reference_type", "reference_name"])
    return [f"{r.reference_type} {r.reference_name}" for r in rows if r.reference_name]


def _mark_booked_elsewhere(ref, doctype: str, name: str, docstatus: int) -> None:
    """Book nothing for a payout whose receipt is already in the books, and tell staff once.

    The payout stays unbooked (``accounting_booked`` 0): the sweep looks again each pass,
    quietly, and books it once that entry no longer carries the receipt (cancelled or amended
    because it was another payout's), or marks it booked once an entry for this document
    carries it. Meanwhile the double-payout guard counts it, so the document cannot be paid
    again."""
    request = ref.b2c_disbursement_request
    if not request or not _has_field("accounting_booked"):
        return
    target = f"{ref.reference_doctype} {ref.reference_name}"
    on = _entry_documents(doctype, name)
    if not on:
        action = f"it is not allocated: allocate it to {target}"
    else:
        action = (
            f"it is on {', '.join(on[:3])}: if that is what this payout paid, nothing more is needed (new payouts "
            f"for {target} stay blocked); if the entry is another payout's, correct it and this payout is booked "
            "on the next pass"
        )
    note = (
        f"M-Pesa receipt {payout_receipt(ref)} is already on {doctype} {name}"
        f"{' (a draft: submit it)' if docstatus == 0 else ''}; {action}."
    )[:500]
    if frappe.db.get_value(B2C_REQUEST_DOCTYPE, request, "accounting_error") != note:
        frappe.log_error(title=f"B2C payout {request} booked elsewhere"[:140], message=note)
    frappe.db.set_value(
        B2C_REQUEST_DOCTYPE, request, {"accounting_booked": 0, "accounting_error": note}, update_modified=False
    )


def already_booked(disbursement_name: str, ref) -> bool:
    """True when a live (draft or submitted) entry already books this reference row."""
    return booked_docstatus(disbursement_name, ref) is not None


def booked_docstatus(disbursement_name: str, ref, for_update: bool = False) -> int | None:
    """Highest docstatus of the live entry booking this row: 1 submitted, 0 draft, None none.

    Keyed on the payout's receipt, which the entry carries (Payment Entry Reference No,
    Journal Entry Cheque No): several rows of one disbursement may pay the same document
    (an amount over the B2C limit split, one claim paid to many numbers), and each row is
    its own payout with its own entry. Not on the disbursement link, so an entry made
    before the suite or by hand for this receipt is found too.

    ``for_update``: every read is a locking read, which sees what is committed now (and waits
    for a cancel or submit still in flight) instead of this transaction's snapshot. The
    retry guard passes it: an entry cancelled after the snapshot must not count as booked.
    """
    receipt = payout_receipt(ref)
    if not receipt:
        return None
    if ref.reference_doctype == "Salary Slip":
        # a Journal Entry names the employee, not the slip
        entries = frappe._dict(
            live="""FROM `tabJournal Entry` je
            JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
            WHERE jea.party = %s AND je.docstatus < 2""",
            key=(ref.party,),
            alias="je",
            ref_no="je.cheque_no",
            amount="jea.debit_in_account_currency",
            tag="jea.b2c_payment_disbursement",
            rows={"reference_doctype": "Salary Slip", "party": ref.party},
        )
    else:
        # The entry must also pay this document: one booked for the receipt but against
        # another document (or none) leaves this one owing, so it must not count as booked.
        # An advance on an order that ERPNext later allocated to an invoice (Get Advances,
        # Payment Reconciliation) keeps the order only as the row's advance voucher.
        on_document = "per.reference_doctype = %s AND per.reference_name = %s"
        key = (ref.reference_doctype, ref.reference_name)
        if frappe.db.has_column("Payment Entry Reference", "advance_voucher_no"):
            on_document = f"(({on_document}) OR (per.advance_voucher_type = %s AND per.advance_voucher_no = %s))"
            key = key * 2
        entries = frappe._dict(
            live=f"""FROM `tabPayment Entry Reference` per
            JOIN `tabPayment Entry` pe ON pe.name = per.parent
            WHERE {on_document} AND pe.docstatus < 2""",
            key=key,
            alias="pe",
            ref_no="pe.reference_no",
            amount="per.allocated_amount",
            tag="per.b2c_payment_disbursement",
            rows={"reference_doctype": ref.reference_doctype, "reference_name": ref.reference_name},
            # payment_entry_service writes the payout's own receipt on every entry it makes
            receipt_is_own=True,
        )
    entries.locking = "FOR UPDATE" if for_update and frappe.db.db_type == "mariadb" else ""
    entries.for_update = for_update
    rows = frappe.db.sql(
        f"SELECT MAX({entries.alias}.docstatus) {entries.live} AND {entries.ref_no} = %s {entries.locking}",
        (*entries.key, receipt),
    )
    value = rows[0][0] if rows else None
    if value is not None:
        return int(value)
    return _spare_entry_docstatus(disbursement_name, ref, receipt, entries)


def _disbursement_rows(filters: dict, fields: list, for_update: bool = False) -> list:
    """Reference rows; ``for_update``: a locking read (committed now, not the snapshot)."""
    if for_update:
        return frappe.db.get_values(
            "B2C Payment Disbursement Reference", filters, fields, as_dict=True, for_update=True
        )
    return frappe.get_all("B2C Payment Disbursement Reference", filters=filters, fields=fields)


def _spare_entry_docstatus(disbursement_name: str, ref, receipt: str, entries) -> int | None:
    """No entry carries the receipt, but one this flow made for the disbursement may still be
    this payout's: a draft made without it (Journal Entries made before journal_service set
    the receipt as Cheque No), or one staff submitted with another Reference No or as
    another voucher type.

    Such entries (tagged with the disbursement, for this document or employee and of this
    payout's amount, and not another payout's by its receipt) are shared among the
    disbursement's payouts of that amount with no entry by their receipt: none is booked
    twice, and this payout is booked once there is one for each of them.

    A Payment Entry that carries a receipt of one of the disbursement's payouts is that
    payout's (a duplicate at most), never a spare: Payment Entries get their payout's receipt
    when they are made, so only a Journal Entry may carry another payout's receipt (a legacy
    stamp script that picks a receipt by amount). Counting such a duplicate for this payout
    would mark it booked with no entry of its own, and cancelling the duplicate would then
    leave the document owing what was paid.
    """
    amount = flt(ref.allocated_amount, 2)
    paid = _disbursement_rows(
        {"parent": disbursement_name, "payment_status": "Paid", **entries.rows},
        ["reference_no", "allocated_amount"],
        entries.for_update,
    )
    receipts = {_receipt_key(receipt)} | {
        _receipt_key(r.reference_no) for r in paid if payout_receipt(r) and flt(r.allocated_amount, 2) == amount
    }
    with_entry = {
        _receipt_key(no)
        for (no,) in frappe.db.sql(
            f"SELECT DISTINCT {entries.ref_no} {entries.live} AND {entries.ref_no} IN %s {entries.locking}",
            (*entries.key, tuple(receipts)),
        )
    }
    spare = sorted(
        (
            (int(docstatus), _receipt_key(no))
            for _, docstatus, no, entry_amount in frappe.db.sql(
                f"""SELECT DISTINCT {entries.alias}.name, {entries.alias}.docstatus, {entries.ref_no},
                {entries.amount} {entries.live} AND {entries.tag} = %s {entries.locking}""",
                (*entries.key, disbursement_name),
            )
            if flt(entry_amount, 2) == amount
        ),
        reverse=True,  # submitted first
    )
    if entries.receipt_is_own:
        payouts = {
            _receipt_key(r.reference_no)
            for r in _disbursement_rows({"parent": disbursement_name}, ["reference_no"], entries.for_update)
            if (r.reference_no or "").strip()
        }
        spare = [e for e in spare if e[1] not in payouts]
    for no in with_entry:  # that payout's own entry, not a spare one
        own = next((e for e in spare if e[1] == no), None)
        if own:
            spare.remove(own)
    waiting = len(receipts - with_entry)  # this payout among them
    # submitted only when each of them can have a submitted one
    return spare[waiting - 1][0] if len(spare) >= waiting else None


def _receipt_key(value) -> str:
    return (value or "").strip().upper()
