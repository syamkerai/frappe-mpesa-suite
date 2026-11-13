"""Round 4.5 review of the tills (money_out.petty_cash, money_out.pay_via_staff): each confirmed finding, a test.

On the in-memory sites of test_r45_petty_cash (the phone, its top-ups through the real B2C controllers and the
party-less branch) and test_r45_pay_via_staff (Employee Advances, the real B2C and B2B guards).

1. A Pay via Staff advance not settled is taken off what its order or invoice owes for every payout: owed.left_to_pay
   (B2B, the schedule, a petty cash spend, Pay via Staff itself, once) and B2C's own guards (the send, a retry),
   through owed.planned_lines; an invoice counts its own advances and its orders'; explain names them.
2. A Failed top-up whose payout is sent again (Retry Failed, an amended batch) is on its way, by its request's
   state: one top-up at a time, and the ceiling at the send of another counts it.
3. The Name on M-Pesa is needed to approve a phone; a phone without one is never topped up nor its name adopted.
4. Fresh reads under the locks: a spend, a top-up and an advance commit their reads away first and read the
   receipt, the book balance, the top-ups and the advances with locking reads; a settlement locks each advance.
5. The holder's name and User come from the holder's Employee record; a change of holder needs an M-Pesa or
   Accounts Manager who is neither holder, and is commented.
6. Only the name the SMS gave is kept with the known M-Pesa names; a Pochi la Biashara is no paybill.
7. An SMS without a balance is recorded as such (SMS Gave a Balance) and is never the "last SMS".
8. The SMS's transaction cost wins over a typed charge; a typed charge alone is capped.
9. No staff advance for a supplier on hold.
10. The Payment Entry and Purchase Order / Invoice fields are made without HRMS too.
"""

import ast
import datetime
import importlib
import json
import sys
import types
from pathlib import Path

import b2b_fixture
import fake_frappe
import pytest
from test_employee_documents import env as ed_env  # noqa: F401 (a fixture the ones below extend)
from test_payroll_schedule import env as payroll_env  # noqa: F401
from test_r45_pay_via_staff import EA, staff_advance
from test_r45_pay_via_staff import env as staff_env  # noqa: F401
from test_r45_petty_cash import (  # noqa: F401 (reader: a fixture)
    ACCOUNTS, CHARGES, DISB, GLE, HOLDER, MANAGER, NUMBER, PAYBILL_SMS, PAYER, PE, PHONE, REQ, ROW, SEND_SMS,
    TILL_SMS, TOP_UP, Saved, as_user, draft, paid_top_up, raises, reader, result, send, spend_entry, top_up,
    top_up_row,
)
from test_r45_petty_cash import env as petty_env  # noqa: F401

APP = Path(__file__).resolve().parents[2] / "mpsa_suite"
PO, PI = "Purchase Order", "Purchase Invoice"
LOCK = "b2c-pay:M-Pesa Petty Cash Phone:PCP-00001"
_dict = fake_frappe._dict

POCHI_SMS = ("UKD1ABC2DH Confirmed. Ksh300.00 sent to MAMA MBOGA on 13/11/26 at 1:10 PM. New M-PESA balance is "
             "Ksh9,700.00. Transaction cost, Ksh0.00. Amount you can transact within the day is 499,700.00.")
NO_BALANCE_SMS = "UKD1ABC2DJ Confirmed. Ksh1,200.00 paid to SHELL KILIMANI. on 13/11/26 at 2:00 PM."
NO_COST_SMS = ("UKD1ABC2DK Confirmed. Ksh1,500.00 paid to SHELL KILIMANI. on 13/11/26 at 10:15 AM. New M-PESA balance "
               "is Ksh8,500.00.")


def _staff_columns(env, monkeypatch):
    """The Employee Advance's Pay via Staff columns on the petty cash site (pay_via_staff reads them with SQL)."""
    pvs = importlib.import_module("mpsa_suite.money_out.pay_via_staff")
    monkeypatch.setitem(b2b_fixture.COLUMNS, EA, pvs.ADVANCE_FIELDS)
    env.frappe.db.columns = env.frappe.db.columns | {(EA, "mpesa_supplier")}


@pytest.fixture
def spends(petty_env, monkeypatch):  # noqa: F811
    env = petty_env
    env.pi("PINV-1", outstanding_amount=10000)
    env.po("PO-1", rounded_total=4000, grand_total=4000)
    env.frappe.db.table("Supplier")["ACME"].update(supplier_name="Shell Kilimani Ltd")
    env.gl(debit=10000)
    _staff_columns(env, monkeypatch)
    as_user(env, HOLDER, "Purchase User")
    return env


def _fresh(monkeypatch, env, module):
    """owed.fresh_snapshot, watched: it lands in the read log."""
    monkeypatch.setattr(module.owed, "fresh_snapshot", lambda: env.log.append(("fresh",)) or True)


def _since(env, mark):
    return env.log[mark:]


# ================================================================ 1. staff advances count for every payout
def test_1_a_staff_advance_is_off_what_the_order_owes_and_named(staff_env):  # noqa: F811
    env = staff_env
    staff_advance(env, "HR-EAD-S1", amount=4000, paid=4000)  # for PO-1 (6,000)
    found = env.owed.left_to_pay(PO, "PO-1")
    assert (found.owed, found.left) == (6000, 2000)
    assert env.owed.explain("PO-1", found) == (
        "PO-1 owes 6,000, less Pay via Staff advance HR-EAD-S1 (Otieno Ouma) not settled 4,000: 2,000 is left to pay.")
    assert env.owed.summary(found)["order_advance"] == 4000  # the request form's headline adds up
    # Pay via Staff itself counts it once, not twice
    assert env.pvs._left(PO, "PO-1")[0] == 2000
    assert env.pvs.document_state(PO, "PO-1")["left"] == 2000
    # what it still stands for: less what was claimed; nothing once cancelled
    env.frappe.db.table(EA)["HR-EAD-S1"].update(claimed_amount=1000)
    assert env.owed.left_to_pay(PO, "PO-1").left == 3000
    env.frappe.db.table(EA)["HR-EAD-S1"].update(docstatus=2)
    assert env.owed.left_to_pay(PO, "PO-1").left == 6000


def test_1_an_invoice_counts_its_own_advances_and_its_orders_once(staff_env):  # noqa: F811
    env = staff_env
    staff_advance(env, "HR-EAD-S1", amount=4000, paid=4000, order=None, invoice="PINV-1")
    staff_advance(env, "HR-EAD-S2", amount=3000, paid=3000)  # PO-1's, the invoice's order
    staff_advance(env, "HR-EAD-S3", amount=1000, paid=1000, invoice="PINV-9")  # PO-1's, made for another invoice
    env.frappe.db.table("Purchase Invoice Item")["PII-1"] = {"name": "PII-1", "parent": "PINV-1", "parenttype": PI,
                                                             "purchase_order": "PO-1"}
    found = env.owed.left_to_pay(PI, "PINV-1")
    assert ([p.name for p in found.parts], found.left) == (["HR-EAD-S1", "HR-EAD-S2"], 3000)
    # settled: the journal lowers the invoice's outstanding, and the advances stop counting
    out = env.pvs.settle_staff_advances("PINV-1")
    assert out["settled"] == [("HR-EAD-S1", 4000), ("HR-EAD-S2", 3000)]
    env.frappe.db.table(PI)["PINV-1"]["outstanding_amount"] = 3000  # as ERPNext writes it back
    found = env.owed.left_to_pay(PI, "PINV-1")
    assert (found.parts, found.left) == ([], 3000)


def test_1_b2b_and_the_schedule_refuse_what_a_staff_advance_takes(staff_env):  # noqa: F811
    env = staff_env
    staff_advance(env, "HR-EAD-S1", amount=4000, paid=0)  # not paid yet: it will be
    mark = len(env.log)
    request = _dict(name="MB2B-NEW", amount=3000, supplier="ACME", company="Astral")
    why = ("PO-1 owes 6,000, less Pay via Staff advance HR-EAD-S1 (Otieno Ouma) not settled 4,000: 2,000 is left to "
           "pay.")
    with raises(env, "KES 3,000 is more than is left to pay. " + why.replace("(", r"\(").replace(")", r"\)")):
        env.b2b._check_document(request, PO, "PO-1", for_update=True)
    assert ("sql", (EA,), True) in _since(env, mark)  # a locking read of the advances
    env.b2b._check_document(_dict(request, amount=2000), PO, "PO-1", for_update=True)
    line = _dict(name="L-1", reference_doctype=PO, reference_name="PO-1", amount=3000, party="ACME",
                 party_type="Supplier")
    assert env.schedule._document_problem(line, _dict(company="Astral"), _dict(), True) == (
        "KES 3,000 is more than is left to pay. " + why)


def test_1_b2cs_own_guards_take_a_staff_advance_off(staff_env):  # noqa: F811
    env = staff_env
    staff_advance(env, "HR-EAD-S1", amount=4000, paid=4000)
    disbursement = object.__new__(env.disb.B2CPaymentDisbursement)
    row = _dict(reference_doctype=PO, reference_name="PO-1", allocated_amount=3000, b2c_disbursement_request=None)
    disbursement.__dict__.update(name="MPESA-B2C-9", references=[row])
    with raises(env, r"PO-1 owes 6000.0, less Pay via Staff advance HR-EAD-S1 \(Otieno Ouma\) not settled 4,000; "
                     "this disbursement allocates 3000.0"):
        disbursement.validate_no_double_payout()
    row.allocated_amount = 2000
    disbursement.validate_no_double_payout()
    # a failed payout of the order retried
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request")
    retry = object.__new__(module.B2CDisbursementRequest)
    retry.__dict__.update(name="B2C-9", reference_doctype=PO, reference_name="PO-1", amount=3000, docstatus=1,
                          status="Failed", b2c_payment_disbursement="MPESA-B2C-9", flags=_dict())
    with raises(env, r"less Pay via Staff advance HR-EAD-S1 \(Otieno Ouma\) not settled 4,000; retrying would pay "
                     "3000"):
        retry.validate_still_owed()


def test_1_a_petty_cash_spend_counts_a_staff_advance(spends):
    staff_advance(spends, "HR-EAD-S1", amount=3000, paid=3000)  # for PO-1 (4,000)
    with raises(spends, r"KES 1,500 is more than PO-1 still owes. PO-1 owes 4,000, less Pay via Staff advance "
                        r"HR-EAD-S1 \(Otieno Ouma\) not settled 3,000: 1,000 is left to pay."):
        spends.pc.record_spend(PO, "PO-1", sms=TILL_SMS)
    assert not spends.frappe.db.table(PE)
    spends.frappe.db.table(EA)["HR-EAD-S1"].update(return_amount=3000)  # returned: it no longer counts
    assert spends.pc.record_spend(PO, "PO-1", sms=TILL_SMS)["amount"] == 1500


# ================================================================ 2. a failed top-up sent again is on its way
def _retry(env, request):
    """Retry Failed on the request: its checks (validate_still_owed), then what retry_failed_payment writes before
    make_b2c_payment_request sends it again."""
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request")
    doc = object.__new__(module.B2CDisbursementRequest)
    doc.__dict__.update(env.frappe.db.table(REQ)[request], doctype=REQ, flags=_dict())
    as_user(env, PAYER, "M-Pesa Payer")
    doc.validate_still_owed()
    env.frappe.db.table(REQ)[request].update(status="Not Initiated", transaction_id=None)


def test_2_a_failed_top_up_retried_is_on_its_way_and_holds_the_next(petty_env):  # noqa: F811
    env = petty_env
    env.gl(debit=5000)  # book 5,000; ceiling 20,000; limit 10,000
    first = top_up(env, 10000)
    send(env, first["name"])
    request = result(env, first["name"], status="Failed")
    assert env.pc.in_flight("PCP-00001") == 0  # failed and not sent again: nothing on its way
    assert top_up_row(env, first["top_up"])["status"] == "Failed"
    _retry(env, request)
    assert env.pc.in_flight("PCP-00001") == 10000
    assert top_up_row(env, first["top_up"])["status"] == "Sending"
    with raises(env, r"Top-up #1 of PCP-00001 is Sending \(MPESA-B2C-1\): wait for M-Pesa's answer before another"):
        top_up(env, 10000)
    # the retry fails too: Failed again, nothing on its way
    env.frappe.db.table(REQ)[request].update(status="Failed")
    env.response.update_b2c_reference_status(request, enqueue_next=True)
    assert (top_up_row(env, first["top_up"])["status"], env.pc.in_flight("PCP-00001")) == ("Failed", 0)


def test_2_the_send_of_another_top_up_counts_a_failed_one_sent_again(petty_env):  # noqa: F811
    env = petty_env
    env.gl(debit=5000)
    first = top_up(env, 10000)
    send(env, first["name"])
    request = result(env, first["name"], status="Failed")
    second = top_up(env, 10000)  # the first failed: another may be prepared
    _retry(env, request)  # and then the first is sent again
    env.locks.clear()  # the transactions so far committed
    mark = len(env.log)
    with raises(env, r"Row #1: PCP-00001 would hold KES 25,000, more than its float ceiling of KES 20,000 \(book "
                     r"balance KES 5,000 and KES 10,000 on its way\)"):
        send(env, second["name"])
    log = _since(env, mark)
    lock = log.index(("lock", LOCK))  # the send's check, under the phone's lock
    assert {("sql", (TOP_UP,), True), ("sql", (GLE,), True), ("sql", (ROW, DISB, REQ), True)} <= set(log[lock:])


def test_2_an_amended_batch_sent_counts_and_its_draft_does_not(petty_env):  # noqa: F811
    env = petty_env
    env.gl(debit=5000)
    first = top_up(env, 10000)
    send(env, first["name"])
    request = result(env, first["name"], status="Failed")
    db = env.frappe.db
    db.table(DISB)[first["name"]]["docstatus"] = 2  # cancelled, and amended
    db.table(DISB)["MPESA-B2C-1-1"] = {"name": "MPESA-B2C-1-1", "docstatus": 0, "company": "Astral"}
    db.table(ROW)["R-AMEND"] = {"name": "R-AMEND", "parent": "MPESA-B2C-1-1", "parenttype": DISB, "idx": 1,
                                "reference_doctype": PHONE, "reference_name": "PCP-00001",
                                "reference_row": first["top_up"], "partyb": NUMBER, "allocated_amount": 10000,
                                "payment_status": "Failed", "b2c_disbursement_request": request}  # the copy
    assert (env.pc.in_flight("PCP-00001"), top_up_row(env, first["top_up"])["status"]) == (0, "Failed")
    db.table(DISB)["MPESA-B2C-1-1"]["docstatus"] = 1  # sent: its row's payout is made anew
    db.table(ROW)["R-AMEND"].update(payment_status="Initiated", b2c_disbursement_request=None)
    assert (env.pc.in_flight("PCP-00001"), top_up_row(env, first["top_up"])["status"]) == (10000, "Sending")


# ================================================================ 3. the Name on M-Pesa
def test_3_the_name_on_mpesa_is_needed_to_approve(petty_env):  # noqa: F811
    env = petty_env
    doc = draft(env, expected_name=None)
    doc.flags = _dict({env.pc.APPROVE_FLAG: True})
    as_user(env, MANAGER, "M-Pesa Manager")
    with raises(env, "Enter the Name on M-Pesa: the name M-Pesa shows for this number, from a test or the SIM's "
                     "registration"):
        env.pc.check_approval(doc)
    doc.expected_name = "  ACHIENG MARY "
    env.pc.check_approval(doc)
    assert (doc.expected_name, doc.approved_by) == ("ACHIENG MARY", MANAGER)
    data = json.loads((APP / "mpsa_suite" / "doctype" / "m_pesa_petty_cash_phone" / "m_pesa_petty_cash_phone.json")
                      .read_text(encoding="utf-8"))
    field = next(f for f in data["fields"] if f["fieldname"] == "expected_name")
    assert field["reqd"] == 1 and "from a test or the SIM's registration" in field["description"]


def test_3_a_phone_with_no_name_is_never_topped_up_nor_its_first_name_adopted(petty_env):  # noqa: F811
    env = petty_env
    out = top_up(env, 3000)  # made while it had one
    env.frappe.db.table(PHONE)["PCP-00001"].update(expected_name=None)
    with raises(env, "PCP-00001 has no Name on M-Pesa"):
        top_up(env, 3000)
    assert env.pc.phone_state("PCP-00001")["can_top_up"] is False
    with raises(env, "Row #1: PCP-00001 has no Name on M-Pesa"):
        send(env, out["name"])
    # paid all the same (sent before): the name M-Pesa gave is not adopted, the phone is held
    env.pc.check_name("PCP-00001", _dict(name="B2C-X", receiver_public_name=f"{NUMBER} - SOMEONE ELSE ENTIRELY"))
    stored = env.frappe.db.table(PHONE)["PCP-00001"]
    assert (stored["name_mismatch"], stored["mpesa_name"]) == (1, None)
    assert "and the phone has no Name on M-Pesa to check it against" in env.alerts[-1][1]
    assert "no Name on M-Pesa to check it against" in env.pc.usable_problem(_dict(stored))


# ================================================================ 4. fresh reads under the locks
def test_4_a_spend_commits_its_reads_away_and_reads_the_receipt_and_balance_locking(spends, monkeypatch):
    _fresh(monkeypatch, spends, spends.pc)
    spends.locks.clear()
    mark = len(spends.log)
    spends.pc.record_spend(PI, "PINV-1", sms=TILL_SMS)
    log = _since(spends, mark)
    receipt = log.index(("lock", "mpesa-receipt:UKD1ABC2DE"))
    assert log.index(("fresh",)) < log.index(("lock", LOCK)) < receipt
    after = log[receipt:]
    # the receipt's Payment Entry (indexed) and the book balance: locking reads; a Journal Entry's Cheque No is read
    # after the fresh snapshot (no index to lock on)
    assert {("sql", (PE,), True), ("sql", ("Journal Entry",), False), ("sql", (GLE,), True)} <= set(after)


def test_4_a_top_up_commits_its_reads_away_and_reads_the_top_ups_locking(petty_env, monkeypatch):  # noqa: F811
    env = petty_env
    _fresh(monkeypatch, env, env.pc)
    env.locks.clear()
    mark = len(env.log)
    top_up(env, 2000)
    log = _since(env, mark)
    lock = log.index(("lock", LOCK))
    assert log.index(("fresh",)) < lock
    assert {("row", PHONE, "PCP-00001", True), ("sql", (TOP_UP,), True), ("sql", (GLE,), True)} <= set(log[lock:])


def test_4_an_advance_commits_its_reads_away_and_reads_the_advances_locking(staff_env, monkeypatch):  # noqa: F811
    env = staff_env
    _fresh(monkeypatch, env, env.pvs)
    staff_advance(env, "HR-EAD-S1", amount=4000, paid=4000)
    env.locks.clear()
    mark = len(env.log)
    env.pvs.make_advance(PO, "PO-1", "EMP-2", 2000)
    log = _since(env, mark)
    lock = log.index(("lock", "b2c-pay:Purchase Order:PO-1"))
    assert log.index(("fresh",)) < lock
    assert {("sql", (EA,), True), ("sql", ("Journal Entry Account", "Journal Entry"), True)} <= set(log[lock:])
    with raises(env, r"KES 1,000 is more than PO-1 has left for a staff advance \(KES 0\)"):
        env.pvs.make_advance(PO, "PO-1", "EMP-2", 1000)  # 6,000 less the two advances


def test_4_a_settlement_locks_each_advance_before_its_locking_reads(staff_env):  # noqa: F811
    env = staff_env
    staff_advance(env, "HR-EAD-S2", amount=3000, paid=3000)  # PO-1's
    env.pi("PINV-2", outstanding_amount=10000)
    for name, invoice in (("PII-1", "PINV-1"), ("PII-2", "PINV-2")):  # two invoices of one order
        env.frappe.db.table("Purchase Invoice Item")[name] = {"name": name, "parent": invoice, "parenttype": PI,
                                                              "purchase_order": "PO-1"}
    mark = len(env.log)
    assert env.pvs.settle_staff_advances("PINV-1")["settled"] == [("HR-EAD-S2", 3000)]
    log = _since(env, mark)
    lock = log.index(("lock", "pay-via-staff-settle:HR-EAD-S2"))
    reads = [i for i, entry in enumerate(log) if entry == ("sql", (EA,), True)]
    settled = [i for i, entry in enumerate(log) if entry == ("sql", ("Journal Entry Account", "Journal Entry"), True)]
    row = log.index(("row", PI, "PINV-1", True))
    assert lock < reads[0] < row and lock < settled[0] < row  # the invoice's row last (R4)
    # the order's other invoice reads what the first committed: nothing of that advance is left to settle
    assert env.pvs.settle_staff_advances("PINV-2") == {"journal_entry": None, "settled": [], "left": 10000}


# ================================================================ 5. the holder
def _saved(env, **change):
    before = _dict(env.frappe.db.table(PHONE)["PCP-00001"])
    doc = Saved(env.frappe, PHONE, dict(before, **change))
    object.__setattr__(doc, "get_doc_before_save", lambda: before)
    return doc


def test_5_the_holders_user_and_name_are_never_typed(petty_env):  # noqa: F811
    env = petty_env
    doc = _saved(env, holder_user=ACCOUNTS, holder_name="Books Keeper")
    as_user(env, HOLDER, "Accounts Manager")  # the holder themselves
    comments = len(env.frappe.comments)
    env.pc.check_update(doc)
    assert (doc.holder_user, doc.holder_name, len(env.frappe.comments)) == (HOLDER, "Achieng Mary", comments)
    env.frappe.db.table(PHONE)["PCP-00001"].update(holder_user=doc.holder_user, holder_name=doc.holder_name)
    with raises(env, "You hold PCP-00001", env.frappe.PermissionError):  # still the holder
        env.pc.top_up("PCP-00001", 1000)


def test_5_a_new_holder_needs_a_manager_who_is_neither_holder(petty_env):  # noqa: F811
    env = petty_env
    env.frappe.db.table("Employee")["EMP-2"]["user_id"] = "otieno@example.com"
    for user, roles, message in ((HOLDER, ("Accounts Manager",), "You hold this phone"),  # the holder
                                 ("otieno@example.com", ("Accounts Manager",), "You hold this phone"),  # the new one
                                 (ACCOUNTS, ("Accounts User",), "needs one of the roles")):  # no manager
        as_user(env, user, *roles)
        with raises(env, message, env.frappe.PermissionError):
            env.pc.check_update(_saved(env, holder="EMP-2"))
    as_user(env, MANAGER, "M-Pesa Manager")
    doc = _saved(env, holder="EMP-2", holder_user=HOLDER)  # a stale User from the form
    env.pc.check_update(doc)
    assert (doc.holder, doc.holder_user, doc.holder_name) == ("EMP-2", "otieno@example.com", "Otieno Ouma")
    assert env.frappe.comments[-1][2] == (f"{MANAGER} changed holder: EMP-3 to EMP-2; holder_user: {HOLDER} to "
                                          "otieno@example.com.")
    with raises(env, "EMP-4 is Left: the holder is an active employee"):
        env.pc.check_update(_saved(env, holder="EMP-4"))
    # the limits alone: anyone who may write it but the holder
    as_user(env, ACCOUNTS, "Accounts User")
    env.pc.check_update(_saved(env, float_ceiling=30000))
    assert env.frappe.comments[-1][2] == f"{ACCOUNTS} changed float_ceiling: 20000 to 30000."


def test_5_a_new_user_id_on_the_holders_employee_is_a_change_of_holder(petty_env):  # noqa: F811
    env = petty_env
    env.frappe.db.table("Employee")["EMP-3"]["user_id"] = "new.login@example.com"  # HR gave the holder another login
    as_user(env, ACCOUNTS, "Accounts User")
    with raises(env, "needs one of the roles", env.frappe.PermissionError):
        env.pc.check_update(_saved(env, float_ceiling=30000))
    as_user(env, MANAGER, "M-Pesa Manager")
    doc = _saved(env, float_ceiling=30000)
    env.pc.check_update(doc)
    assert doc.holder_user == "new.login@example.com"
    assert f"holder_user: {HOLDER} to new.login@example.com" in env.frappe.comments[-1][2]


# ================================================================ 6. the name the SMS gave, and Pochi la Biashara
def test_6_only_the_name_the_sms_gave_is_kept_never_a_typed_one(spends, monkeypatch):
    known = importlib.import_module("mpsa_suite.money_out.known_names")
    seen = []
    monkeypatch.setattr(known, "observe", lambda *a, **kw: seen.append((a, kw)))
    with raises(spends, r"The name paid typed \(EXPECTED EMPLOYEE\) is not the SMS's \(JOHN DOE\)"):
        spends.pc.record_spend(PI, "PINV-1", sms=SEND_SMS, payee="EXPECTED EMPLOYEE")
    assert seen == [] and not spends.frappe.db.table(PE)
    out = spends.pc.record_spend(PI, "PINV-1", sms=SEND_SMS, payee="john  doe")  # the same name, as typed
    assert seen[-1][0] == ("Phone", "254712345678", "254712345678 - JOHN DOE")
    assert spends.frappe.db.table(PE)[out["payment_entry"]]["mpesa_sms_payee"] == "JOHN DOE"
    seen.clear()  # typed with no SMS: recorded, never kept as a known name
    spends.pc.record_spend(PI, "PINV-1", receipt="UKD1ABC2DZ", amount=700, sms_date="2026-11-13", sms_time="08:05",
                           payee="JOHN DOE")
    assert seen == []


def test_6_a_pochi_la_biashara_is_no_paybill(spends, monkeypatch):
    read = spends.pc.parse_sms(POCHI_SMS)
    assert (read.kind, read.payee, read.account, read.phone) == ("Pochi la Biashara", "MAMA MBOGA", None, None)
    sent = spends.pc.parse_sms(SEND_SMS.replace("JOHN DOE 0712345678", "0712345678"))  # a number M-Pesa names no one
    assert (sent.kind, sent.payee, sent.phone) == ("Phone", None, "254712345678")
    known = importlib.import_module("mpsa_suite.money_out.known_names")
    seen = []
    monkeypatch.setattr(known, "observe", lambda *a, **kw: seen.append(a))
    spends.pc.record_spend(PI, "PINV-1", sms=POCHI_SMS)  # ACME has one approved paybill (MSA-1): not its name
    assert seen == []
    spends.pc.record_spend(PI, "PINV-1", sms=PAYBILL_SMS.replace("for account 12345", "for account ACME-1"))
    assert seen == [("Paybill", "888880", "KPLC PREPAID")]  # a paybill's, with its account: as before


# ================================================================ 7. an SMS without a balance
def test_7_an_sms_without_a_balance_is_recorded_as_such(spends):
    out = spends.pc.record_spend(PI, "PINV-1", sms=NO_BALANCE_SMS)
    pe = spends.frappe.db.table(PE)[out["payment_entry"]]
    assert (pe["mpesa_sms_balance"], pe["mpesa_sms_has_balance"], out["sms_balance"]) == (0, 0, None)
    out = spends.pc.record_spend(PI, "PINV-1", sms=TILL_SMS)
    pe = spends.frappe.db.table(PE)[out["payment_entry"]]
    assert (pe["mpesa_sms_balance"], pe["mpesa_sms_has_balance"]) == (8500, 1)


def test_7_a_spend_with_no_balance_is_never_the_last_sms(petty_env):  # noqa: F811
    env = petty_env
    paid_top_up(env, "TU-A", 10000, datetime.datetime(2026, 11, 10, 9, 0))
    env.gl(debit=10000)
    spend_entry(env, "PE-1", 1500, datetime.datetime(2026, 11, 11, 10, 15), 8500)
    env.gl(credit=1500)
    spend_entry(env, "PE-2", 500, datetime.datetime(2026, 11, 12, 10, 15), None)  # stored as Frappe does: 0.0
    env.gl(credit=500)
    assert env.frappe.db.table(PE)["PE-2"]["mpesa_sms_balance"] == 0.0
    status = env.pc.sms_status(_dict(env.frappe.db.table(PHONE)["PCP-00001"]))
    assert (status.book, status.last_sms.name, status.expected, status.difference) == (8000, "PE-1", 8000, 0)
    assert env.pc.attention_section()[1] == []


# ================================================================ 8. the charge
def test_8_the_sms_transaction_cost_wins_over_a_typed_charge(spends):
    with raises(spends, r"The transaction charge typed \(900.0\) is not the SMS's \(0.0\)"):
        spends.pc.record_spend(PI, "PINV-1", sms=TILL_SMS, charge=900)
    assert not spends.frappe.db.table(PE)
    out = spends.pc.record_spend(PI, "PINV-1", sms=PAYBILL_SMS, charge=23)  # the same: as the SMS says
    pe = spends.frappe.db.table(PE)[out["payment_entry"]]
    assert (out["charge"], pe["deductions"][0]["account"], pe["deductions"][0]["amount"]) == (23, CHARGES, 23)


def test_8_a_charge_typed_alone_is_at_most_the_larger_of_100_and_1_percent(reader):  # noqa: F811
    pc, frappe = reader
    typed = dict(receipt="UKD1ABC2DZ", sms_date="2026-11-13", sms_time="08:05")
    assert pc.spend_values(amount=700, charge=100, **typed).charge == 100
    with pytest.raises(frappe.ValidationError, match=r"A transaction charge of KES 101 on KES 700 is more than M-Pesa "
                                                     r"charges \(at most KES 100 is taken\)"):
        pc.spend_values(amount=700, charge=101, **typed)
    assert pc.spend_values(amount=50000, charge=500, **typed).charge == 500
    with pytest.raises(frappe.ValidationError, match="at most KES 500 is taken"):
        pc.spend_values(amount=50000, charge=501, **typed)
    # an SMS pasted without its cost line: the typed charge, capped the same way
    assert pc.spend_values(NO_COST_SMS, charge=23).charge == 23
    with pytest.raises(frappe.ValidationError, match="at most KES 100 is taken"):
        pc.spend_values(NO_COST_SMS, charge=150)
    assert pc.spend_values(NO_COST_SMS).charge is None


# ================================================================ 9. a supplier on hold
def test_9_no_staff_advance_for_a_supplier_on_hold(staff_env):  # noqa: F811
    env = staff_env
    env.frappe.db.table("Supplier")["ACME"].update(on_hold=1, hold_type="All", release_date=None)
    with raises(env, "ACME is on hold for payments."):
        env.pvs.make_advance(PO, "PO-1", "EMP-2", 1000)
    assert not env.made and not env.frappe.db.table(DISB)
    assert env.pvs.document_state(PO, "PO-1")["allowed"] is False
    # an advance made before the hold: its payout is not sent, nor sent again, while it lasts (as B2B's send)
    staff_advance(env, "HR-EAD-S1", paid=0)
    with raises(env, "Row #1: HR-EAD-S1 pays ACME: ACME is on hold for payments."):
        env.pvs.check_send(_dict(references=[_dict(reference_doctype=EA, reference_name="HR-EAD-S1", idx=1)]))
    with raises(env, "HR-EAD-S1 pays ACME: ACME is on hold for payments."):
        env.pvs.check_retry(_dict(reference_doctype=EA, reference_name="HR-EAD-S1"))
    env.frappe.db.table("Supplier")["ACME"].update(on_hold=0)
    env.pvs.check_send(_dict(references=[_dict(reference_doctype=EA, reference_name="HR-EAD-S1", idx=1)]))


# ================================================================ 10. the spend fields without HRMS
def _migrate(monkeypatch):
    custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    custom_field.create_custom_fields = lambda fields, update=False: None
    for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, custom_field.__name__, custom_field)
    return importlib.import_module("mpsa_suite.mpsa_suite.migrate")


@pytest.mark.parametrize("apps, staff", [(["frappe", "erpnext"], False), (["frappe", "erpnext", "hrms"], True)])
def test_10_the_spend_fields_are_made_with_or_without_hrms(monkeypatch, apps, staff):
    frappe, saved = fake_frappe.install()
    try:
        migrate = _migrate(monkeypatch)
        made, sqls = [], []
        monkeypatch.setattr(migrate, "create_fields", lambda fields: made.append(fields))
        frappe.get_installed_apps = lambda: apps
        frappe.db.columns = {(PE, "mpesa_sms_has_balance")}
        frappe.db.sql = lambda query, *a, **kw: sqls.append(" ".join(query.split()))
        migrate.create_till_payment_fields()
        (fields,) = made
        assert {PE, PO, PI} <= set(fields) and ("Employee Advance" in fields) is staff
        pe = {f["fieldname"]: f for f in fields[PE]}
        flag = pe["mpesa_sms_has_balance"]
        assert (flag["fieldtype"], flag["read_only"], flag["no_copy"], flag["hidden"]) == ("Check", 1, 1, 1)
        assert pe["mpesa_sms_balance"]["depends_on"] == "eval:doc.mpesa_petty_cash_phone && doc.mpesa_sms_has_balance"
        # spends recorded before the field: a balance stored other than 0 was the SMS's
        (backfill,) = sqls
        assert backfill.startswith("UPDATE `tabPayment Entry` SET mpesa_sms_has_balance = 1")
        assert "COALESCE(mpesa_sms_balance, 0) != 0" in backfill
    finally:
        fake_frappe.uninstall(saved)


def test_10_after_migrate_makes_them_with_erpnext_not_only_with_hrms():
    tree = ast.parse((APP / "mpsa_suite" / "migrate.py").read_text(encoding="utf-8"))
    after = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "after_migrate")
    blocks = {ast.literal_eval(n.test.args[0]): ast.unparse(n) for n in after.body
              if isinstance(n, ast.If) and isinstance(n.test, ast.Call) and n.test.args}
    assert "create_till_payment_fields()" in blocks["erpnext"]
    assert "create_till_payment_fields()" not in blocks["hrms"]
