from datetime import datetime

import pytest

from frappe_mpsa_payments.daraja import money_out as m

URLS = dict(result_url="https://x/r?ref=A&t=s", queue_timeout_url="https://x/q?ref=A&t=s")
CRED = dict(initiator="api_user", security_credential="c2VjcmV0")


# ---------------------------------------------------------------- amounts / remarks
@pytest.mark.parametrize("value,expected", [(100, 100), ("250", 250), (10.0, 10), ("1500.00", 1500)])
def test_whole_shillings_accepts_integers(value, expected):
    assert m.whole_shillings(value) == expected


@pytest.mark.parametrize("value", [0, -5, "10.5", 99.99, None, "", "abc"])
def test_whole_shillings_refuses_fractions_and_non_positive(value):
    with pytest.raises(ValueError):
        m.whole_shillings(value)


def test_remarks_are_2_to_100_chars():
    assert m.clamp_remarks("x", "Reversal") == "Reversal"
    assert m.clamp_remarks("  ok  ") == "ok"
    assert len(m.clamp_remarks("a" * 300)) == 100


def test_ack_needs_string_or_int_zero():
    assert m.ack_accepted({"ResponseCode": "0"})
    assert m.ack_accepted({"ResponseCode": 0})
    assert not m.ack_accepted({"ResponseCode": "1"})
    assert not m.ack_accepted({"errorCode": "400.002.02"})
    assert not m.ack_accepted(None)


# ---------------------------------------------------------------- request bodies
def test_reversal_payload_matches_daraja_fields():
    p = m.reversal_payload(
        **CRED, **URLS, transaction_id=" OEI2AK4Q16 ", amount="500", receiver_party="600999", remarks="Wrong till"
    )
    assert p == {
        "Initiator": "api_user",
        "SecurityCredential": "c2VjcmV0",
        "CommandID": "TransactionReversal",
        "TransactionID": "OEI2AK4Q16",
        "Amount": 500,
        "ReceiverParty": "600999",
        "RecieverIdentifierType": "11",
        "Remarks": "Wrong till",
        "QueueTimeOutURL": URLS["queue_timeout_url"],
        "ResultURL": URLS["result_url"],
    }


def test_reversal_payload_needs_receipt_and_whole_amount():
    with pytest.raises(ValueError):
        m.reversal_payload(**CRED, **URLS, transaction_id="", amount=10, receiver_party="1", remarks="r")
    with pytest.raises(ValueError):
        m.reversal_payload(**CRED, **URLS, transaction_id="ABC", amount=10.5, receiver_party="1", remarks="r")


def test_b2b_paybill_payload():
    p = m.b2b_payload(
        **CRED, **URLS, command_id="BusinessPayBill", amount=1200, party_a="600992", party_b="888880",
        account_reference="INV-77", requester="254708374149", remarks="Fuel", occasion="Oct",
    )
    assert p["CommandID"] == "BusinessPayBill"
    assert p["SenderIdentifierType"] == "4" and p["RecieverIdentifierType"] == "4"
    assert p["Amount"] == 1200 and isinstance(p["Amount"], int)
    assert p["PartyA"] == "600992" and p["PartyB"] == "888880"
    assert p["AccountReference"] == "INV-77"
    assert p["Requester"] == "254708374149"
    assert p["Occassion"] == "Oct" and "Occasion" not in p  # Daraja's B2B spelling
    assert p["QueueTimeOutURL"] != p["ResultURL"]


def test_b2b_buy_goods_sends_account_reference():
    # Daraja's BusinessBuyGoods body lists AccountReference without marking it optional.
    p = m.b2b_payload(
        **CRED, **URLS, command_id="BusinessBuyGoods", amount=50, party_a="600992", party_b="123456",
        account_reference="TILL-REF", remarks="Lunch",
    )
    assert p["CommandID"] == "BusinessBuyGoods" and p["AccountReference"] == "TILL-REF"
    assert "Requester" not in p and "Occassion" not in p


@pytest.mark.parametrize("fallback,expected", [("MB2B-26-10-000001", "MB2B-26-10-000001"), (None, "Payment")])
def test_b2b_buy_goods_without_reference_uses_fallback(fallback, expected):
    p = m.b2b_payload(
        **CRED, **URLS, command_id="BusinessBuyGoods", amount=50, party_a="600992", party_b="123456",
        remarks="Lunch", fallback_reference=fallback,
    )
    assert p["AccountReference"] == expected


def test_b2b_paybill_ignores_fallback_reference():
    with pytest.raises(ValueError):
        m.b2b_payload(
            **CRED, **URLS, command_id="BusinessPayBill", amount=50, party_a="600992", party_b="888880",
            remarks="x", fallback_reference="MB2B-1",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(command_id="BusinessPayBill", party_b="888880", account_reference=""),  # paybill w/o account
        dict(command_id="SalaryPayment", party_b="888880", account_reference="A"),  # wrong command
        dict(command_id="BusinessBuyGoods", party_b="till-1", account_reference=None),  # non-numeric
    ],
)
def test_b2b_payload_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        m.b2b_payload(**CRED, **URLS, amount=10, party_a="600992", remarks="x", **kwargs)


def test_b2b_payload_rejects_cents():
    with pytest.raises(ValueError):
        m.b2b_payload(
            **CRED, **URLS, command_id="BusinessBuyGoods", amount="10.50", party_a="1", party_b="2", remarks="x"
        )


def test_status_payload_prefers_receipt_and_falls_back_to_conversation_id():
    by_receipt = m.transaction_status_payload(
        **CRED, **URLS, party_a="600999", transaction_id="OEI2AK4Q16", original_conversation_id="abc"
    )
    assert by_receipt["TransactionID"] == "OEI2AK4Q16" and "OriginalConversationID" not in by_receipt
    assert by_receipt["CommandID"] == "TransactionStatusQuery" and by_receipt["IdentifierType"] == "4"
    assert "Occasion" in by_receipt  # Transaction Status spells it correctly

    by_id = m.transaction_status_payload(**CRED, **URLS, party_a="600999", original_conversation_id="abc-123")
    assert by_id["OriginalConversationID"] == "abc-123" and "TransactionID" not in by_id

    with pytest.raises(ValueError):
        m.transaction_status_payload(**CRED, **URLS, party_a="600999")


# ---------------------------------------------------------------- results
REVERSAL_OK = {
    "ResultType": 0,
    "ResultCode": 0,  # integer in the Reversal sample
    "ResultDesc": "The service request is processed successfully.",
    "OriginatorConversationID": "f1e2-4b95-a71d-b30d3cdbb7a7942864",
    "ConversationID": "AG_20210709_1234409f86436c583e3f",
    "TransactionID": "SGT7H7RSMC",
    "ResultParameters": {
        "ResultParameter": [
            {"Key": "DebitAccountBalance", "Value": "Utility Account|KES|7722179.62|7722179.62|0.00|0.00"},
            {"Key": "Amount", "Value": 1.0},
            {"Key": "TransCompletedTime", "Value": 20210709114201},
            {"Key": "OriginalTransactionID", "Value": "OEI2AK4Q16"},
            {"Key": "Charge", "Value": 0.0},
            {"Key": "CreditPartyPublicName", "Value": "254708374149 - John Doe"},
            {"Key": "DebitPartyPublicName", "Value": "600992 - Safaricom Daraja 001"},
        ]
    },
}


def test_reversal_outcomes():
    assert m.reversal_outcome(REVERSAL_OK) == "Reversed"
    assert m.reversal_outcome({"ResultCode": "R000001"}) == "Already Reversed"
    assert m.reversal_outcome({"ResultCode": "R000002"}) == "Failed"
    assert m.reversal_outcome({"ResultCode": 2001}) == "Failed"
    v = m.result_values(REVERSAL_OK)
    assert v["OriginalTransactionID"] == "OEI2AK4Q16"
    assert m.parse_daraja_datetime(v["TransCompletedTime"]) == datetime(2021, 7, 9, 11, 42, 1)


def test_b2b_result_code_is_a_string():
    assert m.b2b_outcome({"ResultCode": "0"}) == "Paid"
    assert m.b2b_outcome({"ResultCode": 0}) == "Paid"
    assert m.b2b_outcome({"ResultCode": "2001"}) == "Failed"
    assert m.b2b_outcome({}) == "Failed"


def test_result_parameter_as_single_dict_or_absent():
    single = {"ResultParameters": {"ResultParameter": {"Key": "BOCompletedTime", "Value": "20200109125710"}}}
    assert m.result_values(single) == {"BOCompletedTime": "20200109125710"}
    assert m.result_values({"ResultCode": 2001}) == {}
    assert m.result_values({"ResultParameters": None}) == {}
    assert m.result_values({"ResultParameters": {"ResultParameter": [{"Key": "TransactionReason"}]}}) == {
        "TransactionReason": None
    }


def test_repeated_key_keeps_first():
    r = {"ResultParameters": {"ResultParameter": [
        {"Key": "DebitPartyName", "Value": "600610 - Org"},
        {"Key": "DebitPartyName", "Value": "254708374149 - Jane"},
    ]}}
    assert m.result_values(r)["DebitPartyName"] == "600610 - Org"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("06.07.2024 22:48:52", datetime(2024, 7, 6, 22, 48, 52)),  # day first, never 7 June
        ("05.03.2024 08:00:00", datetime(2024, 3, 5, 8, 0, 0)),
        (20191219102115, datetime(2019, 12, 19, 10, 21, 15)),
        ("2024-01-02 03:04:05", datetime(2024, 1, 2, 3, 4, 5)),
        ("", None),
        ("garbage", None),
    ],
)
def test_parse_daraja_datetime(value, expected):
    assert m.parse_daraja_datetime(value) == expected


def _status(code=0, state="Completed", amount="300.00", receipt="LKXXXX1234", **extra):
    params = [
        {"Key": "DebitPartyName", "Value": "600310 - Safaricom333"},
        {"Key": "TransactionStatus", "Value": state},
        {"Key": "Amount", "Value": amount},
        {"Key": "ReceiptNo", "Value": receipt},
        {"Key": "FinalisedTime", "Value": 20190208180016},
        {"Key": "TransactionReason"},
    ] + [{"Key": k, "Value": v} for k, v in extra.items()]
    return {"ResultCode": code, "ResultDesc": "ok", "ResultParameters": {"ResultParameter": params}}


def test_b2c_status_completed_with_matching_amount_is_paid():
    assert m.b2c_status_outcome(_status(), expected_amount=300) == ("Paid", "Completed")


def test_b2c_status_amount_mismatch_or_missing_receipt_is_unresolved():
    assert m.b2c_status_outcome(_status(amount="299"), expected_amount=300)[0] == "Unresolved"
    assert m.b2c_status_outcome(_status(receipt=""), expected_amount=300)[0] == "Unresolved"


@pytest.mark.parametrize("state", ["Failed", "Declined", "Expired", "Cancelled"])
def test_b2c_status_final_failure(state):
    assert m.b2c_status_outcome(_status(state=state), expected_amount=300)[0] == "Failed"


@pytest.mark.parametrize("state", ["Pending", "Authorized", ""])
def test_b2c_status_other_states_settle_nothing(state):
    assert m.b2c_status_outcome(_status(state=state), expected_amount=300)[0] == "Unresolved"


def test_b2c_status_failed_query_settles_nothing():
    # e.g. "not found" right after sending: the payout may still complete
    outcome, reason = m.b2c_status_outcome({"ResultCode": "R000002", "ResultDesc": "not found"}, 300)
    assert outcome == "Unresolved" and "R000002" in reason
