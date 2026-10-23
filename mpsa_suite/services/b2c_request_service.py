import frappe

from mpsa_suite.daraja.callbacks import with_token
from frappe.utils import flt, get_url

from mpsa_suite.connectors.factory import get_b2c_connector
from mpsa_suite.daraja import apis
from mpsa_suite.daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT
from mpsa_suite.utils.doctype_names import B2C_REQUEST_DOCTYPE
from mpsa_suite.utils.phone import normalize_msisdn


def validate_b2c_amount_and_party(doc) -> None:
    """Daraja B2C takes whole shillings from 10 to 250,000 and a 2547/2541 MSISDN; refuse rather than round."""
    amount = flt(doc.amount)
    if amount <= 0:
        frappe.throw(f"B2C amount must be positive, got {doc.amount}")
    if doc.mpesa_settings:
        if amount != int(amount):
            frappe.throw(f"M-Pesa B2C amount must be whole shillings, got {doc.amount}")
        if not B2C_MIN_AMOUNT <= amount <= B2C_MAX_AMOUNT:
            frappe.throw(
                f"M-Pesa B2C amount must be between {B2C_MIN_AMOUNT} and {B2C_MAX_AMOUNT:,}, got {doc.amount}"
            )
        msisdn = normalize_msisdn(doc.phone_number)
        if not msisdn:
            frappe.throw(f"Invalid M-Pesa phone number {doc.phone_number!r} on {doc.name}")
        if msisdn != doc.phone_number:
            doc.db_set("phone_number", msisdn, update_modified=False)


def create_b2c_request(doc_name: str) -> dict:
    """
    Create and send a B2C Request.
    1) Loads the Disbursement Request and checks it may be sent (ready_to_send).
    2) Picks the right connector via factory.
    3) Builds the callback URL.
    4) Dispatches the B2C payment request and returns the raw JSON.
    """
    return send(ready_to_send(doc_name))


def ready_to_send(doc_name: str):
    """The request, once every check before its send passed (only throws, before anything leaves):
    submitted, not sent yet, amount and number, a payment schedule's rules, the shortcode."""
    # Row lock: two workers (submit + retry, or a double click) cannot both send.
    frappe.db.get_value(B2C_REQUEST_DOCTYPE, doc_name, "name", for_update=True)
    doc = frappe.get_doc(B2C_REQUEST_DOCTYPE, doc_name)
    if doc.docstatus != 1:
        frappe.throw(f"B2C request {doc_name} must be submitted before it is sent")
    if doc.status not in ("Not Initiated", "", None):
        frappe.throw(f"B2C request {doc_name} is {doc.status}; only unsent requests are sent")
    validate_b2c_amount_and_party(doc)
    # a payout of a payment schedule line: never before its planned day, and only to the line's
    # approved phone (money_out.schedule)
    from mpsa_suite.money_out.schedule import assert_b2c_line_due

    assert_b2c_line_due(doc)

    if not doc.mpesa_settings:
        frappe.throw(f"Select the Mpesa Settings to pay from on {doc_name}")
    apis.require(doc.mpesa_settings, apis.B2C)
    return doc


def send(doc) -> dict:
    """Post a checked request (ready_to_send) to M-Pesa; the connector records the ack."""
    provider = "Mpesa"
    settings_name = doc.mpesa_settings

    # No "?provider=Mpesa": Daraja bans "mpesa" in callback URLs, and the handler ignores it.
    callback_url = with_token(
        f"{get_url()}/api/method/mpsa_suite.mpsa_suite.api.b2c.b2c_results_callback"
    )
    connector = get_b2c_connector(provider, settings_name)
    return connector.send_b2c_request(doc, callback_url)
