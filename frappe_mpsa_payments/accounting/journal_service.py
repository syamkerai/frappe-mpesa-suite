import frappe
from frappe.utils import getdate, nowdate


def create_journal_entry(b2c_disbursement, b2c_disbursement_ref, submit=False):
    """
    Create a Journal Entry for Salary Slip after successful B2C payment.

    Args:
        b2c_disbursement: B2C Payment Disbursement document
        b2c_disbursement_ref: B2C Payment Disbursement Reference document
        submit: whether to submit the Journal Entry after inserting
    """
    try:
        salary_slip = frappe.get_doc("Salary Slip", b2c_disbursement_ref.reference_name)
        if not salary_slip:
            frappe.log_error(
                f"Salary Slip {b2c_disbursement_ref.reference_name} not found",
                "Journal Service",
            )

        if not b2c_disbursement.paid_from:
            frappe.throw("Paid From account is not set in B2C Payment Disbursement")
        if not b2c_disbursement.paid_to:
            frappe.log_error(
                "Paid To Account is not set in B2C Payment Disbursement",
                "Journal Service",
            )

        je = frappe.new_doc("Journal Entry")
        je.voucher_type = "Bank Entry"
        je.posting_date = nowdate()
        je.company = b2c_disbursement.company
        je.user_remark = f"B2C Payment for Salary Slip {salary_slip.name}"
        receipt = (b2c_disbursement_ref.reference_no or "").strip()
        if receipt:
            # The payout's M-Pesa receipt is the entry's Reference No: ERPNext will not submit
            # a Bank Entry without Reference No and Date (validate_cheque_info), and booking
            # finds a salary payout's entry by it (b2c_postprocess_service.booked_docstatus).
            je.cheque_no = receipt
            je.cheque_date = getdate(b2c_disbursement_ref.reference_date or je.posting_date)

        # Credit the paid_from account
        je.append(
            "accounts",
            {
                "account": b2c_disbursement.paid_from,
                "credit_in_account_currency": b2c_disbursement_ref.allocated_amount,
            },
        )

        # Debit the paid_to account (employee)
        je.append(
            "accounts",
            {
                "account": b2c_disbursement.paid_to,
                "debit_in_account_currency": b2c_disbursement_ref.allocated_amount,
                "party_type": b2c_disbursement_ref.party_type,
                "party": b2c_disbursement_ref.party,
                "reference_type": "Payroll Entry",
                "reference_name": b2c_disbursement_ref.payroll_entry,
                "b2c_payment_disbursement": b2c_disbursement.name,
            },
        )

        je.insert(ignore_permissions=True)
        if submit:
            je.submit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Journal Service Error")
        raise  # the caller rolls back and the B2C accounting sweep retries
