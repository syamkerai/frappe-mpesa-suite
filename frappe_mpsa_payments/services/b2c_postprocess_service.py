import frappe

from frappe_mpsa_payments.accounting.journal_service import create_journal_entry
from frappe_mpsa_payments.accounting.loan_disbursement_service import (
    create_loan_disbursement,
)
from frappe_mpsa_payments.accounting.payment_entry_service import (
    create_payment_entry_for_doc,
)
from frappe_mpsa_payments.utils.doctype_names import (
    B2C_REQUEST_DOCTYPE,
    MPESA_SETTINGS_DOCTYPE,
    STANBIC_SETTINGS_DOCTYPE,
)


def handle_successful_payment(disbursement_name: str, reference_name: str) -> None:
    """
    After reference row is updated, create accounting entries:
    - Salary Slip → Journal Entry
    - Others → Payment Entry
    - Loan → Loan Disbursement
    """
    # Lock the row so a re-sent Result callback cannot book the same payout twice.
    frappe.db.get_value(
        "B2C Payment Disbursement Reference", reference_name, "name", for_update=True
    )
    ref = frappe.get_doc("B2C Payment Disbursement Reference", reference_name)
    if already_booked(disbursement_name, ref):
        return

    b2c_req = frappe.get_doc(B2C_REQUEST_DOCTYPE, ref.b2c_disbursement_request)
    b2c_disb = frappe.get_doc(ref.parenttype, disbursement_name)

    if b2c_req.mpesa_settings:
        settings_doctype = MPESA_SETTINGS_DOCTYPE
        settings_name = b2c_req.mpesa_settings
    elif b2c_req.stanbic_settings:
        settings_doctype = STANBIC_SETTINGS_DOCTYPE
        settings_name = b2c_req.stanbic_settings
    else:
        frappe.log_error(
            f"No provider settings found on B2C Request {b2c_req.name}",
            "B2C Postprocess",
        )
        return

    settings = frappe.get_doc(settings_doctype, settings_name)
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
    elif doctype == "Loan":
        create_loan_disbursement(b2c_disb, ref, submit)
    else:
        frappe.log_error(
            f"No post-process for reference {doctype}", "B2C Postprocess Error"
        )


def already_booked(disbursement_name: str, ref) -> bool:
    """True when a live (draft or submitted) entry already books this reference row."""
    if ref.reference_doctype == "Salary Slip":
        return bool(
            frappe.db.sql(
                """SELECT 1 FROM `tabJournal Entry Account` jea
                JOIN `tabJournal Entry` je ON je.name = jea.parent
                WHERE jea.b2c_payment_disbursement = %s AND jea.party = %s
                AND je.docstatus < 2 LIMIT 1""",
                (disbursement_name, ref.party),
            )
        )
    if ref.reference_doctype == "Loan":
        return bool(
            frappe.db.exists(
                "Loan Disbursement",
                {"b2c_payment_disbursement": disbursement_name, "against_loan": ref.reference_name, "docstatus": ["<", 2]},
            )
        )
    return bool(
        frappe.db.sql(
            """SELECT 1 FROM `tabPayment Entry Reference` per
            JOIN `tabPayment Entry` pe ON pe.name = per.parent
            WHERE per.b2c_payment_disbursement = %s AND per.reference_doctype = %s
            AND per.reference_name = %s AND pe.docstatus < 2 LIMIT 1""",
            (disbursement_name, ref.reference_doctype, ref.reference_name),
        )
    )
