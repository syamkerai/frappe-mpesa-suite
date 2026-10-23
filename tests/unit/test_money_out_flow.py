"""State-machine tests for Reversal, B2B and B2C Transaction Status, on an in-memory frappe.

Covers the safety rules: send only from a submitted, unsent document under a row lock;
ack = Initiated; transport timeout = Unknown (never Failed); results matched by the
ack's IDs; a re-sent result never books twice; queue timeouts never settle anything;
a status query settles a payout only while it is unsettled.
"""

import datetime
import importlib
import types

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
    "use_c2b": 1,
    "use_b2c": 1,
    "use_b2b": 1,
    "use_reversal": 1,
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
        common = importlib.import_module("mpsa_suite.money_out.common")
        mods = {
            "frappe": frappe,
            "common": common,
            "b2b": importlib.import_module("mpsa_suite.money_out.b2b"),
            "reversal": importlib.import_module("mpsa_suite.money_out.reversal"),
            "status": importlib.import_module("mpsa_suite.money_out.b2c_status"),
            "api": importlib.import_module("mpsa_suite.money_out.api"),
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
        frappe.db.db_type = "fake"  # named locks are no-ops here
        frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
        frappe.db.table("Mpesa Settings")[SETTINGS["name"]] = dict(SETTINGS)
        frappe.db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"] = {"allow_one_off_b2b": 0}
        frappe.db.table("Supplier M-Pesa Account")["MSA-1"] = {
            "name": "MSA-1", "docstatus": 1, "supplier": "Total Kenya", "account_type": "Paybill",
            "shortcode": "888880", "account_number": "INV-1", "expected_name": "Total Kenya",
            "active_from": datetime.datetime(2026, 10, 1), "is_default": 1, "name_mismatch": 0,
        }
        yield fake_frappe._dict(mods)
    finally:
        fake_frappe.uninstall(saved)


ACK = {"OriginatorConversationID": "OCID-1", "ConversationID": "AG_1", "ResponseCode": "0",
       "ResponseDescription": "Accept the service request successfully."}


def b2b_row(env, **kw):
    row = {"name": "MB2B-1", "docstatus": 1, "status": "Not Initiated", "mpesa_settings": SETTINGS["name"],
           "command_id": "BusinessPayBill", "receiver_shortcode": "888880", "account_reference": "INV-1",
           "amount": 1500.0, "remarks": "Fuel", "supplier": "Total Kenya", "supplier_mpesa_account": "MSA-1"}
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
    env.frappe.db.table("Supplier M-Pesa Account")["MSA-1"].update(
        account_type="Till (Buy Goods)", shortcode="123456", account_number=None)
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


# Names are sequential and, with enforcement off, anyone can post a "Result": ``ref`` alone
# must never settle a request that got an ack, nor one that cannot have reached Safaricom.
FORGED = {"ResultType": 0, "ResultCode": 0, "ResultDesc": "ok", "TransactionID": "QAB1234567"}


@pytest.mark.parametrize("code", [0, 1])
def test_b2b_result_without_ids_never_settles_an_acked_request(env, code):
    row = b2b_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    env.api.b2b_result(Result=dict(FORGED, ResultCode=code), ref="MB2B-1")
    assert row["status"] == "Initiated" and not row.get("result_code") and env.frappe.enqueued == []
    assert env.frappe.errors  # logged as a mismatch

    env.api.b2b_result(Result=b2b_result(), ref="MB2B-1")  # the real Result still lands
    assert row["status"] == "Paid" and row["transaction_id"] == "QKA81LK5CY"


def test_reversal_result_without_ids_never_settles_an_acked_request(env):
    row = reversal_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    env.api.reversal_result(Result=FORGED, ref="MRV-1")
    assert row["status"] == "Initiated" and not row.get("result_code")


@pytest.mark.parametrize(
    "state",
    [
        {"status": "Failed", "error_code": "400.002.02"},  # ack refused: nothing was processed
        {"status": "Failed", "error_message": "Could not connect to M-Pesa; nothing was sent."},
        {"status": "Not Initiated"},
    ],
)
def test_b2b_result_by_ref_only_for_a_request_that_may_have_been_sent(env, state):
    row = b2b_row(env, **state)
    env.api.b2b_result(Result=b2b_result(conv="AG_made_up", occ="OCID_made_up"), ref="MB2B-1")
    assert row["status"] == state["status"] and not row.get("result_code") and env.frappe.enqueued == []


def test_b2b_result_by_ref_needs_ids(env):
    row = b2b_row(env, status="Unknown")
    env.api.b2b_result(Result=FORGED, ref="MB2B-1")  # Safaricom's Results always carry both IDs
    assert row["status"] == "Unknown" and env.frappe.enqueued == []


# Frappe reads a list filter value as [operator, value]: a forged ConversationID of
# ["is", "set"] would match any acked request, with no ref and no token.
OPERATOR_IDS = [["is", "set"], ["like", "%"], ["!=", ""]]


@pytest.mark.parametrize("key", ["ConversationID", "OriginatorConversationID"])
@pytest.mark.parametrize("value", OPERATOR_IDS)
def test_b2b_result_with_an_operator_for_an_id_matches_nothing(env, key, value):
    row = b2b_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    env.api.b2b_result(Result=dict(FORGED, **{key: value}))
    assert row["status"] == "Initiated" and not row.get("result_code") and env.frappe.enqueued == []


@pytest.mark.parametrize("value", OPERATOR_IDS)
def test_reversal_result_with_an_operator_for_an_id_matches_nothing(env, value):
    row = reversal_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    env.api.reversal_result(Result=dict(FORGED, ConversationID=value, OriginatorConversationID=value))
    assert row["status"] == "Initiated" and not row.get("result_code")


def test_b2b_result_by_ref_with_operator_ids_is_refused(env):
    row = b2b_row(env, status="Unknown")
    env.api.b2b_result(Result=dict(FORGED, ConversationID=["is", "set"], OriginatorConversationID=["like", "%"]),
                       ref="MB2B-1")
    assert row["status"] == "Unknown" and env.frappe.enqueued == []


def test_b2b_result_with_real_safaricom_ids_still_matches(env):
    conv, occ = "AG_20191219_00005797af5d7d75f652", "16740-34861180-1"
    row = b2b_row(env, status="Initiated", conversation_id=conv, originator_conversation_id=occ)
    env.api.b2b_result(Result=b2b_result(conv=conv, occ=occ))
    assert row["status"] == "Paid" and row["transaction_id"] == "QKA81LK5CY"


def _as_http(env, monkeypatch, **url_args):
    """Run the endpoint as Frappe does for Safaricom's JSON POST: the query string (ref, t)
    reaches the handler only through ``frappe.local.request.args``, never the kwargs."""
    monkeypatch.setattr(env.api, "callback_token", lambda: "callback-token")
    env.frappe.local.request = fake_frappe._dict(args=url_args)


def test_result_ref_is_read_from_the_url_next_to_the_token(env, monkeypatch):
    row = b2b_row(env, status="Unknown")
    _as_http(env, monkeypatch, ref="MB2B-1", t="callback-token")
    env.api.b2b_result(Result=b2b_result())
    assert row["status"] == "Paid" and row["conversation_id"] == "AG_1"


@pytest.mark.parametrize("url_args", [{"ref": "MB2B-1"}, {"ref": "MB2B-1", "t": "wrong"}, {}])
def test_result_ref_without_the_token_is_ignored(env, monkeypatch, url_args):
    row = b2b_row(env, status="Unknown")
    _as_http(env, monkeypatch, **url_args)
    env.api.b2b_result(Result=b2b_result(), ref="MB2B-1")  # a ref in the body counts for nothing
    assert row["status"] == "Unknown" and env.frappe.enqueued == []


def test_queue_timeout_ref_is_read_from_the_url(env, monkeypatch):
    row = b2b_row(env, status="Initiated", conversation_id="AG_1")
    _as_http(env, monkeypatch, ref="MB2B-1", t="callback-token")
    env.api.b2b_queue_timeout(Result={"ResultCode": 1})
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


# ---------------------------------------------------------------- per-API switches
@pytest.mark.parametrize(
    "switch,make,send",
    [
        ("use_b2b", lambda env: b2b_row(env), lambda env: env.b2b.send_b2b("MB2B-1")),
        ("use_reversal", lambda env: reversal_row(env), lambda env: env.reversal.send_reversal("MRV-1")),
    ],
)
def test_record_without_the_switch_sends_nothing(env, switch, make, send):
    env.frappe.db.table("Mpesa Settings")[SETTINGS["name"]][switch] = 0
    row = make(env)
    with pytest.raises(env.frappe.ValidationError, match="ticked under APIs"):
        send(env)
    assert env.sent == [] and row["status"] == "Not Initiated"


def test_sandbox_money_out_uses_the_test_shortcode_in_till_number(env):
    # One sandbox record: 174379 for STK in Business Shortcode, 600xxx for the rest in Till Number.
    env.frappe.db.table("Mpesa Settings")[SETTINGS["name"]].update(business_shortcode="174379", till_number="600999")
    reversal_row(env)
    env.responses.append(Resp(ACK))
    env.reversal.send_reversal("MRV-1")
    assert env.sent[0][1]["ReceiverParty"] == "600999"


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


def test_status_result_without_ids_never_settles_a_payout(env):
    row = payout(env)
    name = start(env)
    forged = status_result(state="Failed")  # "Failed" needs no amount match
    forged.pop("ConversationID"), forged.pop("OriginatorConversationID")
    env.api.b2c_status_result(Result=forged, ref=name)
    assert row["status"] == "Initiated" and row["result_code"] is None and env.frappe.enqueued == []
    assert env.frappe.db.table("Mpesa B2C Status Query")[name]["status"] == "Sent"


@pytest.mark.parametrize("key", ["ConversationID", "OriginatorConversationID"])
@pytest.mark.parametrize("value", OPERATOR_IDS)
def test_status_result_with_an_operator_for_an_id_settles_nothing(env, key, value):
    row = payout(env)
    name = start(env)
    forged = status_result(state="Failed")  # "Failed" needs no amount match
    forged.pop("ConversationID"), forged.pop("OriginatorConversationID")
    env.api.b2c_status_result(Result=dict(forged, **{key: value}))
    assert row["status"] == "Initiated" and row["result_code"] is None and env.frappe.enqueued == []
    assert env.frappe.db.table("Mpesa B2C Status Query")[name]["status"] == "Sent"


def test_status_result_after_a_lost_ack_is_matched_by_ref(env):
    row = payout(env)
    env.responses.append(requests.ReadTimeout())  # the query may have reached Safaricom
    name = env.status.start_query("B2C-26-10-000001")
    assert env.frappe.db.table("Mpesa B2C Status Query")[name]["status"] == "Failed to Send"
    env.api.b2c_status_result(Result=status_result(), ref=name)
    assert row["status"] == "Paid" and row["transaction_id"] == "LKXXXX1234"


@pytest.mark.parametrize(
    "answer",
    [requests.ConnectTimeout(), Resp({"errorCode": "500.001.1001", "errorMessage": "Unable to lock subscriber"}, 500)],
)
def test_status_result_by_ref_only_for_a_query_that_may_have_been_sent(env, answer):
    row = payout(env)
    env.responses.append(answer)  # never sent, or refused: Safaricom has nothing to answer
    name = env.status.start_query("B2C-26-10-000001")
    env.api.b2c_status_result(Result=status_result(), ref=name)
    assert row["status"] == "Initiated" and row["result_code"] is None and env.frappe.enqueued == []


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


# ---------------------------------------------------------------- B2B: checked again at send (payouts Phase 1)
@pytest.mark.parametrize(
    "change, message",
    [
        (lambda env, row: row.update(supplier_mpesa_account=None), "Pick the supplier's approved M-Pesa account"),
        (lambda env, row: row.update(receiver_shortcode="999999"), "does not match Supplier M-Pesa Account"),
        (lambda env, row: env.frappe.db.table("Supplier M-Pesa Account")["MSA-1"].update(docstatus=2), "cancelled"),
        (lambda env, row: env.frappe.db.table("Mpesa Settings")[SETTINGS["name"]].update(transaction_limit=1000),
         "transaction limit of 1000"),
        (lambda env, row: env.frappe.db.table("Mpesa Settings")[SETTINGS["name"]].update(company="Other Co"),
         "pays for Other Co, not Astral"),
        (lambda env, row: env.frappe.db.table("Supplier").update(
            {"Total Kenya": {"name": "Total Kenya", "on_hold": 1, "hold_type": "Payments", "release_date": None}}),
         "Total Kenya is on hold for payments"),
    ],
)
def test_b2b_send_refuses_what_changed_after_submit_with_no_http_call(env, change, message):
    row = b2b_row(env, company="Astral")
    env.frappe.db.table("Mpesa Settings")[SETTINGS["name"]]["company"] = "Astral"
    change(env, row)
    with pytest.raises(env.frappe.ValidationError, match=message):
        env.b2b.send_b2b("MB2B-1")
    assert env.sent == [] and row["status"] == "Not Initiated"


def test_b2b_one_off_destination_needs_the_switch_and_a_manager_at_send(env):
    row = b2b_row(env, supplier_mpesa_account=None, receiver_shortcode="999999")
    env.frappe.db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"]["allow_one_off_b2b"] = 1
    with pytest.raises(env.frappe.PermissionError):  # a Payer may not send a typed number (R7)
        env.b2b.send_b2b("MB2B-1")
    assert env.sent == []
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.responses.append(Resp(ACK))
    env.b2b.send_b2b("MB2B-1")
    assert row["status"] == "Initiated"


def test_b2b_send_needs_a_payout_role(env):
    b2b_row(env)
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.PermissionError):
        env.b2b.send_b2b("MB2B-1")
    assert env.sent == []


def test_b2b_send_refuses_an_invoice_put_on_hold_after_submit(env, monkeypatch):
    owed = importlib.import_module("mpsa_suite.money_out.owed")
    row = b2b_row(env, purchase_invoice="PINV-1")
    env.frappe.db.table("Purchase Invoice")["PINV-1"] = {
        "name": "PINV-1", "docstatus": 1, "supplier": "Total Kenya", "currency": "KES", "on_hold": 1,
        "outstanding_amount": 5000}
    locks = []
    monkeypatch.setattr(owed, "doc_lock", lambda dt, dn: locks.append((dt, dn)) or __import__("contextlib").nullcontext())
    monkeypatch.setattr(owed, "left_to_pay", lambda dt, dn, **kw: fake_frappe._dict(owed=5000, parts=[], left=5000))
    with pytest.raises(env.frappe.ValidationError, match="PINV-1 is on hold. Release the hold"):
        env.b2b.send_b2b("MB2B-1")
    assert env.sent == [] and row["status"] == "Not Initiated"
    assert locks == [("Purchase Invoice", "PINV-1")]  # under the document's lock


def test_b2b_send_refuses_more_than_is_left_now(env, monkeypatch):
    owed = importlib.import_module("mpsa_suite.money_out.owed")
    b2b_row(env, purchase_invoice="PINV-1")
    env.frappe.db.table("Purchase Invoice")["PINV-1"] = {
        "name": "PINV-1", "docstatus": 1, "supplier": "Total Kenya", "currency": "KES", "outstanding_amount": 5000}
    seen = []

    def left_to_pay(dt, dn, exclude_request=None, exclude_line=None, for_update=False):
        seen.append((exclude_request, for_update))
        part = fake_frappe._dict(kind="b2c", name="D-9", amount=4000, label="B2C D-9 Initiated 4,000")
        return fake_frappe._dict(owed=5000, parts=[part], left=1000)

    monkeypatch.setattr(owed, "left_to_pay", left_to_pay)
    with pytest.raises(env.frappe.ValidationError, match="B2C D-9 Initiated 4,000: 1,000 is left to pay"):
        env.b2b.send_b2b("MB2B-1")
    assert env.sent == [] and seen == [("MB2B-1", True)]


def test_b2b_paid_result_marks_the_entry_as_being_booked(env):
    row = b2b_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    env.api.b2b_result(Result=b2b_result(), ref="MB2B-1")
    assert (row["entry_status"], row["entry_note"]) == ("Entry Not Made", "Booking the entry…")
    # committed with the Result, not by the job: if the job is lost, the sweep books it
    assert row["entry_attempted_on"]


def test_b2b_failed_result_writes_no_entry_state(env):
    row = b2b_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    env.api.b2b_result(Result=dict(b2b_result(code="2001"), ResultParameters=None), ref="MB2B-1")
    assert not row.get("entry_status") and not row.get("entry_attempted_on")


def test_b2b_late_result_on_a_cancelled_unknown_request_is_recorded(env):
    # an admin cancelled it after checking the statement; the money still left (R2)
    row = b2b_row(env, status="Unknown", docstatus=2)
    env.api.b2b_result(Result=b2b_result(), ref="MB2B-1")
    assert row["status"] == "Paid" and row["transaction_id"] == "QKA81LK5CY"
    assert [e for e in env.frappe.enqueued if e[0].endswith("b2b.book_payment_entry")]  # it books Needs Checking


def _controller(env, monkeypatch):
    env.frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
    env.frappe.logger = lambda *a, **kw: None
    module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.mpesa_b2b_payment_request.mpesa_b2b_payment_request")
    calls = []
    monkeypatch.setattr(module, "validate_request", lambda doc, at_submit=False: calls.append(at_submit))
    monkeypatch.setattr(module, "send_b2b", lambda name: calls.append(("send", name)))
    doc = module.MpesaB2BPaymentRequest()
    doc.name, doc.docstatus, doc.status = "MB2B-1", 1, None
    doc.flags = fake_frappe._dict()  # as a Frappe Document has
    doc.get = lambda key, default=None: getattr(doc, key, default)
    return doc, calls


def test_b2b_submit_runs_every_check_again(env, monkeypatch):
    # validate() runs the checks only on drafts; Frappe runs before_submit on submit and on an
    # insert that carries docstatus 1 (the typed-paybill bypass, F1)
    doc, calls = _controller(env, monkeypatch)
    doc.validate()
    assert calls == []  # docstatus 1: not here
    doc.before_submit()
    doc.on_submit()
    assert calls == [True, ("send", "MB2B-1")] and doc.status == "Not Initiated"


def test_b2b_submit_needs_a_payout_role(env, monkeypatch):
    doc, calls = _controller(env, monkeypatch)
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.PermissionError):
        doc.before_submit()
    assert calls == []


def _saved_after_submit(env, monkeypatch, **db_row):
    """The controller for a submitted request whose row the server already changed."""
    doc, _ = _controller(env, monkeypatch)
    row = b2b_row(env, **db_row)
    doc.doctype = "Mpesa B2B Payment Request"
    doc.meta = types.SimpleNamespace(has_field=lambda field: True)
    doc.update = lambda values: [setattr(doc, k, v) for k, v in values.items()]
    doc.get = lambda key, default=None: getattr(doc, key, default)
    return doc, row


def test_b2b_save_after_submit_keeps_what_the_server_wrote(env, monkeypatch):
    # Frappe does not enforce read only on the server: a script or a stale form could otherwise
    # mark an Initiated request Failed (it would stop counting) or give it another receipt
    doc, row = _saved_after_submit(env, monkeypatch, status="Initiated", conversation_id="AG_1")
    doc.status, doc.transaction_id, doc.entry_status, doc.payment_entry = (
        "Failed", "QFORGED01", "Entry Made", "ACC-PAY-9")
    doc.remarks = "kept as typed"  # not a server field: Frappe's own rules apply
    doc.before_update_after_submit()
    assert (doc.status, doc.transaction_id, doc.entry_status, doc.payment_entry) == ("Initiated", None, None, None)
    assert doc.conversation_id == "AG_1" and doc.remarks == "kept as typed"
    assert ("Mpesa B2B Payment Request", "MB2B-1") in env.frappe.db.locks  # a locking read


@pytest.mark.parametrize("values, refused", [
    ({"status": "Paid", "docstatus": 2, "transaction_id": "QKA81LK5CY"}, True),
    ({"status": "Unknown", "docstatus": 2}, True),
    ({"status": "Initiated", "docstatus": 2}, True),
    ({"status": "Failed", "docstatus": 2, "conversation_id": "AG_1"}, True),  # M-Pesa saw it
    ({"status": "Failed", "docstatus": 2}, False),  # never reached M-Pesa
    ({"status": "Not Initiated", "docstatus": 2}, False),
    ({"status": None, "docstatus": 0}, False),
])
def test_b2b_request_that_may_have_reached_mpesa_cannot_be_deleted(env, monkeypatch, values, refused):
    doc, _ = _saved_after_submit(env, monkeypatch)
    for key, value in values.items():
        setattr(doc, key, value)
    if refused:
        with pytest.raises(env.frappe.ValidationError, match="It is the record of that payment and cannot be deleted."):
            doc.on_trash()
    else:
        doc.on_trash()


@pytest.mark.parametrize("status", ["Initiated", "Unknown"])
def test_b2b_cancel_of_a_request_that_may_have_paid_is_for_managers(env, monkeypatch, status):
    # an Initiated request whose Result never came is settled like an Unknown one: a manager
    # checks the statement first (Phase 1 has no B2B status query)
    doc, _ = _saved_after_submit(env, monkeypatch)
    doc.status = status
    env.frappe.user_roles = {"M-Pesa Payer"}
    with pytest.raises(env.frappe.PermissionError):
        doc.before_cancel()
    env.frappe.user_roles = {"Accounts Manager"}
    doc.before_cancel()


def test_b2b_paid_request_cannot_be_cancelled(env, monkeypatch):
    doc, _ = _saved_after_submit(env, monkeypatch)
    doc.status = "Paid"
    env.frappe.user_roles = {"System Manager"}
    with pytest.raises(env.frappe.ValidationError, match="A B2B payment that is Paid cannot be cancelled"):
        doc.before_cancel()


@pytest.mark.parametrize("error", ["QueryTimeoutError", "QueryDeadlockError"])
def test_b2b_result_that_meets_a_lock_is_tried_again_from_a_job(env, monkeypatch, error):
    row = b2b_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    results = importlib.import_module("mpsa_suite.money_out.results")
    real_find = results.find_request
    rolled_back = []
    monkeypatch.setattr(env.frappe.db, "rollback", lambda save_point=None: rolled_back.append(save_point),
                        raising=False)

    def locked(*a, **kw):
        raise getattr(env.frappe, error)("Lock wait timeout exceeded")

    monkeypatch.setattr(env.b2b, "find_request", locked)
    assert env.api.b2b_result(Result=b2b_result(), ref="MB2B-1") == {"ResultCode": 0, "ResultDesc": "Accepted"}
    assert row["status"] == "Initiated" and rolled_back == [None]
    ((method, kw),) = env.frappe.enqueued
    assert method == "mpsa_suite.money_out.b2b.handle_result"
    assert (kw["result"]["TransactionID"], kw["ref"], kw["attempt"]) == ("QKA81LK5CY", "MB2B-1", 2)

    monkeypatch.setattr(env.b2b, "find_request", real_find)  # the job, once the lock is free
    env.b2b.handle_result(kw["result"], kw["ref"], kw["attempt"])
    assert row["status"] == "Paid" and row["transaction_id"] == "QKA81LK5CY"


def test_b2b_result_still_locked_after_the_last_try_is_logged_with_its_payload(env, monkeypatch):
    b2b_row(env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")

    def locked(*a, **kw):
        raise env.frappe.QueryTimeoutError("Lock wait timeout exceeded")

    monkeypatch.setattr(env.b2b, "find_request", locked)
    with pytest.raises(env.frappe.QueryTimeoutError):
        env.b2b.handle_result(b2b_result(), "MB2B-1", env.b2b.RESULT_TRIES)
    assert env.frappe.enqueued == []
    ((_args, kw),) = env.frappe.errors
    assert kw["title"] == "M-Pesa B2B Result not recorded" and "QKA81LK5CY" in kw["message"]


def test_result_handler_error_is_logged_with_the_result(env, monkeypatch):
    # anything else still fails the callback, and the Error Log keeps the Result to replay by hand
    monkeypatch.setattr(env.b2b, "find_request", lambda *a, **kw: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        env.api.b2b_result(Result=b2b_result(), ref="MB2B-1")
    ((args, kw),) = env.frappe.errors
    assert args == () and kw["title"] == "M-Pesa B2B Result Error" and "QKA81LK5CY" in kw["message"]
