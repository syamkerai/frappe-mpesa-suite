import frappe

from frappe_mpsa_payments.accounting.journal_service import create_journal_entry
from frappe_mpsa_payments.accounting.payment_entry_service import (
    create_payment_entry_for_doc,
)
from frappe_mpsa_payments.utils.doctype_names import (
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
    if not already_booked(disbursement_name, ref):
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
        frappe.log_error(traceback, f"B2C accounting failed for {b2c_request}"[:140])
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


def already_booked(disbursement_name: str, ref) -> bool:
    """True when a live (draft or submitted) entry already books this reference row."""
    return booked_docstatus(disbursement_name, ref) is not None


def booked_docstatus(disbursement_name: str, ref) -> int | None:
    """Highest docstatus of the live entry booking this row: 1 submitted, 0 draft, None none."""
    if ref.reference_doctype == "Salary Slip":
        rows = frappe.db.sql(
            """SELECT MAX(je.docstatus) FROM `tabJournal Entry Account` jea
            JOIN `tabJournal Entry` je ON je.name = jea.parent
            WHERE jea.b2c_payment_disbursement = %s AND jea.party = %s
            AND je.docstatus < 2""",
            (disbursement_name, ref.party),
        )
    else:
        rows = frappe.db.sql(
            """SELECT MAX(pe.docstatus) FROM `tabPayment Entry Reference` per
            JOIN `tabPayment Entry` pe ON pe.name = per.parent
            WHERE per.b2c_payment_disbursement = %s AND per.reference_doctype = %s
            AND per.reference_name = %s AND pe.docstatus < 2""",
            (disbursement_name, ref.reference_doctype, ref.reference_name),
        )
    value = rows[0][0] if rows else None
    return None if value is None else int(value)
