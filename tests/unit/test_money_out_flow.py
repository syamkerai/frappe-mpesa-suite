"""State-machine tests for Reversal, B2B and B2C Transaction Status, on an in-memory frappe.

Covers the safety rules: send only from a submitted, unsent document under a row lock;
ack = Initiated; transport timeout = Unknown (never Failed); results matched by the
ack's IDs; a re-sent result never books twice; queue timeouts never settle anything;
a status query settles a payout only while it is unsettled.
"""

import importlib

import pytest
import requests

import fake_frappe

SETTINGS = {
    "name": "Astral 600999",
    "initiator_name": "apiop",
    "security_credential": "Q1JFRA==",
    "business_shortcode": "600999",
    "sandbox": 1,
    "transaction_limit": 0,
    "consumer_key": "k",
}


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._data


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        common = importlib.import_module("frappe_mpsa_payments.money_out.common")
        mods = {
            "frappe": frappe,
            "common": common,
            "b2b": importlib.import_module("frappe_mpsa_payments.money_out.b2b"),
            "reversal": importlib.import_module("frappe_mpsa_payments.money_out.reversal"),
            "status": importlib.import_module("frappe_mpsa_payments.money_out.b2c_status"),
            "api": importlib.import_module("frappe_mpsa_payments.money_out.api"),
            "sent": [],
            "responses": [],
        }
        monkeypatch.setattr(common, "bearer_headers", lambda s, force=False: {"Authorization": "Bearer tok"})
        monkeypatch.setattr(
            common, "invalidate_token", lambda s, rejected=None: mods["sent"].append(f"invalidate {rejected}")
        )

        def fake_post(url, json=None, headers=None, timeout=None):
            mods["sent"].append((url, json))
            nxt = mods["responses"].pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        monkeypatch.setattr(common.requests, "post", fake_post)
        frappe.db.table("Mpesa Settings")[SETTINGS["name"]] = dict(SETTINGS)
        yield fake_frappe._dict(mods)
    finally:
        fake_frappe.uninstall(saved)


ACK = {"OriginatorConversationID": "OCID-1", "ConversationID": "AG_1", "ResponseCode": "0",
       "ResponseDescription": "Accept the service request successfully."}


def b2b_row(env, **kw):
    row = {"name": "MB2B-1", "docstatus": 1, "status": "Not Initiated", "mpesa_settings": SETTINGS["name"],
           "command_id": "BusinessPayBill", "receiver_shortcode": "888880", "account_reference": "INV-1",
           "amount": 1500.0, "remarks": "Fuel", "supplier": "Total Kenya"}
    row.update(kw)
    env.frappe.db.table("Mpesa B2B Payment Request")[row["name"]] = row
    return row


# ---------------------------------------------------------------- B2B send
def test_b2b_ack_marks_initiated_never_paid(env):
    row = b2b_row(env)
    env.responses.append(Resp(ACK))
    env.b2b.send_b2b("MB2B-1")

    url, body = env.sent[0]
    assert url.endswith("/mpesa/b2b/v1/paymentrequest") and "sandbox" in url
    assert body["Amount"] == 1500 and isinstance(body["Amount"], int)
    assert body["AccountReference"] == "INV-1" and body["PartyA"] == "600999"
    assert "b2b_result?ref=MB2B-1" in body["ResultURL"]
    assert "b2b_queue_timeout?ref=MB2B-1" in body["QueueTimeOutURL"]
    assert row["status"] == "Initiated"
    assert (row["conversation_id"], row["originator_conversation_id"]) == ("AG_1", "OCID-1")
    assert ("Mpesa B2B Payment Request", "MB2B-1") in env.frappe.db.locks
    assert env.frappe.request_logs[0]["data"]["SecurityCredential"] == "****"


@pytest.mark.parametrize("state", [{"docstatus": 0}, {"status": "Initiated"}, {"status": "Paid"}, {"status": "Unknown"}])
def test_b2b_refuses_draft_or_already_sent(env, state):
    b2b_row(env, **state)
    with pytest.raises(env.frappe.ValidationError):
        env.b2b.send_b2b("MB2B-1")
    assert env.sent == []


def test_b2b_read_timeout_is_unknown_not_failed(env):
    row = b2b_row(env)
    env.responses.append(requests.ReadTimeout())
    env.b2b.send_b2b("MB2B-1")  # must not raise
    assert row["status"] == "Unknown"


def test_b2b_connect_timeout_is_failed(env):
    row = b2b_row(env)
    env.responses.append(requests.ConnectTimeout())
    env.b2b.send_b2b("MB2B-1")
    assert row["status"] == "Failed"


def test_b2b_sync_error_is_failed(env):
    row = b2b_row(env)
    env.responses.append(Resp({"requestId": "x", "errorCode": "400.002.02", "errorMessage": "Bad Request"}, 400))
    env.b2b.send_b2b("MB2B-1")
    assert row["status"] == "Failed" and row["error_code"] == "400.002.02"


def test_token_error_refreshes_once_and_resends(env):
    row = b2b_row(env)
    env.responses += [Resp({"errorCode": "404.001.03", "errorMessage": "Invalid Access Token"}, 404), Resp(ACK)]
    env.b2b.send_b2b("MB2B-1")
    # Only the refused token is dropped, so a fresher token another worker cached survives.
    assert "invalidate tok" in env.sent and len([s for s in env.sent if s != "invalidate tok"]) == 2
    assert row["status"] == "Initiated"


def test_b2b_buy_goods_sends_account_reference(env):
    # Daraja's BusinessBuyGoods body lists AccountReference without marking it optional.
    b2b_row(env, command_id="BusinessBuyGoods", receiver_shortcode="123456", account_reference=None)
    env.responses.append(Resp(ACK))
    env.b2b.send_b2b("MB2B-1")
    _, body = env.sent[0]
    assert body["CommandID"] == "BusinessBuyGoods" and body["AccountReference"] == "MB2B-1"


# ---------------------------------------------------------------- B2B result
def b2b_result(code="0", conv="AG_1", occ="OCID-1"):
    return {
        "ResultType": "0", "ResultCode": code, "ResultDesc": "The service request is processed successfully",
        "OriginatorConversationID": occ, "ConversationID": conv, "TransactionID": "QKA81LK5CY",
        "ResultParameters": {"ResultParameter": [
            {"Key": "DebitAccountBalance", "Value": "{Amount={CurrencyCode=KES, MinimumAmount=618683, BasicAmount=6186.83}}"},
            {"Key": "Amount", "Value": "190.00"},
            {"Key": "TransCompletedTime", "Value": "20221110110717"},
            {"Key": "DebitPartyCharges", "Value": ""},
            {"Key": "ReceiverPartyPublicName", "Value": "000000- Biller Companyty"},
        ]},
    }


def test_b2b_result_string_zero_pays_and_books_once(env):
    row = b2b_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    env.api.b2b_result(Result=b2b_result(), ref="MB2B-1")
    assert row["status"] == "Paid" and row["transaction_id"] == "QKA81LK5CY"
    assert row["receiver_public_name"].startswith("000000")
    booked = [e for e in env.frappe.enqueued if e[0].endswith("b2b.book_payment_entry")]
    assert len(booked) == 1 and booked[0][1]["enqueue_after_commit"]

    env.api.b2b_result(Result=b2b_result(), ref="MB2B-1")  # Safaricom re-sends
    assert len([e for e in env.frappe.enqueued if e[0].endswith("b2b.book_payment_entry")]) == 1


def test_b2b_failed_result_books_nothing(env):
    row = b2b_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    env.api.b2b_result(Result=dict(b2b_result(code="2001"), ResultParameters=None), ref="MB2B-1")
    assert row["status"] == "Failed" and row["result_code"] == "2001"
    assert env.frappe.enqueued == []


def test_b2b_result_after_transport_timeout_matched_by_ref(env):
    row = b2b_row(env, status="Unknown")
    env.api.b2b_result(Result=b2b_result(), ref="MB2B-1")
    assert row["status"] == "Paid" and row["conversation_id"] == "AG_1"


def test_b2b_result_with_wrong_ids_is_rejected(env):
    row = b2b_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    env.api.b2b_result(Result=b2b_result(conv="AG_other", occ="OCID-other"), ref="MB2B-1")
    assert row["status"] == "Initiated" and env.frappe.errors


def test_b2b_result_ids_win_over_a_different_ref(env):
    b2b_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    row2 = b2b_row(env, name="MB2B-2", status="Initiated", conversation_id="AG_2", originator_conversation_id="OCID-2")
    env.api.b2b_result(Result=b2b_result(), ref="MB2B-2")
    assert row2["status"] == "Initiated"
    assert env.frappe.db.table("Mpesa B2B Payment Request")["MB2B-1"]["status"] == "Initiated"


def test_b2b_queue_timeout_flags_but_never_settles(env):
    row = b2b_row(env, status="Initiated", conversation_id="AG_1")
    env.api.b2b_queue_timeout(ref="MB2B-1", Result={"ResultCode": 1})
    assert row["queue_timed_out"] == 1 and row["status"] == "Initiated"


# ---------------------------------------------------------------- Reversal
def reversal_row(env, **kw):
    env.frappe.db.table("Mpesa C2B Payment Register")["MPC2B-1"] = {
        "name": "MPC2B-1", "transid": "OEI2AK4Q16", "transamount": 500.0, "businessshortcode": "600999", "docstatus": 1}
    row = {"name": "MRV-1", "docstatus": 1, "status": "Not Initiated", "c2b_register": "MPC2B-1",
           "transaction_id": "OEI2AK4Q16", "amount": 500.0, "mpesa_settings": SETTINGS["name"], "remarks": "Reversal"}
    row.update(kw)
    env.frappe.db.table("Mpesa Reversal Request")[row["name"]] = row
    return row


def test_reversal_send_body(env):
    row = reversal_row(env)
    env.responses.append(Resp(ACK))
    env.reversal.send_reversal("MRV-1")
    url, body = env.sent[0]
    assert url.endswith("/mpesa/reversal/v1/request")
    assert body["CommandID"] == "TransactionReversal" and body["TransactionID"] == "OEI2AK4Q16"
    assert body["RecieverIdentifierType"] == "11" and body["ReceiverParty"] == "600999"
    assert body["Amount"] == 500
    assert row["status"] == "Initiated"


@pytest.mark.parametrize("code,status", [(0, "Reversed"), ("R000001", "Already Reversed"), ("R000002", "Failed")])
def test_reversal_results(env, code, status):
    row = reversal_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    result = {"ResultCode": code, "ResultDesc": "d", "ConversationID": "AG_1", "OriginatorConversationID": "OCID-1",
              "TransactionID": "SGT7H7RSMC",
              "ResultParameters": {"ResultParameter": {"Key": "Charge", "Value": 0.0}}}  # single dict
    env.api.reversal_result(Result=result, ref="MRV-1")
    assert row["status"] == status and row["result_code"] == str(code)
    commented = [c for c in env.frappe.comments if c[1] == "MPC2B-1"]
    assert bool(commented) == (status != "Failed")

    env.api.reversal_result(Result=dict(result, ResultCode="R000002"), ref="MRV-1")  # re-send / late dup
    assert row["status"] == status


def test_reversal_validation_requires_full_amount(env):
    reversal_row(env)
    doc = env.frappe.get_doc({"doctype": "Mpesa Reversal Request", "name": "MRV-2", "c2b_register": "MPC2B-1",
                              "amount": 200})
    env.frappe.db.table("Mpesa Reversal Request").pop("MRV-1")
    with pytest.raises(env.frappe.ValidationError):
        env.reversal.validate_request(doc)


def test_reversal_validation_blocks_second_reversal(env):
    reversal_row(env, status="Initiated")
    doc = env.frappe.get_doc({"doctype": "Mpesa Reversal Request", "name": "MRV-2", "c2b_register": "MPC2B-1"})
    with pytest.raises(env.frappe.ValidationError, match="already exists"):
        env.reversal.validate_request(doc)


def test_reversal_validation_fills_from_register(env):
    reversal_row(env, status="Failed")  # a failed attempt does not block a new one
    doc = env.frappe.get_doc({"doctype": "Mpesa Reversal Request", "name": "MRV-2", "c2b_register": "MPC2B-1",
                              "mpesa_settings": SETTINGS["name"]})
    env.reversal.validate_request(doc)
    assert doc.transaction_id == "OEI2AK4Q16" and doc.amount == 500.0


# ---------------------------------------------------------------- B2C Transaction Status
def payout(env, **kw):
    row = {"name": "B2C-26-10-000001", "docstatus": 1, "status": "Initiated", "mpesa_settings": SETTINGS["name"],
           "amount": 300.0, "originator_conversation_id": "OCID-B2C", "conversation_id": "AG_B2C",
           "transaction_id": None, "result_code": None}
    row.update(kw)
    env.frappe.db.table("B2C Disbursement Request")[row["name"]] = row
    return row


def status_result(state="Completed", amount="300.00", code=0, occ="OCID-B2C"):
    return {
        "ResultType": 0, "ResultCode": code, "ResultDesc": "The service request is processed successfully.",
        "OriginatorConversationID": "OCID-Q", "ConversationID": "AG_Q", "TransactionID": "QUERYID",
        "ResultParameters": {"ResultParameter": [
            {"Key": "DebitPartyName", "Value": "600310 - Safaricom333"},
            {"Key": "OriginatorConversationID", "Value": occ},
            {"Key": "TransactionStatus", "Value": state},
            {"Key": "Amount", "Value": amount},
            {"Key": "ReceiptNo", "Value": "LKXXXX1234"},
            {"Key": "FinalisedTime", "Value": 20190208180016},
            {"Key": "DebitPartyName", "Value": "254708374149 - John"},
            {"Key": "TransactionReason"},
        ]},
    }


def start(env):
    env.responses.append(Resp(dict(ACK, OriginatorConversationID="OCID-Q", ConversationID="AG_Q")))
    return env.status.start_query("B2C-26-10-000001")


def test_status_query_sends_by_originator_id_when_no_receipt(env):
    payout(env, status="Not Initiated", conversation_id=None)  # transport timeout on send
    name = start(env)
    url, body = env.sent[0]
    assert url.endswith("/mpesa/transactionstatus/v1/query")
    assert body["OriginalConversationID"] == "OCID-B2C" and "TransactionID" not in body
    assert body["PartyA"] == "600999" and body["IdentifierType"] == "4"
    assert f"status_queue_timeout?ref={name}" in body["QueueTimeOutURL"]
    assert env.frappe.db.table("Mpesa B2C Status Query")[name]["status"] == "Sent"


def test_status_query_refuses_settled_payout_and_cooldown(env):
    payout(env, status="Paid", result_code="0")
    with pytest.raises(env.frappe.ValidationError):
        env.status.start_query("B2C-26-10-000001")
    env.frappe.db.table("B2C Disbursement Request")["B2C-26-10-000001"].update(status="Initiated", result_code=None)
    start(env)
    with pytest.raises(env.frappe.ValidationError, match="still waiting"):
        env.status.start_query("B2C-26-10-000001")


def test_status_completed_settles_payout_as_paid(env):
    row = payout(env)
    name = start(env)
    env.api.b2c_status_result(Result=status_result(), ref=name)
    q = env.frappe.db.table("Mpesa B2C Status Query")[name]
    assert q["outcome"] == "Paid" and q["applied"] == 1 and q["debit_party_name"] == "600310 - Safaricom333"
    assert row["status"] == "Paid" and row["result_code"] == "0" and row["transaction_id"] == "LKXXXX1234"
    jobs = [e for e in env.frappe.enqueued if e[0].endswith("update_b2c_reference_status")]
    assert len(jobs) == 1 and jobs[0][1]["b2c_request_name"] == row["name"]

    env.api.b2c_status_result(Result=status_result(), ref=name)  # re-sent
    assert len([e for e in env.frappe.enqueued if e[0].endswith("update_b2c_reference_status")]) == 1


def test_status_failed_marks_payout_failed_so_retry_is_allowed(env):
    row = payout(env)
    name = start(env)
    env.api.b2c_status_result(Result=status_result(state="Failed"), ref=name)
    assert row["status"] == "Failed" and row["result_code"].startswith("TS:")


@pytest.mark.parametrize(
    "result",
    [
        status_result(amount="250.00"),  # wrong amount
        status_result(state="Pending"),
        status_result(code="R000002"),  # query failed (not found yet)
        status_result(occ="OCID-someone-else"),
    ],
)
def test_status_unclear_results_leave_payout_alone(env, result):
    row = payout(env)
    name = start(env)
    env.api.b2c_status_result(Result=result, ref=name)
    assert row["status"] == "Initiated" and row["result_code"] is None
    assert env.frappe.db.table("Mpesa B2C Status Query")[name]["outcome"] == "Unresolved"
    assert env.frappe.enqueued == []


def test_status_never_overrides_real_result(env):
    row = payout(env)
    name = start(env)
    row.update(status="Failed", result_code="2001")  # the real B2C Result arrived meanwhile
    env.api.b2c_status_result(Result=status_result(), ref=name)
    assert row["status"] == "Failed" and env.frappe.enqueued == []


def test_status_queue_timeout(env):
    payout(env)
    name = start(env)
    env.api.status_queue_timeout(ref=name)
    assert env.frappe.db.table("Mpesa B2C Status Query")[name]["status"] == "Timed Out"


# ---------------------------------------------------------------- role gates
def test_check_status_needs_payout_or_admin_role(env):
    payout(env)
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.PermissionError):
        env.api.check_b2c_status("B2C-26-10-000001")
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.responses.append(Resp(ACK))
    assert env.api.check_b2c_status("B2C-26-10-000001")


def test_make_reversal_needs_payout_or_admin_role(env):
    env.frappe.db.table("Mpesa C2B Payment Register")["MPC2B-1"] = {"name": "MPC2B-1"}
    env.frappe.user_roles = {"Sales User", "Accounts User"}
    with pytest.raises(env.frappe.PermissionError):
        env.api.make_reversal("MPC2B-1", "wrong number")


def test_status_unknown_payout_is_queried_by_originator_id_and_settled(env):
    row = payout(env, status="Status Unknown", conversation_id=None)  # transport error on send
    name = start(env)
    assert env.sent[0][1]["OriginalConversationID"] == "OCID-B2C"
    env.api.b2c_status_result(Result=status_result(), ref=name)
    assert row["status"] == "Paid" and row["result_code"] == "0"
    assert [e for e in env.frappe.enqueued if e[0].endswith("update_b2c_reference_status")]
