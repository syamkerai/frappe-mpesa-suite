import frappe

from frappe_mpsa_payments.daraja.callbacks import with_token
from frappe.utils import flt, get_url

from frappe_mpsa_payments.connectors.factory import get_b2c_connector
from frappe_mpsa_payments.utils.doctype_names import B2C_REQUEST_DOCTYPE
from frappe_mpsa_payments.utils.phone import normalize_msisdn


def validate_b2c_amount_and_party(doc) -> None:
    """Daraja B2C takes whole shillings and a 2547/2541 MSISDN; refuse rather than round."""
    amount = flt(doc.amount)
    if amount <= 0:
        frappe.throw(f"B2C amount must be positive, got {doc.amount}")
    if doc.mpesa_settings:
        if amount != int(amount):
            frappe.throw(f"M-Pesa B2C amount must be whole shillings, got {doc.amount}")
        msisdn = normalize_msisdn(doc.phone_number)
        if not msisdn:
            frappe.throw(f"Invalid M-Pesa phone number {doc.phone_number!r} on {doc.name}")
        if msisdn != doc.phone_number:
            doc.db_set("phone_number", msisdn, update_modified=False)


def create_b2c_request(doc_name: str) -> dict:
    """
    Create and send a B2C Request.
    1) Loads the Disbursement Request.
    2) Picks the right connector via factory.
    3) Builds the callback URL.
    4) Dispatches the B2C payment request and returns the raw JSON.
    """
    # Row lock: two workers (submit + retry, or a double click) cannot both send.
    frappe.db.get_value(B2C_REQUEST_DOCTYPE, doc_name, "name", for_update=True)
    doc = frappe.get_doc(B2C_REQUEST_DOCTYPE, doc_name)
    if doc.docstatus != 1:
        frappe.throw(f"B2C request {doc_name} must be submitted before it is sent")
    if doc.status not in ("Not Initiated", "", None):
        frappe.throw(f"B2C request {doc_name} is {doc.status}; only unsent requests are sent")
    validate_b2c_amount_and_party(doc)

    if not doc.mpesa_settings:
        frappe.throw(f"Select the Mpesa Settings to pay from on {doc_name}")
    provider = "Mpesa"
    settings_name = doc.mpesa_settings

    callback_url = with_token(
        f"{get_url()}/api/method/frappe_mpsa_payments.frappe_mpsa_payments.api.b2c.b2c_results_callback"
        f"?provider={provider}"
    )
    connector = get_b2c_connector(provider, settings_name)
    return connector.send_b2c_request(doc, callback_url)
