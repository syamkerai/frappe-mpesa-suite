"""Transaction Status for B2B payouts (handover F2.4) and status answers for payout receipts
(F11), on an in-memory frappe, fed Trident's live answer for the till payment MB2B-26-10-000002
(Integration Request 35rkf224m5, 27 Oct 2026): ReceiptNo UJRSN5BOEH, CreditPartyName
"835811 - ASTRAL INDUSTRIES LTD HQ", DebitPartyCharges "Business Buy Goods Charge|KES|3.00",
FinalisedTime 20261027143900, a placeholder top-level TransactionID "UJR1000000", and the
payout's own IDs inside ResultParameters.
"""

import copy
import datetime
import importlib
import json
import re
import sys
import types
from pathlib import Path

import pytest

import fake_frappe

REPO = Path(__file__).resolve().parents[2]
B2B, B2C = "Mpesa B2B Payment Request", "B2C Disbursement Request"
QUERY = "Mpesa B2C Status Query"
IR = "Integration Request"
SETTINGS = {"name": "985401", "initiator_name": "apiop", "security_credential": "Q1JFRA==",
            "business_shortcode": "985401", "till_number": "", "sandbox": 0, "use_b2b": 1, "use_b2c": 1}
QUERY_CID, QUERY_OCID = "AG_20261027_010020310zktqk52e3mr", "1e2f-4c6a-9b3d-7d0c5e8f9a1b2c3d4e"
PAYOUT_CID, PAYOUT_OCID = "AG_20261027_010020310vef3ya36rhh", "8a05-481f-85be-737ec5465783134990"
NAME = "MB2B-26-10-000002"

LIVE = {
    "ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
    "OriginatorConversationID": QUERY_OCID, "ConversationID": QUERY_CID, "TransactionID": "UJR1000000",
    "ResultParameters": {"ResultParameter": [
        {"Key": "DebitPartyName", "Value": "985401 - TRIDENT PLUMBERS LTD"},
        {"Key": "CreditPartyName", "Value": "835811 - ASTRAL INDUSTRIES LTD HQ"},
        {"Key": "OriginatorConversationID", "Value": PAYOUT_OCID},
        {"Key": "InitiatedTime", "Value": 20261027143900},
        {"Key": "DebitAccountType", "Value": "MMF Account for Organization"},
        {"Key": "DebitPartyCharges", "Value": "Business Buy Goods Charge|KES|3.00"},
        {"Key": "TransactionReason"},
        {"Key": "ReasonType", "Value": "Business Buy Goods via API"},
        {"Key": "TransactionStatus", "Value": "Completed"},
        {"Key": "FinalisedTime", "Value": 20261027143900},
        {"Key": "Amount", "Value": 50.0},
        {"Key": "ConversationID", "Value": PAYOUT_CID},
        {"Key": "ReceiptNo", "Value": "UJRSN5BOEH"},
    ]},
    "ReferenceData": {"ReferenceItem": {"Key": "Occasion", "Value": f"Status {NAME}"}},
}
ACK = {"OriginatorConversationID": QUERY_OCID, "ConversationID": QUERY_CID, "ResponseCode": "0",
       "ResponseDescription": "Accept the service request successfully."}


def live(**changes):
    """The live answer with ResultParameters values changed (None drops the key)."""
    result = copy.deepcopy(LIVE)
    params = result["ResultParameters"]["ResultParameter"]
    for key, value in changes.items():
        for p in list(params):
            if p["Key"] == key:
                params.remove(p) if value is None else p.update(Value=value)
    return result


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._data


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    stub = types.ModuleType("mpsa_suite.utils.encoding_initiator_password")
    stub.generate_security_credential = lambda *a, **kw: ""
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    try:
        frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
        frappe.db.db_type = "fake"
        frappe.logger = lambda *a, **kw: types.SimpleNamespace(info=lambda *a, **kw: None)
        common = importlib.import_module("mpsa_suite.money_out.common")
        status = importlib.import_module("mpsa_suite.money_out.b2b_status")
        api = importlib.import_module("mpsa_suite.money_out.api")
        statement = importlib.import_module("mpsa_suite.money_out.statement")
        schedule = importlib.import_module("mpsa_suite.money_out.schedule")
        supplier_accounts = importlib.import_module("mpsa_suite.money_out.supplier_accounts")
        calls = fake_frappe._dict(sent=[], responses=[], names=[], lines=[], published=[])
        monkeypatch.setattr(common, "bearer_headers", lambda s, force=False: {"Authorization": "Bearer tok"})

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.sent.append((url, json))
            nxt = calls.responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        monkeypatch.setattr(common.requests, "post", fake_post)
        monkeypatch.setattr(supplier_accounts, "record_receiver_name", lambda doc, name: calls.names.append((doc.name, name)))
        monkeypatch.setattr(statement, "after_payout", lambda doctype, name: calls.lines.append((doctype, name)))
        monkeypatch.setattr(schedule, "sync_line_later", lambda doc: None)
        frappe.publish_realtime = lambda *a, **kw: calls.published.append(kw)
        frappe.db.table("Mpesa Settings")[SETTINGS["name"]] = dict(SETTINGS)
        yield fake_frappe._dict(frappe=frappe, status=status, api=api, calls=calls)
    finally:
        fake_frappe.uninstall(saved)


def payout(env, **values):
    row = {"name": NAME, "docstatus": 1, "status": "Paid", "amount": 50.0, "mpesa_settings": "985401",
           "command_id": "BusinessBuyGoods", "receiver_shortcode": "835811", "supplier": "Astral Industries Ltd",
           "supplier_mpesa_account": "MSA-00002", "transaction_id": "UJRSN5BOEH", "result_code": "0",
           "conversation_id": PAYOUT_CID, "originator_conversation_id": PAYOUT_OCID,
           "receiver_public_name": None, "debit_party_charges": None,
           "trans_completed_time": datetime.datetime(2026, 10, 27, 14, 39, 2), "completed_time_estimated": 1,
           "creation": datetime.datetime(2026, 10, 18, 11, 0)}
    row.update(values)
    env.frappe.db.table(B2B)[NAME] = row
    return row


def ask(env):
    env.calls.responses.append(Resp(ACK))
    return env.status.start_query(NAME)


# ================================================================ asking (F2.4)
def test_a_paid_payout_is_asked_about_by_its_receipt(env):
    payout(env)
    name = ask(env)
    url, body = env.calls.sent[0]
    assert url.endswith("/mpesa/transactionstatus/v1/query")
    assert body["TransactionID"] == "UJRSN5BOEH" and "OriginalConversationID" not in body
    assert body["PartyA"] == "985401" and body["IdentifierType"] == "4"
    assert f"b2c_status_result?ref={name}" in body["ResultURL"]
    assert f"status_queue_timeout?ref={name}" in body["QueueTimeOutURL"]
    q = env.frappe.db.table(QUERY)[name]
    assert (q["b2b_request"], q["status"], q["conversation_id"]) == (NAME, "Sent", QUERY_CID)
    assert not q.get("b2c_request")


def test_a_payout_whose_result_never_came_is_asked_about_by_its_originator_id(env):
    payout(env, status="Initiated", transaction_id=None, result_code=None)
    ask(env)
    body = env.calls.sent[0][1]
    assert body["OriginalConversationID"] == PAYOUT_OCID and "TransactionID" not in body


@pytest.mark.parametrize("values, message", [
    ({"status": "Not Initiated"}, "only a payment M-Pesa may have made"),
    ({"status": "Failed"}, "only a payment M-Pesa may have made"),
    ({"docstatus": 0}, "Only a submitted B2B request"),
    ({"status": "Unknown", "transaction_id": None, "originator_conversation_id": None, "conversation_id": None},
     "Nothing to query by"),
])
def test_what_cannot_be_asked_about_is_refused(env, values, message):
    payout(env, **values)
    with pytest.raises(env.frappe.ValidationError, match=message):
        env.status.start_query(NAME)
    assert env.calls.sent == []


def test_a_query_still_waiting_is_not_sent_again(env):
    payout(env)
    ask(env)
    with pytest.raises(env.frappe.ValidationError, match="still waiting"):
        env.status.start_query(NAME)


def test_check_status_needs_a_payout_or_admin_role(env):
    payout(env)
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.PermissionError):
        env.api.check_b2b_status(NAME)
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.calls.responses.append(Resp(ACK))
    assert env.api.check_b2b_status(NAME)


# ================================================================ the answer (F2.4)
def test_the_live_answer_fills_what_the_tills_result_left_out(env):
    row = payout(env)
    name = ask(env)
    env.api.b2c_status_result(Result=LIVE, ref=name)
    assert row["receiver_public_name"] == "835811 - ASTRAL INDUSTRIES LTD HQ"
    assert row["debit_party_charges"] == "Business Buy Goods Charge|KES|3.00"
    assert row["trans_completed_time"] == datetime.datetime(2026, 10, 27, 14, 39) and row["completed_time_estimated"] == 0
    assert row["transaction_id"] == "UJRSN5BOEH", "the placeholder TransactionID is never taken"
    q = env.frappe.db.table(QUERY)[name]
    assert (q["outcome"], q["applied"], q["receipt_no"]) == ("Paid", 1, "UJRSN5BOEH")
    assert (q["debit_party_charges"], q["debit_account_type"]) == (
        "Business Buy Goods Charge|KES|3.00", "MMF Account for Organization")
    assert env.calls.names == [(NAME, "835811 - ASTRAL INDUSTRIES LTD HQ")]  # the receiver-name check
    assert env.calls.lines == [(B2B, NAME)]  # the charge line
    assert any("filled from Transaction Status query" in text for _dt, _dn, text in env.frappe.comments)

    env.api.b2c_status_result(Result=LIVE, ref=name)  # Safaricom sends it again
    assert env.calls.names == [(NAME, "835811 - ASTRAL INDUSTRIES LTD HQ")] and env.calls.lines == [(B2B, NAME)]


@pytest.mark.parametrize("changes", [
    {"ReceiptNo": "UJRSN5BFQ7"},                                    # another receipt
    {"ConversationID": "AG_20261027_other", "OriginatorConversationID": "other-1"},  # another payout's IDs
    {"ConversationID": None, "OriginatorConversationID": None},       # no IDs of the payout at all
    {"Amount": 60.0},                                               # another amount
    {"CreditPartyName": "999999 - SOMEONE ELSE"},                   # another receiver
    {"TransactionStatus": "Pending"},
])
def test_an_answer_that_is_not_for_this_payout_changes_nothing(env, changes):
    row = payout(env)
    before = dict(row)
    name = ask(env)
    env.api.b2c_status_result(Result=live(**changes), ref=name)
    assert row == before
    assert env.frappe.db.table(QUERY)[name]["outcome"] == "Unresolved"
    assert env.calls.names == [] and env.calls.lines == []


def test_what_the_result_stated_is_kept(env):
    row = payout(env, receiver_public_name="835811 - ASTRAL INDUSTRIES", debit_party_charges="Business Buy Goods Charge|KES|2.00",
                 trans_completed_time=datetime.datetime(2026, 10, 27, 14, 38, 58), completed_time_estimated=0)
    before = dict(row)
    name = ask(env)
    env.api.b2c_status_result(Result=LIVE, ref=name)
    assert row == before
    q = env.frappe.db.table(QUERY)[name]
    assert (q["outcome"], q["applied"]) == ("Paid", 0) and "Nothing was missing" in q["outcome_reason"]
    assert env.calls.lines == []


def test_a_payout_whose_result_never_came_is_settled_by_the_answer(env):
    row = payout(env, status="Initiated", transaction_id=None, result_code=None, completed_time_estimated=0,
                 trans_completed_time=None)
    name = ask(env)
    env.api.b2c_status_result(Result=LIVE, ref=name)
    assert (row["status"], row["result_code"], row["transaction_id"]) == ("Paid", "0", "UJRSN5BOEH")
    assert row["trans_completed_time"] == datetime.datetime(2026, 10, 27, 14, 39)
    assert row["receiver_public_name"] == "835811 - ASTRAL INDUSTRIES LTD HQ"
    assert (row["entry_status"], row["entry_note"]) == ("Entry Not Made", "Booking the entry…") and row["entry_attempted_on"]
    booked = [kw for method, kw in env.frappe.enqueued if method.endswith("b2b.book_payment_entry")]
    assert booked == [dict(queue="short", timeout=300, enqueue_after_commit=True, name=NAME)]
    assert env.calls.names == [(NAME, "835811 - ASTRAL INDUSTRIES LTD HQ")]

    # its real Result comes after all: recorded already, so never booked twice
    b2b = importlib.import_module("mpsa_suite.money_out.b2b")
    b2b.handle_result({"ResultCode": "0", "ConversationID": PAYOUT_CID, "TransactionID": "UJRSN5BOEH"}, NAME)
    assert len([m for m, _kw in env.frappe.enqueued if m.endswith("b2b.book_payment_entry")]) == 1


def test_a_failed_answer_settles_a_payout_whose_result_never_came_as_failed(env):
    row = payout(env, status="Unknown", transaction_id=None, result_code=None)
    name = ask(env)
    env.api.b2c_status_result(Result=live(TransactionStatus="Failed"), ref=name)
    assert (row["status"], row["result_code"]) == ("Failed", "TS:Failed")
    assert not [m for m, _kw in env.frappe.enqueued if m.endswith("book_payment_entry")]


def test_a_failed_answer_for_a_paid_payout_changes_nothing(env):
    row = payout(env)
    before = dict(row)
    name = ask(env)
    env.api.b2c_status_result(Result=live(TransactionStatus="Failed"), ref=name)
    assert row == before
    assert "Left unchanged" in env.frappe.db.table(QUERY)[name]["outcome_reason"]


# ================================================================ when to ask again
def query_row(env, minutes_ago, outcome=None, status="Result Received"):
    name = f"MB2CQ-{len(env.frappe.db.table(QUERY)) + 1}"
    env.frappe.db.table(QUERY)[name] = {"name": name, "b2b_request": NAME, "status": status, "outcome": outcome,
                                        "creation": env.frappe.utils.now_datetime() - datetime.timedelta(minutes=minutes_ago)}


def test_a_paid_payout_is_asked_again_while_something_is_missing(env):
    payout(env)
    assert env.status.may_ask(NAME)
    query_row(env, 5, "Unresolved")
    assert not env.status.may_ask(NAME), "the last query was five minutes ago"
    env.frappe.db.table(QUERY).clear()
    for minutes in (40, 30, 20):
        query_row(env, minutes, "Unresolved")
    assert not env.status.may_ask(NAME), "three queries in all"
    env.frappe.db.table(QUERY).clear()
    query_row(env, 40, "Paid")
    assert not env.status.may_ask(NAME), "Safaricom gave what it had"


def test_nothing_is_asked_when_nothing_is_missing_or_the_payout_is_old(env):
    payout(env, receiver_public_name="835811 - X", debit_party_charges="x|KES|3.00", completed_time_estimated=0)
    assert not env.status.may_ask(NAME)
    payout(env, creation=datetime.datetime(2026, 10, 15, 11, 0))
    assert not env.status.may_ask(NAME)
    payout(env, status="Initiated")
    assert not env.status.may_ask(NAME)


def test_the_results_job_asks_quietly(env):
    payout(env, receiver_public_name="835811 - X", debit_party_charges="x|KES|3.00", completed_time_estimated=0)
    assert env.status.auto_query(NAME) is None and env.calls.sent == []
    payout(env)
    env.calls.responses.append(RuntimeError("boom"))  # something unexpected while sending
    assert env.status.auto_query(NAME) is None
    assert "M-Pesa B2B status query failed" in env.frappe.errors[0][1]["title"]


def test_sweep_asks_about_payouts_with_something_missing(env, monkeypatch):
    payout(env)
    asked = []
    monkeypatch.setattr(env.status, "auto_query", lambda name: asked.append(name) or "MB2CQ-1")
    env.frappe.db.table_exists = lambda doctype, cached=True: True
    env.status.sweep()
    assert asked == [NAME]


# ================================================================ Mpesa Settings → Check Transaction Status (F11)
@pytest.fixture
def mpesa(env, monkeypatch):
    api = importlib.import_module("mpsa_suite.mpsa_suite.api.m_pesa_api")
    monkeypatch.setattr(fake_frappe.FakeDoc, "save", lambda self, **kw: None, raising=False)
    logged = []
    common = importlib.import_module("mpsa_suite.money_in.common")
    monkeypatch.setattr(common, "log_match", lambda *a, **kw: logged.append((a, kw)))
    env.logged = logged
    return api


def asked(env, receipt="UJRSN5BOEH", status="Completed", conversation_id=QUERY_CID):
    env.frappe.db.table(IR)["IR-ASK"] = {
        "name": "IR-ASK", "integration_request_service": "Mpesa Transaction Status", "status": status,
        "owner": "finance@example.com", "data": json.dumps({"mpesa_settings": "985401", "transaction_id": receipt}),
        "output": json.dumps(dict(ACK, ConversationID=conversation_id)) if status == "Completed" else None}


def answer(env, result=LIVE, ref="IR-ASK", minutes_ago=0):
    env.frappe.db.table(IR)["IR-RES"] = {
        "name": "IR-RES", "integration_request_service": "Mpesa Transaction Status Result Callback",
        "status": "Queued", "data": json.dumps({"Result": result}),
        "creation": env.frappe.utils.now_datetime() - datetime.timedelta(minutes=minutes_ago),
        "url": f"https://erp.example.com/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api."
               f"handle_transaction_status_result?ref={ref}&t=token" if ref else None}
    return env.frappe.db.table(IR)["IR-RES"]


def told(env):
    return [(p["user"], p["message"]["status"], p["message"]["message"]) for p in env.calls.published
            if p.get("event") == "mpesa_transaction_status_update"]


def test_the_till_receipt_is_answered_as_the_payout_and_fills_it(env, mpesa):
    row = payout(env)
    asked(env)
    ir = answer(env)
    mpesa.process_mpesa_integration_request("IR-RES")
    assert ir["status"] == "Completed" and env.frappe.errors == []
    summary = json.loads(ir["output"])
    assert (summary["kind"], summary["payout"], summary["ReceiptNo"]) == ("B2B", NAME, "UJRSN5BOEH")
    assert summary["filled"] == ["receiver_public_name", "debit_party_charges", "trans_completed_time"]
    ((user, status, message),) = told(env)
    assert (user, status) == ("finance@example.com", "success")
    assert message.startswith(f"Payout {NAME} Completed: KES 50.00 to 835811 - ASTRAL INDUSTRIES LTD HQ, "
                              "charge KES 3.00, 27-10-2026 14:39.")
    assert row["debit_party_charges"] == "Business Buy Goods Charge|KES|3.00"
    assert not env.frappe.db.table("Mpesa C2B Payment Register"), "a payout receipt is no C2B receipt"


def test_a_b2c_payout_receipt_is_answered_with_the_payout(env, mpesa):
    env.frappe.db.table(B2C)["B2C-26-10-000001"] = {"name": "B2C-26-10-000001", "status": "Paid",
                                                    "transaction_id": "UJRASBFI9Y", "creation": datetime.datetime(2026, 10, 27)}
    asked(env, receipt="UJRASBFI9Y")
    ir = answer(env, live(ReceiptNo="UJRASBFI9Y", Amount=100.0, CreditPartyName="254712345678 - TESTER",
                          DebitPartyCharges=None, ConversationID=None, OriginatorConversationID=None))
    mpesa.process_mpesa_integration_request("IR-RES")
    assert ir["status"] == "Completed" and env.frappe.errors == []
    assert json.loads(ir["output"])["kind"] == "B2C"
    ((_user, status, message),) = told(env)
    assert status == "success" and message.startswith("Payout B2C-26-10-000001 Completed: KES 100.00 to 254712345678")


def test_an_answer_about_another_receipt_tells_the_asker(env, mpesa):
    payout(env)
    asked(env, receipt="UJRSN5BFQ7")
    ir = answer(env)
    mpesa.process_mpesa_integration_request("IR-RES")
    assert ir["status"] == "Failed" and env.frappe.errors == []
    ((user, status, message),) = told(env)
    assert (user, status) == ("finance@example.com", "error") and "we asked about UJRSN5BFQ7" in message


def test_a_result_whose_querys_ack_is_not_recorded_yet_waits(env, mpesa):
    asked(env, status="Queued")
    ir = answer(env)
    mpesa.process_mpesa_integration_request("IR-RES")
    assert ir["status"] == "Queued" and json.loads(ir["output"]) == {"waiting_for": "IR-ASK", "tries": 1}
    assert told(env) == [] and env.frappe.errors == []
    mpesa.process_mpesa_integration_request("IR-RES")
    assert json.loads(ir["output"])["tries"] == 2
    # the ack never came: rejected on the last try, and the asker told
    mpesa.process_mpesa_integration_request("IR-RES", final=True)
    assert ir["status"] == "Failed" and "never recorded" in ir["output"]
    assert told(env)[0][:2] == ("finance@example.com", "error") and env.frappe.errors


def test_a_result_the_ack_does_not_name_is_rejected(env, mpesa):
    asked(env, conversation_id="AG_20261027_someone_else")
    ir = answer(env)
    mpesa.process_mpesa_integration_request("IR-RES")
    assert ir["status"] == "Failed" and "No Transaction Status query of ours" in ir["output"]
    assert told(env) == [] and "rejected" in env.frappe.errors[0][0][1]


def test_money_paid_from_our_shortcode_with_no_erp_payout_is_flagged(env, mpesa):
    asked(env)
    ir = answer(env)  # no B2B request holds UJRSN5BOEH: the portal paid it
    mpesa.process_mpesa_integration_request("IR-RES")
    assert ir["status"] == "Completed" and "outside the ERP" in ir["output"]
    ((_user, status, message),) = told(env)
    assert status == "warning" and "from 985401" in message
    ((result, method), kw) = env.logged[0]
    assert (result, method, kw["receipt"]) == ("Ambiguous", "Bank Reconciliation", "UJRSN5BOEH")
    assert env.frappe.errors == []


def test_a_handled_result_is_not_handled_again(env, mpesa):
    payout(env)
    asked(env)
    ir = answer(env)
    mpesa.process_mpesa_integration_request("IR-RES")
    mpesa.process_mpesa_integration_request("IR-RES")  # the retry sweep reaches it too
    assert len(told(env)) == 1 and ir["status"] == "Completed"


def test_waiting_results_are_tried_again_and_given_up_after_ten_minutes(env, mpesa, monkeypatch):
    tried = []
    monkeypatch.setattr(mpesa, "process_mpesa_integration_request", lambda name, final=False: tried.append((name, final)))
    for name, minutes in (("IR-NEW", 0.5), ("IR-5", 5), ("IR-11", 11)):
        env.frappe.db.table(IR)[name] = {"name": name, "status": "Queued", "creation":
                                         env.frappe.utils.now_datetime() - datetime.timedelta(minutes=minutes),
                                         "integration_request_service": "Mpesa Transaction Status Result Callback"}
    mpesa.retry_waiting_status_results()
    assert sorted(tried) == [("IR-11", True), ("IR-5", False)]


def test_every_status_event_python_publishes_is_one_the_desk_listens_for():
    """The dialog on Mpesa Settings and on the C2B register list closes on Python's event (F11)."""
    js = " ".join(p.read_text(encoding="utf-8") for p in (REPO / "mpsa_suite").rglob("*.js"))
    listened = set(re.findall(r"""frappe\.realtime\.on\(\s*["']([\w]+)["']""", js))
    python = {p: p.read_text(encoding="utf-8") for p in (REPO / "mpsa_suite").rglob("*.py") if "test" not in p.name}
    published = {e for text in python.values() for e in re.findall(r"""event=["'](mpesa_transaction_status\w*)["']""", text)}
    assert "mpesa_transaction_status_update" in listened
    m_pesa_api = (REPO / "mpsa_suite/mpsa_suite/api/m_pesa_api.py").read_text(encoding="utf-8")
    assert 'STATUS_EVENT = "mpesa_transaction_status_update"' in m_pesa_api
    assert published <= listened, f"published but nobody listens: {published - listened}"
