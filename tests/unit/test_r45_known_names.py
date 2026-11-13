"""Round 4.5: know who you pay. Daraja has no Hakikisha, so every name M-Pesa gave a phone, till or
paybill is kept (M-Pesa Known Name, money_out.known_names) and shown wherever a payout is typed,
approved or sent.

Covers: the keys (the payee accounts' format; a paybill's account number left out); what one payout
says (another number's name never kept, a masked phone by its digits); recording from the B2C Result
callback and from B2B Results and status answers, once per payout, the latest name kept and a late
older one only counted; salary-only names (a salary never shows through a record others see); the
recording never stopping the payout; the backfill (latest name, first and last seen, count;
idempotent; quick on 3,000 payouts) and its patch; lookup, verdict and the badges' words; the desk
endpoint (who may read it, salary names hidden, the expected name found from an approved account,
a claim payee or the party); the Send via M-Pesa summary's names, counts and acknowledgement, which
confirm_send enforces; the claim payees' lookups unchanged; the DocType, its permissions and hooks;
and the desk scripts (node)."""

import datetime
import importlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import fake_frappe
import pytest
from test_b2b_status import env as status_env  # noqa: F401 (fixture)
from test_b2b_status import live, payout
from test_claim_payees import CLAIM, LINE, PHONE, REQ, TILL, claim, payee
from test_claim_payees import env as claims_env  # noqa: F401 (fixture)
from test_fix_b2creq import connector  # noqa: F401 (fixture)
from test_money_out_flow import b2b_result, b2b_row
from test_money_out_flow import env as flow_env  # noqa: F401 (fixture)
from test_send_confirm import B2B, B2C, SUPPLIER_ACCOUNT, account, b2b, disbursement, prepared
from test_send_confirm import env as send_env  # noqa: F401 (fixture)

from mpsa_suite.daraja import money_out as daraja

_dict = fake_frappe._dict
ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "mpsa_suite"
KNOWN = "M-Pesa Known Name"
BANK_PAYBILL = "M-Pesa Bank Paybill"
AT = datetime.datetime(2026, 10, 29, 9, 0)
NODE = shutil.which("node")


def kn():
    return importlib.import_module("mpsa_suite.money_out.known_names")


def request(env, name, phone, received, minutes=0, status="Paid", **values):
    """A B2C Disbursement Request as its Result left it."""
    row = {"name": name, "docstatus": 1, "status": status, "phone_number": phone, "receiver_public_name": received,
           "reference_doctype": CLAIM, "party_type": "Employee", "amount": 1000,
           "transaction_completed_datetime": AT + datetime.timedelta(minutes=minutes),
           "creation": AT + datetime.timedelta(minutes=minutes), **values}
    env.frappe.db.table(REQ)[name] = row
    return row


def b2b_request(env, name, shortcode, received, command="BusinessBuyGoods", minutes=0, **values):
    row = {"name": name, "docstatus": 1, "status": "Paid", "command_id": command, "receiver_shortcode": shortcode,
           "receiver_public_name": received, "trans_completed_time": AT + datetime.timedelta(minutes=minutes),
           "creation": AT + datetime.timedelta(minutes=minutes), "supplier": "ACME", **values}
    env.frappe.db.table(B2B)[name] = row
    return row


def known(env, key):
    return env.frappe.db.table(KNOWN).get(key)


def store(env, key, name, times=1, minutes=0, salary_only=0, bank=0, payout="B2C-1"):
    """A record of the list, as the system left it."""
    kind, number = kn().split_key(key)
    env.frappe.db.table(KNOWN)[key] = {
        "name": key, "destination_key": key, "destination_type": kind, "number": number, "mpesa_name": name,
        "received": f"{number} - {name}", "first_seen": AT, "last_seen": AT + datetime.timedelta(minutes=minutes),
        "times_paid": times, "last_payout_doctype": REQ, "last_payout": payout, "source": "B2C Result",
        "salary_only": salary_only, "bank": bank}


# ---------------------------------------------------------------- keys
def test_the_keys_are_the_suites_destination_keys(claims_env):
    k, cp = kn(), claims_env.cp
    assert k.key_of("Phone", "0711 000 333") == k.key_of(cp.PHONE, "+254711000333") == "B2C:254711000333"
    assert k.key_of(cp.TILL, "835811") == k.key_of("BusinessBuyGoods", "835811") == k.key_of("B2B Till", "835811") == (
        "TILL:835811")
    assert k.key_of(cp.PAYBILL, "400200") == k.key_of("BusinessPayBill", "400200") == "PAYBILL:400200"
    assert k.key_of(cp.BANK, "522522 ") == "PAYBILL:522522"
    # a claim payee line's and a payee account's own keys read as the list's (a paybill without its account)
    assert k.known_key(cp.destination_key(PHONE, phone="0711000333")) == "B2C:254711000333"
    assert k.known_key(cp.destination_key(TILL, "835811")) == "TILL:835811"
    assert k.known_key(cp.destination_key(cp.PAYBILL, "647647", "PARK-77")) == "PAYBILL:647647"
    assert k.known_key(cp.pa.destination_key(_dict(account_type=cp.BANK, shortcode="522522",
                                                   account_number="0123"))) == "PAYBILL:522522"
    assert k.split_key("PB:647647:PARK77") == ("Paybill", "647647")
    for wrong in (k.key_of("Phone", "12345"), k.key_of("Till", "83-58"), k.key_of("Fax", "835811"), k.known_key("X:1"),
                  k.known_key(None)):
        assert wrong is None


def test_a_name_m_pesa_gave_another_number_is_never_kept(claims_env):
    k = kn()
    seen = k.observation("Phone", "0711000333", "254711000333 - JANE WANJIKU")
    assert (seen.key, seen.mpesa_name, seen.received) == (
        "B2C:254711000333", "JANE WANJIKU", "254711000333 - JANE WANJIKU")
    assert k.observation("Phone", "0711000333", "JANE WANJIKU").mpesa_name == "JANE WANJIKU"  # no number in front
    assert k.observation("Phone", "0711000333", "2547****0333 - JANE").mpesa_name == "JANE"  # masked, digits agree
    assert k.observation("Phone", "0711000333", "254711000334 - JOHN DOE") is None
    assert k.observation("Phone", "0711000333", "2547****0334 - JOHN DOE") is None
    assert k.observation("Till", "835811", "835811 - SHELL KILIMANI").mpesa_name == "SHELL KILIMANI"
    assert k.observation("Till", "835811", "000000- Biller Companyty") is None  # Daraja's sample: another number
    assert k.observation("Phone", "0711000333", "254711000333 - ") is None
    assert k.observation("Phone", "0711", "X") is None


# ---------------------------------------------------------------- recording: B2C
def b2c_result(name="254711000333 - JANE WANJIKU NJERI", code=0):
    return {"ResultType": 0, "ResultCode": code, "ResultDesc": "The service request is processed successfully.",
            "OriginatorConversationID": "OCID-1", "ConversationID": "AG_1", "TransactionID": "UJT9PAIDOK",
            "ResultParameters": {"ResultParameter": [
                {"Key": "ReceiverPartyPublicName", "Value": name},
                {"Key": "TransactionCompletedDateTime", "Value": "29.10.2026 09:00:00"}]}}


def test_a_paid_b2c_result_keeps_the_name_once(connector):
    row = connector.frappe.db.table(REQ)["B2C-1"]
    row.update(status="Initiated", phone_number="254711000333", reference_doctype=CLAIM, party_type="Employee")
    connector.conn.handle_callback(b2c_result())
    record = known(connector, "B2C:254711000333")
    assert (record["mpesa_name"], record["times_paid"], record["source"], record["last_payout"],
            record["salary_only"]) == ("JANE WANJIKU NJERI", 1, "B2C Result", "B2C-1", 0)
    assert record["destination_type"] == "Phone" and record["number"] == "254711000333"
    connector.conn.handle_callback(b2c_result())  # Safaricom re-sends the Result: nothing counted twice
    assert known(connector, "B2C:254711000333")["times_paid"] == 1


def test_a_failed_b2c_result_or_another_numbers_name_keeps_nothing(connector):
    row = connector.frappe.db.table(REQ)["B2C-1"]
    row.update(status="Initiated", phone_number="254711000333")
    connector.conn.handle_callback(b2c_result(code=1))
    assert row["status"] == "Failed" and not connector.frappe.db.table(KNOWN)
    row.update(status="Initiated", result_code=None)
    connector.conn.handle_callback(b2c_result(name="254711000999 - SOMEONE ELSE"))
    assert row["status"] == "Paid" and not connector.frappe.db.table(KNOWN)


def test_each_payout_counts_once_and_the_latest_name_stays(claims_env):
    k = kn()
    assert k.record_b2c(request(claims_env, "B2C-1", "0711000333", "0711000333 - JANE OLD NAME", minutes=0))
    assert k.record_b2c(request(claims_env, "B2C-3", "254711000333", "254711000333 - JANE WANJIKU", minutes=60))
    assert k.record_b2c(request(claims_env, "B2C-2", "254711000333", "254711000333 - JANE MIDDLE", minutes=30))  # late
    assert not k.record_b2c(claims_env.frappe.db.table(REQ)["B2C-3"])  # the last Result again
    assert not k.record_b2c(request(claims_env, "B2C-4", "254711000333", "x", status="Failed"))
    record = known(claims_env, "B2C:254711000333")
    assert (record["mpesa_name"], record["times_paid"], record["last_payout"]) == ("JANE WANJIKU", 3, "B2C-3")
    assert (record["first_seen"], record["last_seen"]) == (AT, AT + datetime.timedelta(minutes=60))
    assert record["received"] == "254711000333 - JANE WANJIKU"


@pytest.mark.parametrize("values, salary", [
    ({"reference_doctype": "Salary Slip"}, True),
    ({"reference_doctype": None, "party_type": "Employee"}, True),  # an employee paid for no claim or advance
    ({"reference_doctype": "Employee Advance"}, False),
    ({"reference_doctype": CLAIM}, False),
    ({"reference_doctype": "M-Pesa Bulk Payout", "party_type": None}, False),  # a party-less row
])
def test_a_salary_payout_marks_a_number_salary_only(claims_env, values, salary):
    kn().record_b2c(request(claims_env, "B2C-1", "0711000555", "0711000555 - MARY AKINYI", **values))
    assert known(claims_env, "B2C:254711000555")["salary_only"] == (1 if salary else 0)


def test_a_salary_never_shows_through_a_record_others_see(claims_env):
    k = kn()
    k.record_b2c(request(claims_env, "B2C-1", "0711000555", "0711000555 - MARY SALARY",
                         reference_doctype="Salary Slip"))
    k.record_b2c(request(claims_env, "B2C-2", "0711000555", "0711000555 - MARY SALARY", minutes=5,
                         reference_doctype="Salary Slip"))
    mary = known(claims_env, "B2C:254711000555")
    assert (mary["salary_only"], mary["times_paid"]) == (1, 2)
    # the first payout that is no salary: the record starts again from it
    k.record_b2c(request(claims_env, "B2C-3", "0711000555", "0711000555 - MARY AKINYI", minutes=10))
    record = known(claims_env, "B2C:254711000555")
    assert (record["salary_only"], record["times_paid"], record["mpesa_name"], record["last_payout"]) == (
        0, 1, "MARY AKINYI", "B2C-3")
    assert record["first_seen"] == AT + datetime.timedelta(minutes=10)
    # a later salary is left out of it
    assert not k.record_b2c(request(claims_env, "B2C-4", "0711000555", "0711000555 - MARY SALARY NEW", minutes=20,
                                    reference_doctype="Salary Slip"))
    mary = known(claims_env, "B2C:254711000555")
    assert (mary["mpesa_name"], mary["times_paid"]) == ("MARY AKINYI", 1)


def test_keeping_a_name_never_stops_the_payout(claims_env, monkeypatch):
    frappe = claims_env.frappe

    def broken(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(frappe, "get_doc", broken)
    assert kn().record_b2c(request(claims_env, "B2C-1", "0711000333", "0711000333 - JANE")) is False
    assert ("rollback", "mpesa_known_name") in claims_env.log
    assert any(kw.get("title") == "M-Pesa known name not kept" for _a, kw in frappe.errors)

    def deadlock(*a, **kw):
        raise frappe.QueryDeadlockError("deadlock")

    monkeypatch.setattr(frappe, "get_doc", deadlock)
    with pytest.raises(frappe.QueryDeadlockError):  # the transaction is gone: the payout's job runs again
        kn().record_b2c(request(claims_env, "B2C-2", "0711000333", "0711000333 - JANE"))


def test_a_record_another_job_inserted_meanwhile_is_added_to(claims_env, monkeypatch):
    frappe, k = claims_env.frappe, kn()
    real = frappe.get_doc

    class Taken:
        flags = _dict()

        def insert(self, ignore_permissions=False):
            store(claims_env, "B2C:254711000333", "JANE WANJIKU", payout="B2C-0")  # the other job's, committed
            raise Exception("Duplicate entry 'B2C:254711000333' for key 'PRIMARY'")

    monkeypatch.setattr(frappe, "get_doc", lambda *a, **kw: Taken() if isinstance(a[0], dict) else real(*a, **kw))
    assert k.record_b2c(request(claims_env, "B2C-1", "0711000333", "0711000333 - JANE WANJIKU", minutes=5))
    assert known(claims_env, "B2C:254711000333")["times_paid"] == 2
    assert ("rollback", "mpesa_known_name_insert") in claims_env.log
    assert ("row", KNOWN, "B2C:254711000333", True) in claims_env.log  # read under the record's lock


def test_only_the_system_writes_the_list(claims_env):
    k = kn()
    with pytest.raises(claims_env.frappe.PermissionError, match="written by the system"):
        k.check_system_write(_dict(flags=_dict()))
    k.check_system_write(_dict(flags=_dict({k.FLAG: True})))
    claims_env.frappe.flags.in_patch = True
    k.check_system_write(_dict(flags=_dict()))


# ---------------------------------------------------------------- recording: B2B
def test_a_paid_b2b_result_keeps_its_paybills_name_once(flow_env):
    b2b_row(flow_env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    result = b2b_result()
    result["ResultParameters"]["ResultParameter"][-1]["Value"] = "888880 - TOTAL KENYA LTD"
    flow_env.api.b2b_result(Result=result, ref="MB2B-1")
    record = flow_env.frappe.db.table(KNOWN)["PAYBILL:888880"]
    assert (record["mpesa_name"], record["times_paid"], record["source"], record["last_payout_doctype"],
            record["last_payout"], record["bank"], record["salary_only"]) == (
        "TOTAL KENYA LTD", 1, "B2B Result", B2B, "MB2B-1", 0, 0)
    flow_env.api.b2b_result(Result=result, ref="MB2B-1")  # re-sent
    assert flow_env.frappe.db.table(KNOWN)["PAYBILL:888880"]["times_paid"] == 1


def test_a_b2b_result_naming_another_number_keeps_nothing(flow_env):
    row = b2b_row(flow_env, status="Initiated", conversation_id="AG_1", originator_conversation_id="OCID-1")
    flow_env.api.b2b_result(Result=b2b_result(), ref="MB2B-1")  # "000000- Biller Companyty" for paybill 888880
    assert row["status"] == "Paid" and not flow_env.frappe.db.table(KNOWN)


def test_a_bank_paybill_and_a_salary_paid_by_b2b_are_flagged(flow_env):
    k = kn()
    flow_env.frappe.db.table(BANK_PAYBILL)["KCB"] = {"name": "KCB", "paybill": "522522", "enabled": 1}
    k.record_b2b(b2b_request(flow_env, "MB2B-7", "522522", "522522 - KCB Paybill AC", command="BusinessPayBill",
                             employee="EMP-1", supplier=None))
    record = flow_env.frappe.db.table(KNOWN)["PAYBILL:522522"]
    assert (record["bank"], record["salary_only"], record["mpesa_name"]) == (1, 1, "KCB Paybill AC")
    k.record_b2b(b2b_request(flow_env, "MB2B-8", "835811", "835811 - SHELL", employee="EMP-1", supplier=None,
                             expense_claim="HR-EXP-1"))  # an employee paid for a claim: no salary
    assert flow_env.frappe.db.table(KNOWN)["TILL:835811"]["salary_only"] == 0


def test_a_tills_name_from_its_status_answer_is_kept(status_env):
    payout(status_env)  # a Paid till payment whose Result named no one
    doc = status_env.frappe.get_doc(B2B, "MB2B-26-10-000002")
    assert status_env.status.fill(doc, daraja.result_values(live()), "Transaction Status query Q-1")
    record = status_env.frappe.db.table(KNOWN)["TILL:835811"]
    assert (record["mpesa_name"], record["source"], record["times_paid"]) == (
        "ASTRAL INDUSTRIES LTD HQ", "B2B Result", 1)


# ---------------------------------------------------------------- the backfill
def test_the_backfill_builds_each_destination_from_history_and_is_idempotent(claims_env):
    env, k = claims_env, kn()
    env.frappe.db.columns |= {(B2B, "expense_claim")}
    request(env, "B2C-1", "0711000333", "0711000333 - JANE OLD", minutes=0)
    request(env, "B2C-3", "254711000333", "254711000333 - JANE WANJIKU", minutes=60)
    request(env, "B2C-2", "+254711000333", "254711000333 - JANE MIDDLE", minutes=30)
    request(env, "B2C-4", "254711000333", "254711000333 - FAILED", minutes=90, status="Failed")
    request(env, "B2C-5", "254711000333", "254711000999 - SOMEONE ELSE", minutes=95)  # another number
    request(env, "B2C-6", "254711000444", None, minutes=5)  # no name
    request(env, "B2C-7", "254711000555", "254711000555 - MARY SALARY", reference_doctype="Salary Slip")
    request(env, "B2C-8", "254711000666", "254711000666 - PAUL SALARY", reference_doctype="Salary Slip")
    request(env, "B2C-9", "254711000666", "254711000666 - PAUL OTIENO", minutes=40)
    b2b_request(env, "MB2B-1", "835811", "835811 - SHELL KILIMANI")
    b2b_request(env, "MB2B-2", "835811", "835811 - SHELL KILIMANI", minutes=10, receiver_public_name=None)
    b2b_request(env, "MB2B-3", "522522", "522522 - KCB Paybill AC", command="BusinessPayBill")
    counts = k.backfill()
    assert counts == {"inserted": 5, "updated": 0, "unchanged": 0}
    jane = known(env, "B2C:254711000333")
    assert (jane["mpesa_name"], jane["times_paid"], jane["first_seen"], jane["last_seen"], jane["last_payout"],
            jane["source"]) == ("JANE WANJIKU", 3, AT, AT + datetime.timedelta(minutes=60), "B2C-3", "History")
    assert (known(env, "B2C:254711000555")["salary_only"], known(env, "B2C:254711000666")["salary_only"]) == (1, 0)
    assert known(env, "B2C:254711000666")["mpesa_name"] == "PAUL OTIENO"
    assert known(env, "B2C:254711000444") is None
    assert (known(env, "TILL:835811")["times_paid"], known(env, "PAYBILL:522522")["bank"]) == (1, 1)
    before = json.dumps(env.frappe.db.table(KNOWN), default=str, sort_keys=True)
    assert k.backfill() == {"inserted": 0, "updated": 0, "unchanged": 5}  # again: the same values, nothing written
    assert json.dumps(env.frappe.db.table(KNOWN), default=str, sort_keys=True) == before
    # a payout recorded since, then the backfill again: the history holds it, the record agrees
    k.record_b2c(request(env, "B2C-10", "254711000333", "254711000333 - JANE WANJIKU N", minutes=120))
    assert k.backfill() == {"inserted": 0, "updated": 0, "unchanged": 5}
    assert known(env, "B2C:254711000333")["times_paid"] == 4


def test_the_backfill_is_quick_on_3000_payouts(claims_env):
    env, k = claims_env, kn()
    for i in range(3000):
        phone = f"25471100{i % 450:04d}"
        request(env, f"B2C-{i}", phone, f"{phone} - PERSON {i % 450}", minutes=i)
    started = time.perf_counter()
    assert k.backfill()["inserted"] == 450
    assert time.perf_counter() - started < 10
    records = env.frappe.db.table(KNOWN)
    assert sum(r["times_paid"] for r in records.values()) == 3000


def test_the_patch_runs_the_backfill_and_is_registered():
    source = (APP / "mpsa_suite" / "patches" / "known_mpesa_names.py").read_text(encoding="utf-8")
    assert "known_names.backfill()" in source and "ran_under_old_name(__name__)" in source
    lines = (APP / "patches.txt").read_text(encoding="utf-8").splitlines()
    post = lines[lines.index("[post_model_sync]"):]
    assert "mpsa_suite.mpsa_suite.patches.known_mpesa_names" in post  # after the DocType is made


# ---------------------------------------------------------------- lookup and verdict
def test_lookup_answers_by_the_lists_keys(claims_env):
    k = kn()
    store(claims_env, "B2C:254711000333", "JANE WANJIKU", times=3)
    store(claims_env, "PAYBILL:647647", "NAIROBI COUNTY")
    found = k.lookup(["0711000333", "B2C:254711000333", "PB:647647:PARK77", "TILL:999999", None])
    assert sorted(found) == ["B2C:254711000333", "PAYBILL:647647"]
    jane = found["B2C:254711000333"]
    assert (jane.name, jane.times_paid, jane.last_payout, jane.salary_only, jane.bank) == (
        "JANE WANJIKU", 3, "B2C-1", 0, 0)
    assert jane.first_seen == AT and jane.last_seen == AT
    assert k.lookup([]) == {} and k.lookup(["nonsense"]) == {}


@pytest.mark.parametrize("typed, info, verdict", [
    ("Jane Wanjiku", None, "new"),
    ("Jane Wanjiku", {"name": ""}, "new"),
    ("Jane Wanjiku", {"name": "JANE WANJIKU NJERI"}, "match"),
    ("jane njeri", {"name": "JANE WANJIKU NJERI"}, "match"),
    ("", {"name": "JANE WANJIKU NJERI"}, "match"),  # nothing typed: M-Pesa's name stands
    ("Peter Otieno", {"name": "JANE WANJIKU NJERI"}, "differs"),
    ("Mama Mboga Ltd", {"name": "KCB Paybill AC", "bank": 1}, "match"),  # a bank's name is no check of the holder
])
def test_the_verdict(claims_env, typed, info, verdict):
    assert kn().verdict(typed, info) == verdict


def test_the_badges_say_what_the_plan_says(claims_env):
    k = kn()
    store(claims_env, "B2C:254711000333", "JANE WANJIKU", times=3)
    store(claims_env, "B2C:254711000444", "PETER OTIENO", times=1)
    found = k.lookup(["B2C:254711000333", "B2C:254711000444"])
    assert k.describe("B2C:254711000333", found["B2C:254711000333"], "Jane Wanjiku")["badge"] == {
        "colour": "green", "text": "M-Pesa: JANE WANJIKU · paid 3 times, last 29 Oct"}
    assert k.describe("B2C:254711000444", found["B2C:254711000444"], None)["badge"]["text"] == (
        "M-Pesa: PETER OTIENO · paid 1 time, last 29 Oct")
    assert k.describe("B2C:254711000444", found["B2C:254711000444"], "Jane Wanjiku")["badge"] == {
        "colour": "orange", "text": "M-Pesa knows this number as PETER OTIENO"}
    assert k.describe("B2C:254711000555", None, "Mary")["badge"] == {
        "colour": "blue", "text": "New number: never paid; check it with the person"}
    claims_env.frappe.db.table(BANK_PAYBILL)["KCB"]["paybill"] = "522522"
    assert k.describe("PAYBILL:522522", None, "Mama Mboga")["badge"] == {
        "colour": "gray", "text": "M-Pesa shows the bank's name only"}
    assert k.describe("nothing", None)["badge"] is None and k.describe("nothing", None)["status"] == "invalid"
    last_year = _dict(found["B2C:254711000333"], last_seen=datetime.datetime(2025, 3, 2))
    assert k.describe("B2C:254711000333", last_year)["badge"]["text"].endswith("last 2 Mar 2025")


# ---------------------------------------------------------------- the desk endpoint, salary names hidden
def test_the_desk_hides_a_salary_name_from_who_may_not_see_salaries(claims_env):
    k, frappe = kn(), claims_env.frappe
    store(claims_env, "B2C:254711000555", "MARY AKINYI", times=12, salary_only=1)
    store(claims_env, "B2C:254711000333", "JANE WANJIKU", times=2)
    asked = json.dumps([{"id": "a", "type": "Phone", "number": "0711000555"}, {"id": "b", "key": "B2C:254711000333"},
                        {"id": "c", "type": "Till", "number": "835811"}])
    frappe.user_roles = {"Accounts User"}
    answer = k.for_destinations(asked)
    assert answer["a"]["status"] == "hidden" and answer["a"]["badge"] == {"colour": "gray", "text": "Paid before"}
    assert "name" not in answer["a"] and "times_paid" not in answer["a"] and answer["a"]["verdict"] is None
    assert (answer["b"]["name"], answer["b"]["status"]) == ("JANE WANJIKU", "known")
    assert answer["c"]["status"] == "new" and answer["c"]["verdict"] == "new"
    # round 4.5 review: and a phone paid to an Employee is hidden from who reads no Expense Claims either
    assert k.query_conditions("accounts@example.com") == (
        "COALESCE(`tabM-Pesa Known Name`.salary_only, 0) = 0 AND (COALESCE(`tabM-Pesa Known Name`.employee_payee, 0) "
        "= 0 OR COALESCE(`tabM-Pesa Known Name`.destination_type, '') != 'Phone')")
    assert k.has_permission(_dict(salary_only=1)) is False and k.has_permission(_dict(salary_only=0)) is True
    frappe.user_roles = {"HR User"}  # who may see salaries sees it
    assert k.for_destinations(asked)["a"]["name"] == "MARY AKINYI"
    assert k.query_conditions("hr@example.com") == "" and k.has_permission(_dict(salary_only=1)) is True


def test_the_desk_endpoint_is_for_who_may_read_the_list(claims_env):
    claims_env.denied.add((KNOWN, "read"))
    with pytest.raises(claims_env.frappe.PermissionError):
        kn().for_destinations([{"id": "a", "key": "B2C:254711000333"}])
    claims_env.denied.clear()
    with pytest.raises(claims_env.frappe.ValidationError, match="at most 500"):
        kn().for_destinations([f"B2C:2547110{i:05d}" for i in range(501)])


def test_the_desk_finds_the_name_to_compare_where_the_user_may_read_it(claims_env):
    k, db = kn(), claims_env.frappe.db
    store(claims_env, "B2C:254711000333", "JANE WANJIKU", times=2)
    store(claims_env, "TILL:835811", "SHELL KILIMANI", times=2)
    db.table(SUPPLIER_ACCOUNT)["MSA-7"] = {"name": "MSA-7", "account_type": TILL, "shortcode": "835811",
                                           "destination_key": "TILL:835811", "expected_name": "Shell Kilimani"}
    db.table("Supplier")["ACME"]["supplier_name"] = "Acme Hardware"
    claim(claims_env, [payee(name="LN-5", payee_type=PHONE, phone="0711000333", payee_name="Peter Otieno")])
    answer = k.for_destinations([
        {"id": "account", "account_doctype": SUPPLIER_ACCOUNT, "account": "MSA-7"},  # its destination too
        {"id": "payee", "type": "Phone", "number": "0711000333", "claim_payee": "LN-5"},
        {"id": "party", "route": "B2B Till", "destination": "835811", "party_type": "Supplier", "party": "ACME"},
        "B2C:254711000333"])
    assert (answer["account"]["key"], answer["account"]["typed_name"], answer["account"]["verdict"]) == (
        "TILL:835811", "Shell Kilimani", "match")
    assert db.table(LINE)["LN-5"]["typed_name"] == "Peter Otieno"  # M-Pesa's name replaced what was typed
    assert (answer["payee"]["typed_name"], answer["payee"]["verdict"]) == ("Peter Otieno", "differs")
    assert (answer["party"]["typed_name"], answer["party"]["verdict"]) == ("Acme Hardware", "differs")
    assert answer["B2C:254711000333"]["verdict"] == "match"
    claims_env.denied.update({(SUPPLIER_ACCOUNT, "read"), ("Supplier", "read")})  # what the user may not read
    answer = k.for_destinations([{"id": "a", "account_doctype": SUPPLIER_ACCOUNT, "account": "MSA-7"},
                                 {"id": "p", "type": "Till", "number": "835811", "party_type": "Supplier",
                                  "party": "ACME"}])
    assert answer["a"]["status"] == "invalid" and answer["p"]["typed_name"] is None


# ---------------------------------------------------------------- the Send via M-Pesa confirm screen
def names_of(env):
    """Two of the disbursement's phones M-Pesa paid before: one under its party's name, one not."""
    store(env, "B2C:254712345678", "JANE WANJIRU", times=4)
    store(env, "B2C:254712345679", "PETER OTIENO", times=1)


def three_rows(env, third="254712345670"):
    rows = disbursement(env)["references"]
    rows.append(_dict(rows[1], name="R3", partyb=third, allocated_amount=250.0))


def test_the_send_screen_shows_each_rows_name_and_the_counts(send_env):
    names_of(send_env)
    three_rows(send_env)
    r = prepared(send_env, B2C, "B2C-1")
    names = r["known_names"]
    assert names["header"] == "1 known and matching · 1 under another name · 1 new"
    assert names["counts"] == {"match": 1, "partial": 0, "differs": 1, "new": 1, "bank": 0, "hidden": 0}
    assert names["acknowledge"] == "I checked the 1 different name and 1 new number with the people paid."
    assert [(x["name"] if x["status"] == "known" else None, x["typed_name"], x["verdict"]) for x in names["rows"]] == [
        ("JANE WANJIRU", "Jane Wanjiru", "match"), ("PETER OTIENO", "Jane Wanjiru", "differs"),
        (None, "Jane Wanjiru", "new")]
    assert names["rows"][1]["badge"]["text"] == "M-Pesa knows this number as PETER OTIENO"
    assert all("_known" not in row for row in r["rows"])  # the rows themselves are as before
    assert r["token"]


def test_confirm_send_refuses_without_the_acknowledgement(send_env):
    names_of(send_env)
    three_rows(send_env)
    for refused in (None, "0", 0, ""):
        token = prepared(send_env, B2C, "B2C-1")["token"]
        send_env.clock[0] += 6
        with pytest.raises(send_env.frappe.ValidationError, match='Tick "I checked the 1 different name'):
            send_env.sc.confirm_send(B2C, "B2C-1", token, acknowledged=refused)
    assert send_env.submitted == []
    token = prepared(send_env, B2C, "B2C-1")["token"]
    send_env.clock[0] += 6
    assert send_env.sc.confirm_send(B2C, "B2C-1", token, acknowledged="1")["docstatus"] == 1


def test_a_send_whose_names_all_match_needs_no_acknowledgement(send_env):
    names_of(send_env)
    disbursement(send_env)["references"].pop()  # only Jane's matching phone left
    r = prepared(send_env, B2C, "B2C-1")
    assert r["known_names"]["header"] == "1 known and matching" and r["known_names"]["acknowledge"] is None
    send_env.clock[0] += 6
    assert send_env.sc.confirm_send(B2C, "B2C-1", r["token"])["docstatus"] == 1


def test_the_names_are_read_again_at_the_confirm(send_env):
    names_of(send_env)
    disbursement(send_env)["references"].pop()
    token = prepared(send_env, B2C, "B2C-1")["token"]
    send_env.clock[0] += 6
    # a Result since the summary: M-Pesa now names Jane's phone otherwise
    send_env.frappe.db.table(KNOWN)["B2C:254712345678"]["mpesa_name"] = "JOHN DOE"
    with pytest.raises(send_env.frappe.ValidationError, match="1 under another name"):
        send_env.sc.confirm_send(B2C, "B2C-1", token)


def test_a_b2b_send_compares_the_tills_name_with_the_approved_account(send_env):
    store(send_env, "PAYBILL:888880", "ACME HARDWARE LIMITED", times=6)
    b2b(send_env)
    names = prepared(send_env)["known_names"]
    assert (names["rows"][0]["typed_name"], names["rows"][0]["verdict"], names["acknowledge"]) == (
        "ACME HARDWARE", "match", None)
    assert names["rows"][0]["badge"]["text"] == "M-Pesa: ACME HARDWARE LIMITED · paid 6 times, last 29 Oct"
    send_env.frappe.db.table(KNOWN)["PAYBILL:888880"]["mpesa_name"] = "SOMEONE ELSE"
    names = prepared(send_env)["known_names"]
    assert names["rows"][0]["verdict"] == "differs" and names["header"] == "1 under another name"
    assert names["acknowledge"] == "I checked the 1 different name with the people paid."


def test_a_payee_accounts_approval_shows_the_name_and_asks_nothing(send_env):
    account(send_env)  # paybill 888880, expected ACME HARDWARE, never paid
    send_env.frappe.session.user, send_env.frappe.user_roles = "boss@example.com", {"M-Pesa Manager"}
    r = prepared(send_env, SUPPLIER_ACCOUNT, "MSA-9")
    assert r["known_names"]["header"] == "1 new" and r["known_names"]["acknowledge"] is None
    send_env.clock[0] += 6
    assert send_env.sc.confirm_send(SUPPLIER_ACCOUNT, "MSA-9", r["token"])["docstatus"] == 1


def test_a_reversal_has_no_names(send_env):
    reversal = _dict(doctype="Mpesa Reversal Request", transaction_id="X", amount=10)
    assert send_env.sc._rows_and_names(reversal)[1] is None


# ---------------------------------------------------------------- the claim payees' lookups, unchanged
def test_the_claim_lookups_read_the_list_with_their_rules(claims_env):
    cp, k = claims_env.cp, kn()
    store(claims_env, "B2C:254711000333", "JANE WANJIKU NJERI", minutes=30, payout="B2C-2")
    store(claims_env, "B2C:254711000555", "MARY SALARY", salary_only=1)
    assert not claims_env.frappe.db.table(REQ)  # no payout is read: the names come from the list
    found = cp.mpesa_names(["0711 000 333", "0711000555", "0799000000", None])
    assert {m: (v.name, v.payout) for m, v in found.items()} == {"254711000333": ("JANE WANJIKU NJERI", "B2C-2")}
    assert cp.name_from(found["254711000333"]) == "B2C-2, paid 2026-10-29"
    assert cp.mpesa_name("0711 000 333") == {"phone": "254711000333", "name": "JANE WANJIKU NJERI",
                                             "from": "B2C-2, paid 2026-10-29"}
    assert cp.mpesa_name("0711000555") == {}  # a salary's name is never read
    store(claims_env, "B2C:254722000111", "EMPLOYEE ONE")
    assert cp.mpesa_name("0722 000 111") == {}  # an employee's phone: refused as a payee, whose it is not said
    line = payee(payee_type=PHONE, phone="0711 000 333", payee_name="Jane W")
    cp.clean_line(line, set(), [])
    assert (line.payee_name, line.mpesa_name, line.typed_name) == ("JANE WANJIKU NJERI", "JANE WANJIKU NJERI", "Jane W")
    assert cp.name_from(_dict(payout=None, paid_on=AT)) == "M-Pesa, seen 2026-10-29"
    assert k.lookup(["B2C:254711000333"])  # the same record the claim read


# ---------------------------------------------------------------- the DocType, its permissions and hooks
def test_the_doctype_is_written_by_the_system_and_read_by_who_pays():
    doc = json.loads((APP / "mpsa_suite" / "doctype" / "m_pesa_known_name" / "m_pesa_known_name.json")
                     .read_text(encoding="utf-8"))
    fields = {f["fieldname"]: f for f in doc["fields"]}
    assert (doc["name"], doc["module"], doc["autoname"], doc.get("is_submittable", 0), doc["track_changes"]) == (
        KNOWN, "Mpsa Suite", "field:destination_key", 0, 0)
    for name in ("destination_key", "destination_type", "number", "mpesa_name", "received", "first_seen", "last_seen",
                 "times_paid", "last_payout_doctype", "last_payout", "source", "salary_only", "bank"):
        assert fields[name].get("read_only") == 1, name
    assert fields["destination_type"]["options"].split("\n") == ["Phone", "Till", "Paybill"]
    assert fields["source"]["options"].split("\n") == ["B2C Result", "B2B Result", "SMS", "History"]
    assert fields["last_payout"]["fieldtype"] == "Dynamic Link" and fields["last_payout"]["options"] == (
        "last_payout_doctype")
    assert not any(p.get(r) for p in doc["permissions"] for r in ("write", "create", "delete", "submit"))
    roles = {p["role"] for p in doc["permissions"]}
    assert {"M-Pesa Manager", "M-Pesa Payer", "Accounts User", "HR User", "Expense Approver"} <= roles
    assert "M-Pesa Viewer" not in roles and "System Manager" not in roles
    hooks = (APP / "hooks.py").read_text(encoding="utf-8")
    assert ('permission_query_conditions["M-Pesa Known Name"] = '
            '"mpsa_suite.money_out.known_names.query_conditions"') in hooks
    assert 'has_permission["M-Pesa Known Name"] = "mpsa_suite.money_out.known_names.has_permission"' in hooks
    controller = (APP / "mpsa_suite" / "doctype" / "m_pesa_known_name" / "m_pesa_known_name.py").read_text(
        encoding="utf-8")
    assert "known_names.check_system_write(self)" in controller


def test_each_result_path_keeps_the_name():
    connector = (APP / "connectors" / "mpesa" / "b2c_connector.py").read_text(encoding="utf-8")
    assert "known_names.record_b2c(req)" in connector
    assert "known_names.record_b2b(doc, values.get(\"ReceiverPartyPublicName\"))" in (
        APP / "money_out" / "b2b.py").read_text(encoding="utf-8")
    status = (APP / "money_out" / "b2b_status.py").read_text(encoding="utf-8")
    assert status.count("known_names.record_b2b(") == 2  # an answer filling the name, and one settling the payout


# ---------------------------------------------------------------- the desk scripts (node)
PRELUDE = r"""
const fs = require("fs");
const setup = JSON.parse(process.env.MPSA_SETUP || "{}");
global.__ = (s, args) => (args ? String(s).replace(/\{(\d+)\}/g, (_, i) => String(args[i])) : s);
global.flt = (v) => parseFloat(v) || 0;
global.format_currency = (v) => String(v);
global.window = global;
global.registered = {};
global.frappe = {
	boot: { mpsa: setup.mpsa || {} },
	provide: (ns) => { let o = global; for (const p of ns.split(".")) { o[p] = o[p] || {}; o = o[p]; } },
	ui: { form: { on: (dt, handlers) => { registered[dt] = Object.keys(handlers); } } },
	model: { can_read: (dt) => (setup.readable || []).includes(dt) },
	utils: { escape_html: (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;") },
};
global.load = (path) => (0, eval)(fs.readFileSync(path, "utf8"));
"""
KNOWN_JS = APP / "public" / "js" / "mpsa_known_names.js"
SEND_JS = APP / "public" / "js" / "mpsa_send.js"
node = pytest.mark.skipif(not NODE, reason="node is not installed")


def run_node(tmp_path, paths, scenario, **setup):
    script = PRELUDE + "".join(f"load({json.dumps(str(p))});\n" for p in paths)
    script += "console.log(JSON.stringify((() => {\n" + scenario + "\n})()));\n"
    harness = tmp_path / "harness.js"
    harness.write_text(script, encoding="utf-8")
    done = subprocess.run([NODE, str(harness)], capture_output=True, text=True, encoding="utf-8", timeout=60,
                          env=dict(os.environ, MPSA_SETUP=json.dumps(setup)))
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@node
def test_the_badges_render_each_colour_and_escape_names(tmp_path):
    out = run_node(tmp_path, [KNOWN_JS], """
const b = (colour, text, extra) => mpsa_known_names.badge_html(Object.assign({ badge: { colour, text } }, extra || {}));
return {
	green: b("green", "M-Pesa: JANE · paid 3 times, last 28 Oct", { verdict: "match" }),
	orange: b("orange", "M-Pesa knows this number as <b>JOHN</b>", { verdict: "differs", typed_name: "Jane" }),
	blue: b("blue", "New number: never paid; check it with the person", { status: "new", verdict: "new" }),
	none: mpsa_known_names.badge_html({}),
	counts: mpsa_known_names.counts([{ badge: {}, verdict: "match" }, { badge: {}, verdict: "match" },
		{ badge: {}, verdict: "differs" }, { badge: {}, status: "new", verdict: "new" }, { badge: {}, bank: 1 }, null]),
	forms: registered,
	allowed: mpsa_known_names.allowed(),
};
""", readable=["M-Pesa Known Name"])
    assert "var(--green-600)" in out["green"] and "M-Pesa: JANE · paid 3 times, last 28 Oct" in out["green"]
    assert "var(--orange-600)" in out["orange"] and "&lt;b&gt;JOHN&lt;/b&gt;" in out["orange"]
    assert "<b>" not in out["orange"]
    assert "(given: Jane)" in out["orange"]
    assert "var(--blue-600)" in out["blue"] and out["none"] == ""
    assert out["counts"] == ("2 known and matching · 1 under another name · 1 new · 1 bank (M-Pesa shows the bank's "
                             "name only)")
    assert out["allowed"] is True
    forms = out["forms"]
    for doctype in ("Supplier M-Pesa Account", "Employee M-Pesa Account", "Mpesa B2B Payment Request",
                    "M-Pesa Payment Schedule", "Expense Claim", "B2C Payment Disbursement"):
        assert "refresh" in forms[doctype], doctype
    assert "phone" in forms["Supplier M-Pesa Account"] and "receiver_shortcode" in forms["Mpesa B2B Payment Request"]
    assert {"phone", "mpesa_payees_remove"} <= set(forms["M-Pesa Claim Payee"])
    assert {"partyb", "references_remove"} <= set(forms["B2C Payment Disbursement Reference"])
    assert {"destination", "lines_remove"} <= set(forms["M-Pesa Payment Schedule Line"])


@node
def test_nothing_is_asked_of_the_server_by_who_may_not_read_the_list(tmp_path):
    out = run_node(tmp_path, [KNOWN_JS], """
let asked = 0;
frappe.xcall = () => { asked += 1; return Promise.resolve({}); };
mpsa_known_names.fetch([{ id: "a", key: "B2C:254711000333" }]);
return { allowed: mpsa_known_names.allowed(), asked, wanted: mpsa_known_names.wanted({ id: "x" }) };
""", readable=[])
    assert out == {"allowed": False, "asked": 0, "wanted": False}


SEND_SCENARIO = """
const names = {
	header: "1 known and matching · 1 under another name",
	acknowledge: "I checked the 1 different name with the people paid.",
	rows: [
		{ status: "known", verdict: "match",
			badge: { colour: "green", text: "M-Pesa: JANE · paid 2 times, last 28 Oct" } },
		{ status: "known", verdict: "differs", typed_name: "Jane Wanjiru",
			badge: { colour: "orange", text: "M-Pesa knows this number as PETER OTIENO" } },
	],
};
const rows = [{ receiver: "Jane", destination: "M-Pesa phone 254712345678", amount: 1500 },
	{ receiver: "Jane", destination: "M-Pesa phone 254712345679", amount: 500 }];
const r = { doctype: "B2C Payment Disbursement", name: "B2C-1", rows, total: 2000, problems: [], warnings: [],
	known_names: names, token: "t1", delay: 1 };
const html = mpsa_send.summary_html(r);
const plain = mpsa_send.summary_html(Object.assign({}, r, { known_names: null }));
// the dialog: Confirm works only after the countdown and the tick
const calls = [];
let ticked = false, tick = null, primary = null, disabled = null;
global.setInterval = (fn) => { tick = fn; return 1; };
global.clearInterval = () => {};
global.setTimeout = () => 1;
const button = { prop: (k, v) => { if (k === "disabled") disabled = v; return button; }, text: () => button };
frappe.ui.Dialog = function (opts) {
	this.fields = opts.fields;
	this.fields_dict = { summary: { $wrapper: { html: () => null } } };
	this.get_value = (f) => (f === "acknowledged" ? ticked : null);
	this.set_primary_action = (label, fn) => { primary = fn; };
	this.get_primary_btn = () => button;
	this.show = () => null;
	this.hide = () => null;
	this.$wrapper = { remove: () => null };
};
frappe.xcall = (method, args) => { calls.push(args); return Promise.resolve({}); };
frappe.show_alert = () => null;
let fields = [];
const Dialog = frappe.ui.Dialog;
frappe.ui.Dialog = function (opts) { fields = opts.fields; Dialog.call(this, opts); };
mpsa_send.confirm({ reload_doc: () => null }, r);
primary();                       // too early
tick();                          // the countdown is over, the box not ticked
const after_countdown = disabled;
primary();
const before = calls.length;
ticked = true;
fields.find((f) => f.fieldname === "acknowledged").onchange();
const after_tick = disabled;
primary();
return { html, plain, after_countdown, after_tick, before, sent: calls, ack: mpsa_send.acknowledgement(r),
	box: fields.filter((f) => f.fieldtype === "Check").map((f) => f.label) };
"""


@node
def test_the_send_dialog_shows_the_names_and_confirms_only_once_ticked(tmp_path):
    out = run_node(tmp_path, [SEND_JS], SEND_SCENARIO)
    html = out["html"]
    assert "M-Pesa names" in html and "1 known and matching · 1 under another name" in html
    assert "M-Pesa: JANE · paid 2 times, last 28 Oct" in html and "var(--green-600)" in html
    assert "M-Pesa knows this number as PETER OTIENO" in html and "Name given: Jane Wanjiru" in html
    assert "M-Pesa names" not in out["plain"] and "M-Pesa knows" not in out["plain"]
    assert out["ack"] == "I checked the 1 different name with the people paid."
    assert out["box"] == [out["ack"]]  # the box the sender ticks says what they checked
    assert out["after_countdown"] is True and out["before"] == 0  # not ticked: Confirm off, nothing sent
    assert out["after_tick"] is False
    assert out["sent"] == [{"doctype": "B2C Payment Disbursement", "name": "B2C-1", "token": "t1", "acknowledged": 1}]


@node
def test_the_send_dialog_asks_no_tick_when_every_name_matches(tmp_path):
    scenario = SEND_SCENARIO.replace('acknowledge: "I checked the 1 different name with the people paid.",',
                                     "acknowledge: null,").replace(
        'fields.find((f) => f.fieldname === "acknowledged").onchange();', "")
    out = run_node(tmp_path, [SEND_JS], scenario)
    assert out["box"] == [] and out["after_countdown"] is False and out["before"] == 1
    assert out["sent"][0]["acknowledged"] == 0
