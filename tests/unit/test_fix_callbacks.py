"""Callback endpoints on an in-memory frappe: C2B verification results, Transaction Status
and Account Balance results matched by exact IDs, and STK re-prompts on the same request.

The fake's LIKE filter matches every row, which is what a ConversationID of "%" did on a
real database, so a lookup is only right if it confirms the exact ID afterwards.
"""

import datetime
import importlib
import json
import sys
import types

import pytest

import fake_frappe

SETTINGS = "TESTC2B"
SHORTCODE = "174379"
EXPRESS = "Mpesa Express Request"
IR = "Integration Request"
HELD = "Mpesa C2B Verification"


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    # m_pesa_api imports the security-credential helper, which needs `cryptography`
    stub = types.ModuleType("frappe_mpsa_payments.utils.encoding_initiator_password")
    stub.generate_security_credential = lambda *a, **kw: ""
    sys.modules[stub.__name__] = stub
    try:
        api = importlib.import_module("frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api")
        c2b_verify = importlib.import_module("frappe_mpsa_payments.money_in.c2b_verify")
        monkeypatch.setattr(fake_frappe.FakeDoc, "save", lambda self, **kw: None, raising=False)
        frappe.logger = lambda *a, **kw: types.SimpleNamespace(info=lambda *a, **kw: None)
        frappe.db.table("Mpesa Settings")[SETTINGS] = {
            "name": SETTINGS, "business_shortcode": SHORTCODE, "till_number": "", "use_stk": 1, "use_c2b": 1,
            "flags": fake_frappe._dict(),
        }
        yield fake_frappe._dict(frappe=frappe, api=api, c2b_verify=c2b_verify)
    finally:
        fake_frappe.uninstall(saved)


def add(env, doctype, name, **row):
    env.frappe.db.table(doctype)[name] = {"name": name, "flags": fake_frappe._dict(), **row}
    return env.frappe.db.table(doctype)[name]


# ---------------------------------------------------------------- C2B verification result
def held(env, name, conversation_id, **row):
    # conversation_id None: the query's ack was lost (money_out.common.post gave no answer)
    state = {"attempts": 1, "settings": SETTINGS, "conversation_id": conversation_id,
             "originator_conversation_id": f"OC-{conversation_id}" if conversation_id else None}
    return add(env, IR, name, integration_request_service=HELD, status="Queued", request_id="RKL51ZDR4F",
               output=json.dumps(state), **row)


def verification_result(conversation_id="AG_20261020_c2b0001"):
    return {"ResultCode": 0, "ConversationID": conversation_id, "OriginatorConversationID": f"OC-{conversation_id}",
            "ResultParameters": {"ResultParameter": [{"Key": "ReceiptNo", "Value": "RKL51ZDR4F"}]}}


def test_c2b_result_reads_the_held_request_from_the_query_string(env):
    # Frappe builds a JSON POST's arguments from the body only: "ir" is never in kwargs.
    # The query's ack was lost, so the result's IDs cannot find the request: only ?ir= can.
    held(env, "IR-HELD", None)
    assert env.api.c2b_verification_result(Result=verification_result())["ResultCode"] == 1
    assert env.frappe.enqueued == []
    env.frappe.local.request = types.SimpleNamespace(args={"ir": "IR-HELD", "t": "token"})
    answer = env.api.c2b_verification_result(Result=verification_result())
    assert answer == {"ResultCode": 0, "ResultDesc": "Accepted"}
    method, kwargs = env.frappe.enqueued[-1]
    assert method.endswith(".apply_c2b_verification") and kwargs["ir"] == "IR-HELD"


def test_c2b_result_without_ir_is_found_by_its_exact_ids(env):
    held(env, "IR-NEAR", "AG_20261020_c2b00011")  # LIKE '%AG_..._c2b0001%' matches this too
    add(env, IR, "IR-STATUS", integration_request_service="Mpesa Transaction Status",
        output=json.dumps({"ConversationID": "AG_20261020_c2b0001"}))
    held(env, "IR-HELD", "AG_20261020_c2b0001")
    answer = env.api.c2b_verification_result(Result=verification_result())
    assert answer["ResultCode"] == 0
    assert env.frappe.enqueued[-1][1]["ir"] == "IR-HELD"
    # the OriginatorConversationID alone is enough
    result = {**verification_result(), "ConversationID": None}
    assert env.c2b_verify.find_held(result) == "IR-HELD"


@pytest.mark.parametrize("conversation_id", ["%", "attempts", "AG_20261020_c2b000", None])
def test_c2b_result_with_wildcard_or_partial_id_is_rejected(env, conversation_id):
    held(env, "IR-HELD", "AG_20261020_c2b0001")
    result = {"ResultCode": 0, "ConversationID": conversation_id, "OriginatorConversationID": conversation_id}
    assert env.api.c2b_verification_result(Result=result)["ResultCode"] == 1
    assert env.frappe.enqueued == []


def test_c2b_result_must_name_a_held_confirmation(env):
    add(env, IR, "IR-OTHER", integration_request_service="Mpesa Transaction Status", output="{}")
    env.frappe.local.request = types.SimpleNamespace(args={"ir": "IR-OTHER"})
    assert env.api.c2b_verification_result(Result=verification_result())["ResultCode"] == 1
    assert env.api.c2b_verification_result(Result="not json")["ResultCode"] == 1
    assert env.frappe.enqueued == []


# ---------------------------------------------------------------- Transaction Status result
ASKED_CID = "AG_20261020_0000status01"


def status_query(env, receipt="QJK1ABC2DE"):
    add(env, IR, "IR-QUERY", integration_request_service="Mpesa Transaction Status", status="Completed",
        data=json.dumps({"mpesa_settings": SETTINGS, "transaction_id": receipt}),
        output=json.dumps({"OriginatorConversationID": "OC-77", "ConversationID": ASKED_CID, "ResponseCode": "0"}))


def unsolicited(env, conversation_id=ASKED_CID, status="Completed", credit=SHORTCODE, receipt="QJK1ABC2DE"):
    return env.api._unsolicited_status_result(
        {"ResultCode": 0, "ConversationID": conversation_id},
        {"TransactionStatus": status, "ReceiptNo": receipt, "Amount": "95000"},
        receipt,
        credit,
    )


def test_status_result_answering_our_query_is_accepted(env):
    status_query(env)
    assert unsolicited(env) is None


@pytest.mark.parametrize("conversation_id", ["%", "%%%", "AG_2026%", "AG_20261020_0000status0", "OC-77"])
def test_forged_status_result_with_wildcard_conversation_id_is_rejected(env, conversation_id):
    # "%" used to make ``output like '%%%'`` match the newest completed query (finding C26)
    status_query(env)
    assert unsolicited(env, conversation_id=conversation_id)


@pytest.mark.parametrize("status", ["Declined", "Failed", "Reversed", "", None])
def test_status_result_for_money_that_did_not_arrive_is_rejected(env, status):
    # ResultCode 0 only means the query worked (finding C18)
    status_query(env)
    reason = unsolicited(env, status=status)
    assert reason and "not Completed" in reason


def test_status_result_without_credit_party_is_rejected(env):
    status_query(env)
    assert "missing" in unsolicited(env, credit="")
    assert unsolicited(env, credit="999999")


# ---------------------------------------------------------------- Account Balance result
BAL_CID = "AG_20261020_0000balance1"


def balance_query(env):
    ack = {"OriginatorConversationID": "OC-9", "ConversationID": BAL_CID, "ResponseCode": "0"}
    return add(env, IR, "IR-BAL", request_description="Mpesa Balance Query", reference_doctype="Mpesa Settings",
               reference_docname=SETTINGS, status="Completed", output=str(ack))  # the connector logs str(dict)


def balance_result(conversation_id=BAL_CID, balance="Working Account|KES|481000.00|481000.00|0.00|0.00"):
    params = [{"Key": "BOCompletedTime", "Value": 20261020120000}]
    if balance:
        params.append({"Key": "AccountBalance", "Value": balance})
    return {"ResultCode": 0, "ResultDesc": "ok", "ConversationID": conversation_id,
            "ResultParameters": {"ResultParameter": params}}


@pytest.mark.parametrize("conversation_id", ["attempts", "%", "AG_20261020_c2b0001", BAL_CID[:-1]])
def test_balance_result_never_touches_another_request(env, conversation_id):
    # "attempts" matched every held C2B confirmation and marked it Completed (finding C26)
    c2b = held(env, "IR-HELD", "AG_20261020_c2b0001")
    bal = balance_query(env)
    before = (dict(c2b), dict(bal))
    env.api._handle_balance_result(Result=balance_result(conversation_id))
    assert (c2b, bal) == before
    assert not env.frappe.db.table("Mpesa Settings")[SETTINGS].get("account_balance")


def test_balance_result_without_a_balance_changes_nothing(env):
    bal = balance_query(env)
    before = dict(bal)
    env.api._handle_balance_result(Result=balance_result(balance=None))
    assert bal == before


def test_balance_result_for_our_query_is_recorded(env):
    bal = balance_query(env)
    env.api._handle_balance_result(Result=balance_result())
    assert bal["status"] == "Completed" and json.loads(bal["output"])["ConversationID"] == BAL_CID
    assert env.frappe.db.table("Mpesa Settings")[SETTINGS]["account_balance"].startswith("Working Account")


# ---------------------------------------------------------------- STK prompt sent again
NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)  # fake_frappe's now_datetime


def ago(seconds):
    return NOW - datetime.timedelta(seconds=seconds)


def express(env, **row):
    values = {"status": "In Progress", "verification_status": None, "transaction_id": None,
              "checkout_request_id": "ws_CO_old", "callback_payload": None, "result_code": None,
              "result_desc": None, "booking_attempts": 0, "settings": SETTINGS, "is_reconciled": 0,
              "timestamp": ago(600), "creation": ago(900)}
    values.update(row)
    return add(env, EXPRESS, "EXP-1", **values)


@pytest.mark.parametrize(
    "row",
    [{"status": "Completed"}, {"transaction_id": "SJK1ABC2DE"}, {"verification_status": "Pending"},
     {"verification_status": "Mismatch"}, {"verification_status": "Unverified"}],
)
def test_prompt_refused_on_paid_or_undecided_request(env, row):
    express(env, **row)
    with pytest.raises(env.frappe.ValidationError):
        env.api.initiate_stk_push(payment_gateway=f"Mpesa-{SETTINGS}", phone_number="254708374149",
                                  request_amount=100, doctype=EXPRESS, document_name="EXP-1")


@pytest.mark.parametrize(
    "row",
    [{"status": "Failed", "verification_status": "Verified"}, {"status": "Failed"}, {},
     # the last prompt came back and was settled seconds ago: it is finished
     {"status": "In Progress", "verification_status": "Verified", "timestamp": ago(5)},
     # nothing came back for two minutes: Safaricom has timed the prompt out
     {"timestamp": ago(120)},
     # first prompt, sent on submit
     {"checkout_request_id": None, "timestamp": None, "creation": ago(1)}],
)
def test_prompt_allowed_after_failure_or_on_a_fresh_request(env, row):
    express(env, **row)
    env.api._check_can_prompt_again("EXP-1")
    env.api._check_can_prompt_again("EXP-MISSING")


@pytest.mark.parametrize(
    "row", [{"timestamp": ago(5)}, {"timestamp": ago(89)}, {"timestamp": None, "creation": ago(5)}]
)
def test_prompt_refused_while_the_last_one_may_still_be_on_the_phone(env, row):
    # its callback would no longer find the request once a new CheckoutRequestID replaces it
    express(env, **row)
    with pytest.raises(env.frappe.ValidationError, match="just sent"):
        env.api._check_can_prompt_again("EXP-1")


def test_accepted_prompt_clears_the_previous_settlement(env):
    failed = json.dumps({"CheckoutRequestID": "ws_CO_old", "ResultCode": 1032})
    row = express(env, status="Failed", verification_status="Verified", callback_payload=failed,
                  result_code="1032", result_desc="Request cancelled by user", booking_attempts=2)
    env.api._stk_push_accepted(
        response={"MerchantRequestID": "m-2", "CheckoutRequestID": "ws_CO_new", "ResponseCode": "0"},
        payload={"Amount": 100, "PhoneNumber": 254708374149},
        document_name="EXP-1",
        doctype=EXPRESS,
        settings_name=SETTINGS,
    )
    assert row["checkout_request_id"] == "ws_CO_new"
    assert (row["status"], row["verification_status"], row["callback_payload"]) == ("In Progress", None, None)
    assert (row["result_code"], row["result_desc"], row["booking_attempts"]) == (None, None, 0)


@pytest.mark.parametrize(
    "settled",
    [{"status": "Completed", "verification_status": "Verified", "transaction_id": "SJK1ABC2DE", "is_reconciled": 1},
     {"verification_status": "Pending"}, {"verification_status": "Mismatch"}, {"verification_status": "Unverified"}],
)
def test_prompt_accepted_after_the_earlier_one_settled_keeps_that_result(env, settled):
    # the guard passed, then the earlier prompt's callback was stored or settled during
    # Safaricom's round trip for the new prompt: its result and receipt must survive
    row = express(env)
    env.api._check_can_prompt_again("EXP-1")
    paid = json.dumps({"CheckoutRequestID": "ws_CO_old", "ResultCode": 0, "MpesaReceiptNumber": "SJK1ABC2DE"})
    row.update(callback_payload=paid, result_code="0", **settled)
    before = dict(row)
    env.api._stk_push_accepted(
        response={"MerchantRequestID": "m-2", "CheckoutRequestID": "ws_CO_new", "ResponseCode": "0"},
        payload={"Amount": 100, "PhoneNumber": 254708374149},
        document_name="EXP-1",
        doctype=EXPRESS,
        settings_name=SETTINGS,
    )
    assert row == before
    assert (EXPRESS, "EXP-1") in env.frappe.db.locks  # read again under the lock
    assert any("ws_CO_new" in str(kw.get("message")) for _args, kw in env.frappe.errors)


def test_settled_prompt_covers_only_the_prompt_it_settled(env):
    failed_old = json.dumps({"CheckoutRequestID": "ws_CO_old", "ResultCode": 1032})
    doc = fake_frappe._dict(status="Failed", verification_status="Verified", callback_payload=failed_old)
    assert env.api._settled_prompt(doc, "ws_CO_old")  # Safaricom re-sent the settled callback
    assert not env.api._settled_prompt(doc, "ws_CO_new")  # the prompt sent again on this request
    assert env.api._settled_prompt(fake_frappe._dict(doc, status="Completed"), "ws_CO_new")
    assert env.api._settled_prompt(fake_frappe._dict(doc, transaction_id="SJK1ABC2DE"), "ws_CO_new")
    assert env.api._settled_prompt(fake_frappe._dict(doc, callback_payload=None), "ws_CO_new")
