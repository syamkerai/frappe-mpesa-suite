import frappe
from frappe.utils import nowdate

from mpsa_suite.money_out.common import document_dimensions, payout_cost_center
from mpsa_suite.mpsa_suite.api.payment_entry import (
    create_payment_entry,
)
from mpsa_suite.wallet.accounts import b2c_utility_gl


def _not_taken(request, problem: str) -> None:
    """The claim or advance took only part (or none) of a B2C payout: a possible double payment.
    A Comment on the payout and an alert to the Notify Role (M-Pesa Suite Settings), once: the entry
    is made now, so the booking is not tried again. Never raises."""
    from mpsa_suite.money_out.party_accounts import alert

    doc = frappe._dict(doctype="B2C Disbursement Request", name=request)
    message = f"{request}: {problem}"
    try:
        if request:
            frappe.get_doc("B2C Disbursement Request", request).add_comment("Comment", problem)
    except Exception:
        frappe.log_error(title="M-Pesa B2C booking comment failed", message=f"{request}\n{frappe.get_traceback()}")
    frappe.log_error(title="M-Pesa B2C payout not all on its document", message=message)
    alert(message, doc)


def create_payment_entry_for_doc(b2c_disbursement, b2c_disbursement_ref, submit=False):
    """
    Create a Payment Entry for various reference doctypes.

    Args:
        b2c_disbursement: B2C Payment Disbursement document
        b2c_disbursement_ref: B2C Payment Disbursement Reference document
        submit: whether to submit the Payment Entry
    """
    current_user = frappe.session.user
    frappe.set_user("Administrator")

    try:
        # Gather parameters
        party_type = b2c_disbursement_ref.party_type
        party_account = b2c_disbursement.paid_to
        party = frappe.db.get_value(party_type, b2c_disbursement_ref.party, "name")
        # An Expense Claim or Employee Advance: its own employee and Payable (or Advance) Account,
        # which ERPNext and HRMS check against the reference, whoever M-Pesa paid
        from mpsa_suite.money_out.employee_documents import booking_party

        booking = booking_party(
            b2c_disbursement_ref.reference_doctype, b2c_disbursement_ref.reference_name
        )
        if booking:
            party_type, party, party_account = "Employee", booking.employee, booking.account
        amount = b2c_disbursement_ref.allocated_amount
        company = b2c_disbursement.company
        currency = frappe.db.get_value("Company", company, "default_currency")
        mode_of_payment = b2c_disbursement.mode_of_payment
        reference_date = b2c_disbursement_ref.reference_date
        reference_no = b2c_disbursement_ref.reference_no
        dt, dn = b2c_disbursement_ref.reference_doctype, b2c_disbursement_ref.reference_name
        request = getattr(b2c_disbursement_ref, "b2c_disbursement_request", None)
        settings = request and frappe.db.get_value("B2C Disbursement Request", request, "mpesa_settings")

        references = [
            {
                "reference_doctype": b2c_disbursement_ref.reference_doctype,
                "reference_name": b2c_disbursement_ref.reference_name,
                "allocated_amount": amount,
                "b2c_payment_disbursement": b2c_disbursement.name,
            }
        ]
        problem = None
        if booking:
            # The claim or advance takes what HRMS says it still owes (read last, a locking read), as a
            # B2B payout's entry does: paid in part another way since the send (a bank entry), the rest
            # stays on the entry unallocated and staff are told, instead of ERPNext refusing the entry
            # ("Allocated Amount cannot be greater than outstanding") on every sweep.
            from mpsa_suite.money_out.b2b_entries import employee_reference_rows

            rows, problem = employee_reference_rows(dt, dn, amount, booking.employee)
            references = [dict(row, b2c_payment_disbursement=b2c_disbursement.name) for row in rows]

        create_payment_entry(
            company,
            party,
            amount,
            currency,
            mode_of_payment,
            party_type=party_type,
            reference_date=reference_date,
            reference_no=reference_no,
            posting_date=nowdate(),
            # the document's cost center and project, as the B2B entries (handover F5)
            cost_center=payout_cost_center(settings, company, dt, dn),
            dimensions=document_dimensions(dt, dn),
            submit=submit,
            references=references,
            party_account=party_account,
            bank_gl=b2c_utility_gl(b2c_disbursement_ref),
        )
        if problem:
            _not_taken(request, problem)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Payment Entry Service Error")
        raise  # the caller rolls back and the B2C accounting sweep retries
    finally:
        frappe.set_user(current_user)
