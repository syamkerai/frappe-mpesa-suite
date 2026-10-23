from mpsa_suite.daraja.verify import (
    FAILED,
    MISMATCH,
    PENDING,
    VERIFIED,
    c2b_verification,
    credit_shortcode,
    parse_stk_callback,
    stk_verification,
)
from mpsa_suite.utils.payouts import owed

# ---------------------------------------------------------------- STK
PAID_CALLBACK = {
    "MerchantRequestID": "29115-34620561-1",
    "CheckoutRequestID": "ws_CO_191220191020363925",
    "ResultCode": 0,
    "ResultDesc": "The service request is processed successfully.",
    "CallbackMetadata": {
        "Item": [
            {"Name": "Amount", "Value": 100.00},
            {"Name": "MpesaReceiptNumber", "Value": "NLJ7RT61SV"},
            {"Name": "Balance"},
            {"Name": "TransactionDate", "Value": 20191219102115},
            {"Name": "PhoneNumber", "Value": 254708374149},
        ]
    },
}
CANCELLED_CALLBACK = {
    "MerchantRequestID": "29115-34620561-1",
    "CheckoutRequestID": "ws_CO_191220191020363925",
    "ResultCode": 1032,
    "ResultDesc": "Request cancelled by user",
}


def test_parse_stk_callback_success_and_missing_values():
    p = parse_stk_callback(PAID_CALLBACK)
    assert p["ok"] and p["result_code"] == "0"
    assert p["receipt"] == "NLJ7RT61SV"
    assert p["amount"] == 100.0
    assert p["transaction_date"] == "20191219102115"


def test_parse_stk_callback_failure_has_no_metadata():
    p = parse_stk_callback(CANCELLED_CALLBACK)
    assert not p["ok"] and p["result_code"] == "1032"
    assert p["receipt"] is None and p["amount"] is None


def test_paid_callback_confirmed_by_query():
    outcome, _ = stk_verification(parse_stk_callback(PAID_CALLBACK), {"ResultCode": "0"}, 100)
    assert outcome == VERIFIED


def test_forged_paid_callback_contradicted_by_query():
    outcome, reason = stk_verification(parse_stk_callback(PAID_CALLBACK), {"ResultCode": "1032"}, 100)
    assert outcome == MISMATCH and "1032" in reason


def test_paid_callback_with_wrong_amount_is_not_booked():
    outcome, _ = stk_verification(parse_stk_callback(PAID_CALLBACK), {"ResultCode": 0}, 5000)
    assert outcome == MISMATCH


def test_rounded_prompt_amount_within_tolerance():
    # the prompt was KES 99.6, STK sent the whole shilling 100
    outcome, _ = stk_verification(parse_stk_callback(PAID_CALLBACK), {"ResultCode": 0}, 99.6, tolerance=1)
    assert outcome == VERIFIED


def test_query_without_answer_is_pending():
    parsed = parse_stk_callback(PAID_CALLBACK)
    assert stk_verification(parsed, None, 100)[0] == PENDING
    assert stk_verification(parsed, {"errorCode": "500.001.1001", "errorMessage": "being processed"}, 100)[0] == PENDING
    assert stk_verification(parsed, {"ResultCode": "4999"}, 100)[0] == PENDING


def test_failed_callback_confirmed_failed():
    assert stk_verification(parse_stk_callback(CANCELLED_CALLBACK), {"ResultCode": "1032"}, 100)[0] == FAILED


def test_failed_callback_but_query_paid_is_mismatch():
    assert stk_verification(parse_stk_callback(CANCELLED_CALLBACK), {"ResultCode": "0"}, 100)[0] == MISMATCH


# ---------------------------------------------------------------- C2B
CONFIRMATION = {"transid": "RKL51ZDR4F", "transamount": 500.0, "businessshortcode": "600966"}


def status_result(receipt="RKL51ZDR4F", amount="500.00", status="Completed", credit="600966 - Astral Ltd", code=0):
    return {
        "ResultCode": code,
        "ResultDesc": "The service request is processed successfully.",
        "ConversationID": "AG_1",
        "ResultParameters": {
            "ResultParameter": [
                {"Key": "DebitPartyName", "Value": "254708374149 - John Doe"},
                {"Key": "CreditPartyName", "Value": credit},
                {"Key": "TransactionStatus", "Value": status},
                {"Key": "Amount", "Value": amount},
                {"Key": "ReceiptNo", "Value": receipt},
                {"Key": "TransactionReason"},
            ]
        },
    }


def test_c2b_confirmed():
    assert c2b_verification(CONFIRMATION, status_result(), {"600966"})[0] == VERIFIED


def test_c2b_receipt_case_insensitive():
    assert c2b_verification({**CONFIRMATION, "transid": "rkl51zdr4f"}, status_result(), {"600966"})[0] == VERIFIED


def test_c2b_unknown_shortcode():
    outcome, reason = c2b_verification({**CONFIRMATION, "businessshortcode": "999999"}, status_result(), {"600966"})
    assert outcome == MISMATCH and "999999" in reason


def test_c2b_forged_amount():
    assert c2b_verification({**CONFIRMATION, "transamount": 50000}, status_result(), {"600966"})[0] == MISMATCH


def test_c2b_other_receipt():
    assert c2b_verification(CONFIRMATION, status_result(receipt="OTHER12345"), {"600966"})[0] == MISMATCH


def test_c2b_paid_to_someone_else():
    assert c2b_verification(CONFIRMATION, status_result(credit="123456 - Other Ltd"), {"600966"})[0] == MISMATCH



def test_c2b_without_credit_party_is_not_verified():
    # fail closed: a result that does not name the shortcode paid proves nothing about it
    outcome, reason = c2b_verification(CONFIRMATION, status_result(credit=None), {"600966"})
    assert outcome == MISMATCH and "CreditPartyName" in reason
    assert c2b_verification(CONFIRMATION, status_result(credit=""), {"600966"})[0] == MISMATCH
    no_key = status_result()  # Daraja's own sample lists DebitPartyName twice, no CreditPartyName
    params = no_key["ResultParameters"]["ResultParameter"]
    params[1] = {"Key": "DebitPartyName", "Value": "600966 - Astral Ltd"}
    assert c2b_verification(CONFIRMATION, no_key, {"600966"})[0] == MISMATCH


def test_c2b_not_completed():
    assert c2b_verification(CONFIRMATION, status_result(status="Failed"), {"600966"})[0] == MISMATCH


def test_c2b_query_failure_is_pending():
    assert c2b_verification(CONFIRMATION, status_result(code=2001), {"600966"})[0] == PENDING


def test_c2b_till_number_counts_as_ours():
    conf = {**CONFIRMATION, "businessshortcode": "5123456"}
    result = status_result(credit="5123456 - Astral Shop")
    assert c2b_verification(conf, result, {"600966", "5123456"})[0] == VERIFIED


def test_credit_shortcode():
    assert credit_shortcode("600966 - Astral Ltd") == "600966"
    assert credit_shortcode(None) == ""


# ---------------------------------------------------------------- B2C payouts
def test_owed_per_doctype():
    assert owed("Purchase Invoice", {"outstanding_amount": 1200}) == 1200
    assert owed("Purchase Order", {"rounded_total": 1000, "grand_total": 999.6, "advance_paid": 400}) == 600
    assert owed("Employee Advance", {"advance_amount": 5000, "paid_amount": 5000}) == 0
    assert owed("Expense Claim", {"grand_total": 800, "total_amount_reimbursed": 300}) == 500
    assert owed("Expense Claim", {"total_sanctioned_amount": 800}) == 800
    assert owed("Salary Slip", {"net_pay": 45000}) == 45000


def test_owed_never_negative_and_unknown_doctype():
    assert owed("Purchase Order", {"grand_total": 100, "advance_paid": 150}) == 0
    assert owed("Journal Entry", {"total": 1}) is None
    assert owed("Loan", {"loan_amount": 1}) is None  # Loan payouts were removed
    assert owed("Purchase Invoice", None) == 0
