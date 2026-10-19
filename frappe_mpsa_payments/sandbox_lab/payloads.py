"""Payloads the lab sends to Daraja's sandbox or replays / forges against our own callback URLs.

Pure Python (no Frappe import) so it is unit-tested without a bench.
"""

import random
import string
from datetime import datetime

C2B_SIMULATE_V2 = "/mpesa/c2b/v2/simulate"
C2B_SIMULATE_V1 = "/mpesa/c2b/v1/simulate"
DARAJA_TEST_MSISDN = "254708374149"
FAKE_RECEIPT_PREFIX = "ZZ"  # real M-Pesa receipts never start with ZZ; easy to spot in reports


def fake_receipt(rng=random) -> str:
    """A 10-character M-Pesa-looking receipt that Safaricom has never issued."""
    return FAKE_RECEIPT_PREFIX + "".join(rng.choice(string.ascii_uppercase + string.digits) for _ in range(8))


def lab_reference(tag: str, rng=random) -> str:
    """A bill reference no document matches (unknown-reference test). 12 chars max."""
    return ("LAB" + tag.upper() + "".join(rng.choice(string.digits) for _ in range(6)))[:12]


def typo_variant(ref: str) -> str:
    """How customers mistype invoice numbers: no separators, lower case (SINV-26-00012 -> sinv2600012)."""
    out = "".join(c for c in str(ref) if c.isalnum()).lower()
    return out if out != str(ref) else out.upper()


def daraja_time(when: datetime | None = None) -> str:
    return (when or datetime.now()).strftime("%Y%m%d%H%M%S")


def c2b_simulate(shortcode, amount, msisdn=DARAJA_TEST_MSISDN, bill_ref=None, paybill=True) -> dict:
    """Body for Daraja's sandbox-only C2B simulate (a customer paying the paybill or till)."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Amount must be a positive whole number of shillings")
    body = {
        "ShortCode": str(shortcode),
        "CommandID": "CustomerPayBillOnline" if paybill else "CustomerBuyGoodsOnline",
        "Amount": amount,
        "Msisdn": str(msisdn),
    }
    body["BillRefNumber"] = str(bill_ref or "") if paybill else None
    return body


def c2b_confirmation(transid, amount, shortcode, bill_ref="", msisdn=DARAJA_TEST_MSISDN, when=None,
                     first_name="Sandbox", transaction_type="Pay Bill") -> dict:
    """A C2B confirmation exactly as Safaricom posts it to the ConfirmationURL."""
    return {
        "TransactionType": transaction_type,
        "TransID": transid,
        "TransTime": daraja_time(when),
        "TransAmount": f"{float(amount):.2f}",
        "BusinessShortCode": str(shortcode),
        "BillRefNumber": bill_ref or "",
        "InvoiceNumber": "",
        "OrgAccountBalance": "",
        "ThirdPartyTransID": "",
        "MSISDN": str(msisdn),
        "FirstName": first_name,
        "MiddleName": "",
        "LastName": "",
    }


def confirmation_from_register(row: dict) -> dict:
    """Rebuild the confirmation Safaricom sent from a stored Mpesa C2B Payment Register."""
    get = row.get
    return {
        "TransactionType": get("transactiontype") or "Pay Bill",
        "TransID": get("transid"),
        "TransTime": get("transtime") or "",
        "TransAmount": f"{float(get('transamount') or 0):.2f}",
        "BusinessShortCode": get("businessshortcode"),
        "BillRefNumber": get("billrefnumber") or "",
        "InvoiceNumber": get("invoicenumber") or "",
        "OrgAccountBalance": get("orgaccountbalance") or "",
        "ThirdPartyTransID": get("thirdpartytransid") or "",
        "MSISDN": get("msisdn") or "",
        "FirstName": get("firstname") or "",
        "MiddleName": get("middlename") or "",
        "LastName": get("lastname") or "",
    }


def stk_callback(checkout_request_id, merchant_request_id="", result_code=0, amount=None, receipt=None,
                 phone=None, when=None) -> dict:
    """An STK callback body as Safaricom posts it to the CallBackURL."""
    result_code = int(result_code)
    callback = {
        "MerchantRequestID": merchant_request_id or "",
        "CheckoutRequestID": checkout_request_id,
        "ResultCode": result_code,
        "ResultDesc": "The service request is processed successfully." if result_code == 0
        else "Request cancelled by user",
    }
    if result_code == 0:
        items = [
            {"Name": "Amount", "Value": float(amount or 0)},
            {"Name": "MpesaReceiptNumber", "Value": receipt or fake_receipt()},
            {"Name": "TransactionDate", "Value": int(daraja_time(when))},
            {"Name": "PhoneNumber", "Value": int(phone) if phone else 254708374149},
        ]
        callback["CallbackMetadata"] = {"Item": items}
    return {"Body": {"stkCallback": callback}}


def stk_body(callback_payload: dict) -> dict:
    """Wrap a stored stkCallback dict (Mpesa Express Request.callback_payload) for re-posting."""
    inner = callback_payload.get("Body", {}).get("stkCallback") if "Body" in callback_payload else callback_payload
    return {"Body": {"stkCallback": inner}}
