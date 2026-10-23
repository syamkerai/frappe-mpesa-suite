import frappe

from frappe_mpsa_payments.daraja.callbacks import KIND_RESULT, safaricom_callback

from frappe_mpsa_payments.services.b2c_request_service import create_b2c_request
from frappe_mpsa_payments.services.b2c_response_service import process_b2c_result


def make_b2c_payment_request(doc_name: str) -> dict:
    # Not whitelisted: it sends money. Reached only from B2C Disbursement Request
    # on_submit / retry_failed_payment, which check state and roles first.
    """
    Queues and initiates a B2C payment request by creating the
    B2C Request document and delegating to the appropriate connector.
    """
    return create_b2c_request(doc_name)


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_RESULT)
def b2c_results_callback(**kwargs):
    """
    Handle the B2C callback from Daraja.
    """

    try:
        result_payload = kwargs.get("Result") or {}
        process_b2c_result(None, result_payload)
        return "OK"

    except Exception:
        frappe.log_error(frappe.get_traceback(), "B2C Callback Error")
        raise
