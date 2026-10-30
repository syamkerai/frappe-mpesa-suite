"""Round 4.2: a payout M-Pesa refused (Bromak's till 813712, 2026-10-30: "Receiver party is invalid",
SFC_IC0003, TransactionID "UJU0000000"). A failed Result's TransactionID is no receipt, so it is not
stored (B2B and B2C) and the prints show none for a failed payout; the B2B form says what to do, a
draft and the Send via M-Pesa dialog warn when M-Pesa refused the same number before and nothing paid
it since; the account forms say a till is paid on its Store (or Head Office) number."""

import datetime
import importlib
import json
from pathlib import Path

import fake_frappe
import pytest
from test_claim_payees import CLAIM, env as claim_env  # noqa: F401 (fixture)
from test_fix_b2creq import REQ, connector  # noqa: F401 (fixture)
from test_money_out_flow import b2b_result, b2b_row, env as flow_env  # noqa: F401 (fixture)
from test_send_confirm import MANAGER, SUPPLIER_ACCOUNT, account, prepared, env as send_env  # noqa: F401 (fixture)
from test_send_confirm import b2b as draft_b2b

from mpsa_suite.daraja import money_out as daraja

_dict = fake_frappe._dict
B2B = "Mpesa B2B Payment Request"
ROOT = Path(__file__).resolve().parents[2] / "mpsa_suite"
AT = datetime.datetime(2026, 10, 30, 10, 52)
REFUSED = {"ResultType": 0, "ResultCode": "SFC_IC0003", "ResultDesc": "Receiver party is invalid",
           "OriginatorConversationID": "OCID-1", "ConversationID": "AG_1", "TransactionID": "UJU0000000"}


def refused_row(table, name="MB2B-0", shortcode="813712", command="BusinessBuyGoods", creation=AT, **values):
    table[name] = {"name": name, "docstatus": 1, "status": "Failed", "result_code": "SFC_IC0003",
                   "result_desc": "Receiver party is invalid", "transaction_id": "UJU0000000",
                   "command_id": command, "receiver_shortcode": shortcode, "creation": creation, **values}
    return table[name]


# ---------------------------------------------------------------- the helpers (pure)
def test_the_hint_names_the_store_number_for_a_till_and_the_number_for_a_paybill():
    till = daraja.failure_hint("SFC_IC0003", "BusinessBuyGoods")
    assert "Store Number (or Head Office number)" in till and "Query My Till" in till
    assert "fails the same way" in till
    assert daraja.failure_hint(" sfc_ic0003 ", "BusinessBuyGoods") == till
    paybill = daraja.failure_hint("SFC_IC0003", "BusinessPayBill")
    assert "paybill number" in paybill and "Store Number" not in paybill
    for code in ("1", "2001", "0", None, ""):  # sending again may work: no hint
        assert daraja.failure_hint(code, "BusinessBuyGoods") is None


def test_a_result_keeps_its_receipt_only_when_it_paid():
    assert daraja.result_receipt({"TransactionID": " UJU0000000 "}, paid=False) is None
    assert daraja.result_receipt({"TransactionID": "UJT1MBJN3E"}, paid=False) is None  # a failed B2C attempt
    assert daraja.result_receipt({"TransactionID": " QKA81LK5CY "}, paid=True) == "QKA81LK5CY"
    assert daraja.result_receipt({}, paid=True) is None and daraja.result_receipt(None, paid=True) is None


# ---------------------------------------------------------------- what is stored
def test_a_refused_b2b_result_stores_no_receipt(flow_env):
    row = b2b_row(flow_env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1",
                  command_id="BusinessBuyGoods", receiver_shortcode="813712")
    flow_env.api.b2b_result(Result=REFUSED, ref="MB2B-1")
    assert (row["status"], row["result_code"], row["transaction_id"]) == ("Failed", "SFC_IC0003", None)
    paid = b2b_row(flow_env, name="MB2B-2", status="Initiated", conversation_id="AG_2", originator_conversation_id="OCID-2")
    flow_env.api.b2b_result(Result=b2b_result(conv="AG_2", occ="OCID-2"), ref="MB2B-2")
    assert (paid["status"], paid["transaction_id"]) == ("Paid", "QKA81LK5CY")


def test_a_failed_b2c_result_stores_no_receipt(connector):
    connector.frappe.db.table(REQ)["B2C-1"]["status"] = "Initiated"
    connector.conn.handle_callback(
        {"ResultType": 0, "ResultCode": 1, "ResultDesc": "The balance is insufficient for the transaction.",
         "OriginatorConversationID": "OCID-1", "ConversationID": "AG_1", "TransactionID": "UJT1MBJN3E"})
    connector.commit()
    row = connector.frappe.db.table(REQ)["B2C-1"]
    assert row["status"] == "Failed" and not row.get("transaction_id")


def test_a_paid_b2c_result_keeps_its_receipt(connector):
    connector.frappe.db.table(REQ)["B2C-1"]["status"] = "Initiated"
    connector.conn.handle_callback(
        {"ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
         "OriginatorConversationID": "OCID-1", "ConversationID": "AG_1", "TransactionID": "UJT9PAIDOK"})
    row = connector.frappe.db.table(REQ)["B2C-1"]
    assert (row["status"], row["transaction_id"]) == ("Paid", "UJT9PAIDOK")


def test_a_schedule_line_to_a_refused_number_warns_and_its_failure_says_what_to_do(flow_env, monkeypatch):
    schedule = importlib.import_module("mpsa_suite.money_out.schedule")
    table = flow_env.frappe.db.table(B2B)
    refused_row(table, shortcode="888880", command="BusinessPayBill")  # MSA-1's paybill
    refused_row(table, name="MB2B-9", shortcode="813712")
    supplier_line = _dict(route="B2B Paybill", party="Total Kenya", party_type="Supplier", payee_account="MSA-1")
    (warning,) = schedule._refused(supplier_line)
    assert warning.startswith("MB2B-0 to 888880 failed: M-Pesa does not know this paybill number")
    one_off = _dict(route="B2B Till", party=None, one_off_type="Till (Buy Goods)", one_off_shortcode="813712")
    assert schedule._refused(one_off)[0].startswith("MB2B-9 to 813712 failed: M-Pesa refused this till number")
    assert schedule._refused(_dict(route="B2C Phone", party="EMP-1", party_type="Employee")) == []
    monkeypatch.setattr(schedule, "_paid_outside", lambda line: ["paid outside"])
    assert schedule._warnings(one_off) == [schedule._refused(one_off)[0], "paid outside"]
    failed = _dict(name="MB2B-9", docstatus=1, status="Failed", transaction_id=None, error_message=None,
                   result_desc="Receiver party is invalid", result_code="SFC_IC0003", command_id="BusinessBuyGoods",
                   trans_completed_time=None, entry_status=None)
    problem = schedule._mirror(failed)["last_problem"]
    assert problem.startswith("Receiver party is invalid M-Pesa refused this till number") and "Store Number" in problem
    assert schedule._mirror(_dict(failed, result_code="1"))["last_problem"] == "Receiver party is invalid"


def test_a_schedule_line_keeps_no_receipt_of_a_failed_payout(flow_env):
    schedule = importlib.import_module("mpsa_suite.money_out.schedule")
    values = _dict(name="MB2B-1", docstatus=1, status="Failed", transaction_id="UJU0000000", error_message=None,
                   result_desc="Receiver party is invalid", trans_completed_time=None, entry_status=None)
    assert schedule._mirror(values)["receipt"] is None
    assert schedule._mirror(_dict(values, status="Paid", transaction_id="QKA81LK5CY"))["receipt"] == "QKA81LK5CY"


# ---------------------------------------------------------------- the form and the send dialog
def test_the_failed_request_says_what_to_do(flow_env):
    doc = fake_frappe.FakeDoc(flow_env.frappe, B2B, refused_row(flow_env.frappe.db.table(B2B), name="MB2B-1"))
    assert "Store Number" in flow_env.b2b.receiver_hint(doc)
    doc = fake_frappe.FakeDoc(flow_env.frappe, B2B, refused_row(flow_env.frappe.db.table(B2B), name="MB2B-5",
                                                                result_code="1"))
    assert flow_env.b2b.receiver_hint(doc) is None  # a balance or other failure: sending again may work


def test_a_draft_to_a_number_mpesa_refused_warns_until_something_paid_it(flow_env):
    table = flow_env.frappe.db.table(B2B)
    refused_row(table)
    draft = fake_frappe.FakeDoc(flow_env.frappe, B2B, b2b_row(flow_env, name="MB2B-2", docstatus=0,
                                                              command_id="BusinessBuyGoods", receiver_shortcode="813712"))
    hint = flow_env.b2b.receiver_hint(draft)
    assert hint.startswith("MB2B-0 to 813712 failed: M-Pesa refused this till number") and "Store Number" in hint
    assert flow_env.b2b.refused_before("813712", "BusinessPayBill") is None  # another command, another lookup
    assert flow_env.b2b.refused_before("813713", "BusinessBuyGoods") is None
    # made before the refusal but paid after it (an Unknown one confirmed later): paid since, by when it paid
    table["MB2B-3"] = {"name": "MB2B-3", "docstatus": 1, "status": "Paid", "command_id": "BusinessBuyGoods",
                       "receiver_shortcode": "813712", "creation": AT - datetime.timedelta(hours=1),
                       "trans_completed_time": AT + datetime.timedelta(hours=1)}
    assert flow_env.b2b.receiver_hint(draft) is None  # paid since: whatever was wrong is fixed
    # refused again after that payment: the latest refusal is the one named
    refused_row(table, name="MB2B-4", creation=AT + datetime.timedelta(hours=2))
    assert flow_env.b2b.receiver_hint(draft).startswith("MB2B-4 to 813712 failed:")
    table["MB2B-3"]["trans_completed_time"] = AT - datetime.timedelta(minutes=30)  # paid before both refusals
    del table["MB2B-4"]
    assert flow_env.b2b.receiver_hint(draft).startswith("MB2B-0 to 813712 failed:")


def test_the_send_dialog_warns_before_paying_or_approving_a_refused_number(send_env):
    refused_row(send_env.frappe.db.table(B2B))
    draft_b2b(send_env, command_id="BusinessBuyGoods", receiver_shortcode="813712", account_reference=None)
    (warning,) = prepared(send_env)["warnings"]
    assert warning.startswith("MB2B-0 to 813712 failed:") and "Store Number" in warning
    send_env.frappe.session.user, send_env.frappe.user_roles = MANAGER, {"M-Pesa Manager"}  # who approves accounts
    account(send_env, account_type="Till (Buy Goods)", shortcode="813712", account_number=None)
    assert any(w.startswith("MB2B-0 to 813712 failed:") for w in prepared(send_env, SUPPLIER_ACCOUNT, "MSA-9")["warnings"])
    account(send_env, name="MSA-8", account_type="Paybill", shortcode="813712")  # a paybill: another lookup
    assert prepared(send_env, SUPPLIER_ACCOUNT, "MSA-8")["warnings"] == []
    refused_row(send_env.frappe.db.table(B2B), name="MB2B-6", shortcode="888881", command="BusinessPayBill")
    account(send_env, name="MSA-7", account_type="Paybill", shortcode="888881")
    (warning,) = prepared(send_env, SUPPLIER_ACCOUNT, "MSA-7")["warnings"]
    assert warning.startswith("MB2B-6 to 888881 failed: M-Pesa does not know this paybill number")


# ---------------------------------------------------------------- the prints
@pytest.fixture
def pp(claim_env):
    return importlib.import_module("mpsa_suite.money_out.payment_print")


def test_the_prints_show_no_receipt_or_charge_for_a_failed_payout(claim_env, pp, monkeypatch):
    monkeypatch.setattr(pp, "_sent_by", lambda doctype, name: None)
    db = claim_env.frappe.db
    # stored before this release: a failed B2C attempt's ID, and B2B's placeholder
    db.table(REQ).update({
        "REQ-1": {"name": "REQ-1", "docstatus": 1, "status": "Failed", "transaction_id": "UJT1MBJN3E", "amount": 1000,
                  "reference_doctype": CLAIM, "reference_name": "HR-EXP-1", "creation": AT,
                  "result_desc": "The balance is insufficient for the transaction."},
        "REQ-2": {"name": "REQ-2", "docstatus": 1, "status": "Paid", "transaction_id": "UJT2", "amount": 500,
                  "reference_doctype": CLAIM, "reference_name": "HR-EXP-1", "creation": AT}})
    assert [(r.payout, r.receipt) for r in pp.payment_rows(CLAIM, "HR-EXP-1")] == [("REQ-1", ""), ("REQ-2", "UJT2")]
    (block,) = pp.mpsa_payout_proof(fake_frappe.FakeDoc(claim_env.frappe, REQ, db.table(REQ)["REQ-1"])).blocks
    assert (block.status, block.receipt, block.charge, block.sms) == ("Failed", "", None, "")
    b2b = refused_row(db.table(B2B), name="MB2B-1", debit_party_charges="Buy Goods Charge|KES|2.00",
                      purchase_order="PO-1", amount=15700)
    (block,) = pp.mpsa_payout_proof(fake_frappe.FakeDoc(claim_env.frappe, B2B, b2b)).blocks
    assert (block.status, block.receipt, block.charge) == ("Failed", "", None)
    db.columns |= {(B2B, "purchase_order")}
    assert [(r.payout, r.status, r.receipt) for r in pp.payment_rows("Purchase Order", "PO-1")] == [
        ("MB2B-1", "Failed", "")]
    # with B2C Charge Bands set, a paid payout's charge is estimated; a failed one's is not
    statement = importlib.import_module("mpsa_suite.money_out.statement")
    monkeypatch.setattr(statement, "charge_bands", lambda: ["band"])
    monkeypatch.setattr(statement, "band_charge", lambda bands, amount: 13.0)
    (paid,) = pp.mpsa_payout_proof(fake_frappe.FakeDoc(claim_env.frappe, REQ, db.table(REQ)["REQ-2"])).blocks
    (failed,) = pp.mpsa_payout_proof(fake_frappe.FakeDoc(claim_env.frappe, REQ, db.table(REQ)["REQ-1"])).blocks
    assert (paid.charge, paid.charge_how, failed.charge) == (13.0, "est.", None)
    # an entry carrying the failed B2C attempt's old ID is not that payout's proof
    entry = fake_frappe.FakeDoc(claim_env.frappe, "Payment Entry", {
        "name": "PE-2", "docstatus": 1, "reference_no": "UJT1MBJN3E", "party_name": "Jane", "paid_amount": 1000,
        "mode_of_payment": "Cash", "references": []})
    assert pp.mpsa_payout_proof(entry).blocks[0].get("payout") is None
    # a Payment Entry carrying the placeholder is its own proof, never the failed request's
    entry = fake_frappe.FakeDoc(claim_env.frappe, "Payment Entry", {
        "name": "PE-1", "docstatus": 1, "reference_no": "UJU0000000", "party_name": "Bromak", "paid_amount": 15700,
        "mode_of_payment": "Bank", "references": []})
    (block,) = pp.mpsa_payout_proof(entry).blocks
    assert (block.name, block.to, block.get("payout")) == ("Bromak", "Bank", None)


# ---------------------------------------------------------------- the forms' texts
def test_the_account_forms_say_a_till_is_paid_on_its_store_number():
    for folder, field in (("supplier_m_pesa_account", "account_type"), ("supplier_m_pesa_account", "shortcode"),
                          ("employee_m_pesa_account", "account_type"), ("employee_m_pesa_account", "shortcode"),
                          ("m_pesa_claim_payee", "shortcode")):
        doc = json.loads((ROOT / "mpsa_suite" / "doctype" / folder / f"{folder}.json").read_text(encoding="utf-8"))
        (f,) = [f for f in doc["fields"] if f["fieldname"] == field]
        assert "Store Number (or Head Office number)" in f["description"], (folder, field)
    script = (ROOT / "mpsa_suite" / "doctype" / "mpesa_b2b_payment_request" / "mpesa_b2b_payment_request.js").read_text(
        encoding="utf-8")
    # escaped once, in a div so that show_message inserts it as HTML (text alone is escaped again)
    assert "__onload || {}).receiver_hint" in script and "`<div>${frappe.utils.escape_html(receiver_hint)}</div>`" in script
