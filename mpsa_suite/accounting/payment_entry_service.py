import frappe
from frappe.utils import nowdate

from mpsa_suite.money_out.common import document_dimensions, payout_cost_center
from mpsa_suite.mpsa_suite.api.payment_entry import (
    create_payment_entry,
)
from mpsa_suite.wallet.accounts import b2c_utility_gl


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
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Payment Entry Service Error")
        raise  # the caller rolls back and the B2C accounting sweep retries
    finally:
        frappe.set_user(current_user)
