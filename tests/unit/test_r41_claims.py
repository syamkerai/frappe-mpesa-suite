"""Round 4.1 (the defects of Trident's live test, 2026-10-28) on the in-memory site of
test_claim_payees: staff and self payments refused or flagged (a user's phone, the name M-Pesa
paid now or before), a bank account never verified by name, phones masked, a claim payee's
approved supplier account recorded, the maker-checker texts, the bookings that lock their document
first, and the statuses of payout batches and B2B requests.
"""

import datetime
import importlib
import sys
import types

import b2b_fixture
import fake_frappe
import pytest
from test_claim_payees import (  # noqa: F401 (env, hrms_modules: fixtures)
    B2B, B2C, BANK, CLAIM, LINE, PHONE, ROW, SUITE, TILL, b2b_doc, claim, disbursement, env, hrms_modules,
    hrms_stub, paid_result, payee, raises, request, settings, stored, validate)

_dict = fake_frappe._dict
PAYER, OTHER = "payer@example.com", "colleague@example.com"


@pytest.fixture
def staff(env, monkeypatch):
    """Named staff: the claimant Mary Atieno (EMP-1), an employee John Kamau (EMP-2), a payer with a
    login and a phone but no Employee, and an M-Pesa Manager; alerts kept with their roles."""
    db = env.frappe.db
    db.table("Employee")["EMP-1"].update(employee_name="Mary Atieno")
    db.table("Employee")["EMP-2"].update(employee_name="John Kamau")
    env.meta_fields["User"] = {"mobile_no", "phone", "full_name"}
    db.columns = db.columns | {(LINE, "staff_check")}
    db.table("User").update({
        PAYER: {"name": PAYER, "enabled": 1, "user_type": "System User", "mobile_no": "0722 999 001", "phone": None,
                "full_name": "Peter Payer"},
        OTHER: {"name": OTHER, "enabled": 1, "user_type": "System User", "mobile_no": None, "phone": "0722999002",
                "full_name": "Carol Colleague"},
        "manager@example.com": {"name": "manager@example.com", "enabled": 1, "user_type": "System User",
                                "full_name": "Mike Manager"},
        "customer@example.com": {"name": "customer@example.com", "enabled": 1, "user_type": "Website User",
                                 "mobile_no": "0722999003"}})
    env.staff_alerts = []
    monkeypatch.setattr(env.cp.pa, "alert", lambda message, doc, *a, **kw: env.staff_alerts.append(
        (doc.name, message, kw)) or env.alerts.append((doc.name, message)))
    return env


# ---------------------------------------------------------------- (a) a user's phone is staff's
def test_a_users_phone_is_refused_as_a_payee_and_your_own_is_said_so(staff):
    found = staff.cp.employee_destinations()
    assert found.users == {"B2C:254722999001": PAYER, "B2C:254722999002": OTHER}  # no website user
    line = payee(payee_type=PHONE, phone="0722 999 001")
    staff.cp.clean_line(line, set(), [])
    staff.frappe.session.user = PAYER
    assert "0722 999 001 is your own phone" in staff.cp.employee_problem(line, "EMP-1", found)
    staff.frappe.session.user = "accounts@example.com"
    assert "the phone of a user of this site (staff)" in staff.cp.employee_problem(line, "EMP-1", found)
    # at the send a phone is masked, as the summary shows it
    assert "2547****9001 is the phone of a user" in staff.cp.employee_problem(line, "EMP-1", found, masked=True)


def test_a_claim_line_to_a_users_phone_is_refused_on_save_and_in_a_pasted_list(staff):
    doc = claim(staff, [payee(payee_type=PHONE, phone="0722 999 002")], store=False)
    with raises(staff, "the phone of a user of this site"):
        validate(staff, doc)
    rows = staff.cp.table_rows([["Ann", "0722999001", "500"], ["Bob", "0722999002", "500"]])
    staff.frappe.session.user = PAYER
    checked = staff.cp.check_pasted(rows, "EMP-1", (), staff.cp.employee_destinations())
    assert checked[0]["errors"] == ["your own phone: staff are paid by reimbursing their claims"]
    assert "user of this site (staff)" in checked[1]["errors"][0]


def test_an_employees_own_login_phone_is_the_employees(staff):
    staff.meta_fields["Employee"] = {"cell_number", "bank_ac_no", "user_id"}
    staff.frappe.db.table("Employee")["EMP-1"]["user_id"] = OTHER
    line = payee(payee_type=PHONE, phone="0722999002")
    staff.cp.clean_line(line, set(), [])
    assert "the claimant's own phone" in staff.cp.employee_problem(line, "EMP-1", staff.cp.employee_destinations())


def test_an_employees_bank_account_on_erpnexts_bank_account_is_refused(staff):
    staff.frappe.db.columns = staff.frappe.db.columns | {("Bank Account", "party_type")}
    staff.frappe.db.table("Bank Account")["BA-1"] = {"name": "BA-1", "party_type": "Employee", "party": "EMP-1",
                                                     "bank_account_no": "1234-567-890", "iban": None}
    line = payee(payee_type=BANK)  # KCB 1234567890: the claimant's own (HR-EXP-2026-00713)
    staff.cp.clean_line(line, set(), [])
    assert "is the claimant's own bank account" in staff.cp.employee_problem(line, "EMP-1",
                                                                             staff.cp.employee_destinations())


# ---------------------------------------------------------------- (b) the name M-Pesa paid is staff's
def paid_till(env, received, sent_by=PAYER, owner="accounts@example.com", **line_kw):
    line = payee(**line_kw)
    claim(env, [line])
    stored(env, line.name).update(sent_by=sent_by)
    request(env, line, name="MB2B-1", docstatus=1, status="Initiated", owner=owner)
    paid_result(env, "MB2B-1", receiver=received)
    env.cp.on_payout_result(B2B, "MB2B-1")
    return stored(env, line.name)


@pytest.mark.parametrize("received, who", [
    ("835811 - MARY ATIENO", "the name of the claimant"),
    ("835811 - PETER PAYER", "the name of its sender"),
    ("835811 - JOHN KAMAU STORES", "an employee's name"),
])
def test_a_line_paid_under_a_staff_name_is_flagged_and_its_destination_blocked(staff, received, who):
    row = paid_till(staff, received)
    assert row["staff_check"] == "Blocked" and who in row["staff_note"]
    assert "EMP-2" not in row["staff_note"]  # an employee is not named on the claim
    (claim_name, message, kw) = [a for a in staff.staff_alerts if "Staff or self payment" in a[1]][0]
    assert kw["roles"] == ("M-Pesa Manager",) and set(kw["exclude"]) == {PAYER, "accounts@example.com"}
    assert any("staff or self payment" in e[1].get("title", "") for e in staff.frappe.errors)
    # the destination is refused for every claim payee now
    doc = claim(staff, [payee("LN-2", shortcode="835811")], name="HR-EXP-2", store=False)
    with raises(staff, "835811 is blocked for claim payees: M-Pesa paid it under a staff member's name \\(HR-EXP-1"):
        validate(staff, doc)


def test_an_employee_name_is_checked_only_while_the_setting_says_so(staff):
    settings(staff, claim_payee_employee_names=0)
    assert not paid_till(staff, "835811 - JOHN KAMAU STORES").get("staff_check")
    settings(staff, claim_payee_employee_names=1)


def test_one_first_name_alone_matches_no_staff_member(staff):
    assert not paid_till(staff, "835811 - MARY").get("staff_check")
    assert staff.cp.staff_match("MARY JANE", [("the claimant", "Mary")]) is None


def test_a_bank_account_is_never_flagged_by_name(staff):
    assert not paid_till(staff, "522522 - KCB Paybill AC", payee_type=BANK).get("staff_check")


def test_the_send_refuses_a_number_paid_before_under_a_staff_name_and_flags_it_from_a_job(staff):
    request(staff, payee("LN-0", shortcode="835811"), name="MB2B-0", docstatus=1, status="Paid",
            receiver_public_name="835811 - MARY ATIENO SHOP")  # an earlier payout, not of a claim payee
    line = payee(shortcode="835811")
    claim(staff, [line])
    staff.frappe.session.user = PAYER
    with raises(staff, "Staff or self payment: M-Pesa paid '835811 - MARY ATIENO SHOP', the name of the claimant"):
        staff.cp.check_b2b(b2b_doc(staff, line, docstatus=1))
    assert stored(staff)["status"] == "Draft"  # not noted as sent
    (method, kw), = [e for e in staff.frappe.enqueued if e[0].endswith("claim_payees.flag_staff")]
    assert "enqueue_after_commit" not in kw and kw["line"] == "LN-1" and PAYER in kw["exclude"]
    staff.cp.flag_staff(kw["claim"], kw["line"], kw["note"], kw["exclude"])  # the job
    assert stored(staff)["staff_check"] == "Blocked"


def test_a_cleared_destination_is_sent_and_never_flagged_again(staff):
    request(staff, payee("LN-0", shortcode="835811"), name="MB2B-0", docstatus=1, status="Paid",
            receiver_public_name="835811 - MARY ATIENO SHOP")
    line = payee(shortcode="835811")
    claim(staff, [line])
    stored(staff).update(staff_check="Cleared", staff_note="Cleared by m: checked")
    staff.frappe.session.user = PAYER
    staff.cp.check_b2b(b2b_doc(staff, line, docstatus=1))
    assert (stored(staff)["status"], stored(staff)["sent_by"]) == ("In Flight", PAYER)


def test_a_manager_who_was_not_involved_clears_a_staff_flag(staff):
    row = paid_till(staff, "835811 - JOHN KAMAU STORES")
    assert row["staff_check"] == "Blocked"
    staff.frappe.session.user, staff.frappe.user_roles = "accounts@example.com", {"Accounts User"}
    with pytest.raises(staff.frappe.PermissionError):
        staff.cp.clear_staff_flag("HR-EXP-1", "LN-1", "checked")
    staff.frappe.user_roles = {"M-Pesa Manager"}
    staff.frappe.session.user = PAYER  # sent it
    with pytest.raises(staff.frappe.PermissionError, match="another M-Pesa Manager clears it"):
        staff.cp.clear_staff_flag("HR-EXP-1", "LN-1", "checked")
    staff.frappe.session.user = "manager@example.com"
    with raises(staff, "Say how you checked"):
        staff.cp.clear_staff_flag("HR-EXP-1", "LN-1", " ")
    assert staff.cp.clear_staff_flag("HR-EXP-1", "LN-1", "John Kamau Stores is a shop in Thika") == {
        "line": "LN-1", "cleared": 1}
    row = stored(staff)
    assert row["staff_check"] == "Cleared" and "Cleared by manager@example.com" in row["staff_note"]
    assert "Staff flag on 835811 cleared for claim payees by manager@example.com" in staff.frappe.comments[-1][2]
    assert staff.cp.blocked_destinations() == {}


# ---------------------------------------------------------------- (c) a bank account is never verified
def test_names_are_matched_with_at_least_two_words_for_staff(env):
    assert env.cp.pa.names_match("Mary", "MARY JANE")  # a payee name: as before
    assert not env.cp.pa.names_match("Mary", "MARY JANE", min_words=2)
    assert env.cp.pa.names_match("Mary Jane", "MARY JANE DOE", min_words=2)


# ---------------------------------------------------------------- masked phones
def test_a_phone_is_masked_in_the_name_check_and_its_alarm(env):
    line = payee(payee_type=PHONE)
    claim(env, [line])
    verdict, note = env.cp.name_check(line, "254711000999 - JOHN DOE")
    assert verdict is False and note == "M-Pesa paid '2547****0999 - JOHN DOE': number 2547****0999, not 2547****0333."
    line.update(name_note=note, parent="HR-EXP-1")
    env.cp._name_alarm("HR-EXP-1", line)
    assert "0711 000 333" not in env.frappe.comments[-1][2] and "2547****0333" in env.frappe.comments[-1][2]
    assert "254711000999" not in str(env.frappe.errors[-1]) and "254711000999" not in env.alerts[-1][1]


# ---------------------------------------------------------------- an approved supplier account paid from a claim
def test_a_line_paid_to_an_approved_supplier_account_is_recorded_on_the_request_and_account(env, monkeypatch):
    recorded = []
    monkeypatch.setattr(env.cp.pa, "record_receiver_name", lambda doctype, account, name, payout, notify=None:
                        recorded.append((doctype, account, name, payout.name)))
    line = payee()
    claim(env, [line])
    stored(env).update(supplier_account="MSA-00002-1")
    request(env, line, name="MB2B-1", docstatus=1, status="Initiated")
    paid_result(env, "MB2B-1")
    env.cp.on_payout_result(B2B, "MB2B-1")
    assert env.frappe.db.table(B2B)["MB2B-1"]["supplier_mpesa_account"] == "MSA-00002-1"
    assert recorded == [("Supplier M-Pesa Account", "MSA-00002-1", "835811 - SHELL KILIMANI", "MB2B-1")]


def test_a_draft_payee_request_drops_a_copied_supplier_account_and_a_sent_one_may_not_carry_one(env):
    line = payee()
    claim(env, [line])
    doc = b2b_doc(env, line, supplier_mpesa_account="MSA-1")  # an amended copy of a paid request
    env.cp.check_b2b(doc)
    assert doc.supplier_mpesa_account is None
    with raises(env, "clear Supplier M-Pesa Account"):
        env.cp.check_b2b(b2b_doc(env, line, docstatus=1, supplier_mpesa_account="MSA-1"))


# ---------------------------------------------------------------- texts and the form
@pytest.mark.parametrize("on, text", [(True, "An M-Pesa Payer who did not prepare them sends them"),
                                      (False, "An M-Pesa Payer sends them")])
def test_the_prepared_comment_says_who_sends_as_maker_checker_is(env, monkeypatch, on, text):
    schedule = importlib.import_module("mpsa_suite.money_out.schedule")
    monkeypatch.setattr(schedule, "maker_checker_on", lambda: on)
    claim(env, [payee()])
    env.cp.make_payouts([env.frappe.get_doc(CLAIM, "HR-EXP-1")], ("Draft",), b2b_fixture.SETTINGS)
    assert text in env.frappe.comments[-1][2]


def test_the_form_lists_lines_another_claims_staff_flag_blocks(env):
    env.frappe.db.columns = env.frappe.db.columns | {(LINE, "staff_check")}
    claim(env, [payee(staff_check="Blocked")], name="HR-EXP-1")
    stored(env).update(staff_check="Blocked", destination_key="TILL:835811")
    lines = [_dict(payee("LN-2", 2, status="Draft", destination_key="TILL:835811"))]
    assert env.cp._blocked_lines(lines) == [{"line": "LN-2", "idx": 2, "claim": "HR-EXP-1", "flagged_idx": 1}]


def test_the_name_verdict_patch(env):
    patch = importlib.import_module("mpsa_suite.mpsa_suite.patches.claim_payee_name_verdict")
    v = patch.verdict
    assert v(_dict(payee_type=TILL, name_matches=1, name_note="M-Pesa paid 'X'.")) == ("Matches", 1)
    assert v(_dict(payee_type=BANK, name_matches=1, name_note="the bank's own name")) == ("Not Verified", 0)
    assert v(_dict(payee_type=TILL, name_matches=0, name_note="Name not checked (till): ...")) == ("Not Verified", 0)
    assert v(_dict(payee_type=PHONE, name_matches=0, name_note="M-Pesa paid 'Y', but the claim gives 'Z'.")) == (
        "Does Not Match", 0)


# ---------------------------------------------------------------- bookings lock their document first
def test_a_read_only_transaction_is_committed_before_the_booking_lock(env):
    owed = env.owed
    env.frappe.db.transaction_writes = 0
    env.log.clear()
    with owed.booking_lock(CLAIM, "HR-EXP-1"):
        pass
    assert env.log[:2] == [("commit",), ("lock", "b2c-pay:Expense Claim:HR-EXP-1")]
    env.frappe.db.transaction_writes = 3  # a caller's writes are never committed early
    assert owed.fresh_snapshot() is False
    env.frappe.db.transaction_writes = 0
    env.frappe.local.mpesa_named_locks = {"mpesa:held"}  # nor a lock held until commit released
    assert owed.fresh_snapshot() is False
    env.frappe.local.mpesa_named_locks = set()


def test_a_b2c_payouts_booking_locks_the_document_it_pays_first(env, monkeypatch):
    for name, attr in (("mpsa_suite.accounting.journal_service", "create_journal_entry"),
                       ("mpsa_suite.accounting.payment_entry_service", "create_payment_entry_for_doc")):
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**{attr: None}))
    monkeypatch.delitem(sys.modules, "mpsa_suite.services.b2c_postprocess_service", raising=False)
    post = importlib.import_module("mpsa_suite.services.b2c_postprocess_service")
    monkeypatch.setattr(post, "_handle", lambda d, r: env.log.append(("booked", d, r)) or True)
    env.frappe.db.table(ROW)["R-1"] = {"name": "R-1", "parent": "D-1", "reference_doctype": CLAIM,
                                       "reference_name": "HR-EXP-1"}
    env.frappe.db.transaction_writes = 0
    env.log.clear()
    assert post.handle_successful_payment("D-1", "R-1") is True
    assert env.log == [("row", ROW, "R-1", False), ("commit",), ("lock", "b2c-pay:Expense Claim:HR-EXP-1"),
                       ("booked", "D-1", "R-1")]


def test_a_b2b_booking_commits_its_peek_away_and_locks_the_document_first(env):
    line = payee()
    claim(env, [line])
    request(env, line, name="MB2B-1", docstatus=1, status="Paid", transaction_id="RCPT1")
    env.frappe.db.transaction_writes = 0
    env.log.clear()
    with env.entries._locks("MB2B-1", CLAIM, "HR-EXP-1", "RCPT1"):
        pass
    locks = [e for e in env.log if e[0] in ("commit", "lock")]
    assert locks[:3] == [("commit",), ("lock", "b2c-pay:Expense Claim:HR-EXP-1"), ("lock", "book:RCPT1")]


def test_the_entry_sweep_locks_each_document_before_its_savepoint(env, monkeypatch):
    entries = env.entries
    monkeypatch.setattr(env.frappe.db, "sql", lambda *a, **kw: [_dict(name="MB2B-1")])
    monkeypatch.setattr(entries, "_peek", lambda name: (CLAIM, "HR-EXP-1", "RCPT1"))
    monkeypatch.setattr(entries, "_refresh_one", lambda name, cutoff: env.log.append(("refresh", name)))
    monkeypatch.setattr(entries, "_remind", lambda name, days: None)
    env.frappe.db.transaction_writes = 0
    env.log.clear()
    entries.refresh_entries()
    order = [e[0] if e[0] != "lock" else e[1] for e in env.log]
    assert order.index("b2c-pay:Expense Claim:HR-EXP-1") < order.index("savepoint") < order.index("refresh")


# ---------------------------------------------------------------- statuses
@pytest.mark.parametrize("rows, status", [
    ([], "Not Initiated"),
    (["Paid", "Paid"], "Paid"),
    (["Failed", "Failed"], "Failed"),
    (["Paid", "Failed"], "Partially Paid"),
    (["Paid", "Initiated"], "Partially Paid"),  # what was paid shows while the rest is on its way
    (["Failed", "Initiated"], "Initiated"),  # nothing paid yet: never Partially Paid
    (["Not Initiated", "Initiated"], "Initiated"),
])
def test_a_batch_status_follows_its_rows(env, rows, status):
    batch = importlib.import_module("mpsa_suite.services.b2c_batch_status")
    assert batch.batch_status(rows) == status


def test_a_batch_status_is_written_under_its_lock_and_only_when_it_changed(env):
    batch = importlib.import_module("mpsa_suite.services.b2c_batch_status")
    db = env.frappe.db
    db.table(B2C)["D-1"] = {"name": "D-1", "docstatus": 1, "status": "Initiated"}
    db.table(ROW).update({"D-1-R1": {"name": "D-1-R1", "parent": "D-1", "parenttype": B2C, "parentfield": "references",
                                     "payment_status": "Paid"},
                          "D-1-R2": {"name": "D-1-R2", "parent": "D-1", "parenttype": B2C, "parentfield": "references",
                                     "payment_status": "Initiated"}})
    assert batch.sync_status("D-1") == "Partially Paid" and db.table(B2C)["D-1"]["status"] == "Partially Paid"
    assert ("row", B2C, "D-1", True) in env.log
    db.table(B2C)["D-2"] = {"name": "D-2", "docstatus": 0, "status": None}
    assert batch.sync_status("D-2") is None  # a draft keeps its status
    batch.sync_status_later("D-1")
    assert env.frappe.enqueued[-1] == ("mpsa_suite.services.b2c_batch_status.sync_status",
                                       {"queue": "short", "timeout": 300, "enqueue_after_commit": True, "name": "D-1"})


def test_the_status_job_heals_rows_and_syncs_every_recent_batch_without_a_retry_cap(env, monkeypatch):
    scheduler = importlib.import_module("mpsa_suite.services.b2c_scheduler_service")
    response = importlib.import_module("mpsa_suite.services.b2c_response_service")
    batch = importlib.import_module("mpsa_suite.services.b2c_batch_status")
    healed, synced = [], []
    monkeypatch.setattr(env.frappe.db, "sql", lambda *a, **kw: [_dict(name="REQ-1", status="Failed")])
    monkeypatch.setattr(response, "update_b2c_reference_status", lambda name, enqueue_next=False: healed.append(
        (name, enqueue_next)))
    monkeypatch.setattr(batch, "sync_status", lambda name: synced.append(name))
    env.frappe.db.table(B2C).update({
        "D-1": {"name": "D-1", "docstatus": 1, "status": "Initiated", "retry_count": 9, "creation": "2026-10-28"},
        "D-2": {"name": "D-2", "docstatus": 1, "status": "Paid", "creation": "2026-10-28"}})
    scheduler.update_b2c_disbursement_statuses()
    assert healed == [("REQ-1", False)] and synced == ["D-1"]


def test_a_b2b_request_whose_result_never_came_is_asked_about(env, monkeypatch):
    status = importlib.import_module("mpsa_suite.money_out.b2b_status")
    monkeypatch.setattr(status, "migrated", lambda: True)
    asked = []
    monkeypatch.setattr(status, "start_query", lambda name: asked.append(name) or "Q-1")
    old = datetime.datetime(2026, 10, 18, 9, 0)  # sent three hours before the fake's now
    env.frappe.db.table(B2B).update({
        "MB2B-1": {"name": "MB2B-1", "docstatus": 1, "status": "Initiated", "result_code": None,
                   "mpesa_settings": "S", "originator_conversation_id": "OC-1", "modified": old, "creation": old},
        "MB2B-2": {"name": "MB2B-2", "docstatus": 1, "status": "Paid", "result_code": "0", "mpesa_settings": "S",
                   "originator_conversation_id": "OC-2", "modified": old, "creation": old}})
    status.sweep()
    assert asked == ["MB2B-1"]
