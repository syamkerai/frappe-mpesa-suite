"""Payouts Phase 3 review fixes (round 1), on the in-memory site of test_payroll_schedule.

Covers: a salary is never paid both by an HRMS bank entry and by M-Pesa (the payroll's bank or
cash entries, drafts and entries naming no employee, are checked at Pay Selected, at a B2B
salary's send, by the job that makes a B2C payout and by its send); a scheduled B2C payout is
committed with its row's link before it is sent, and its send commits a marker first, so the
lost-job sweep fails only what provably never reached M-Pesa and makes an interrupted send
Status Unknown; a scheduled B2C payout cancelled while M-Pesa may have paid it leaves its line
Unknown (a manager only plans it again), only a manager cancels one in flight, a Paid one is never
cancelled or deleted, a cancelled disbursement sends nothing, and a row is paid by one payout
only, never by one typed by hand; nobody pays themselves, and a payment to an employee with no
Salary Slip is paid by a manager; salaries are shown only to who may see them (the report, the
schedule, the B2B requests); a Salary Slip with M-Pesa payments is not cancelled.
"""

import datetime
import importlib
import itertools
import sys
import types

import pytest

import fake_frappe
from test_payroll_schedule import (  # noqa: F401
    ACCOUNTS,
    B2B,
    LINE,
    NOW,
    PAYER,
    PAYROLL,
    PHONE,
    REQ,
    ROW,
    SCHEDULE,
    SETTINGS,
    SLIP,
    bank_line,
    confirmed_phone,
    env,
    line_of,
    phone_line,
    prepared,
    run_jobs,
    schedule_row,
)

DISB = "B2C Payment Disbursement"
JE, JEA = "Journal Entry", "Journal Entry Account"
BANK_PROBLEM = "PAY-1 has a bank or cash entry not made by M-Pesa (ACC-JV-9)"


def hrms_bank_entry(env, docstatus=0, party=None):
    """What HRMS's Make Bank Entry leaves: a draft Bank Entry whose payable row names the payroll
    and, with employee-wise accounting off (the default), no employee."""
    env.je("ACC-JV-9", None, [{"account": "Payroll Payable - AIL", "debit_in_account_currency": 80000,
                               "reference_type": PAYROLL, "reference_name": "PAY-1", "party_type":
                                   "Employee" if party else None, "party": party},
                              {"account": "Bank - AIL", "credit_in_account_currency": 80000}],
           voucher_type="Bank Entry", docstatus=docstatus)


# ---------------------------------------------------------------- never paid by bank and by M-Pesa
@pytest.mark.parametrize("docstatus, party", [(0, None), (1, None), (0, "EMP-1")])
def test_pay_selected_refuses_a_salary_whose_payroll_has_a_bank_entry(env, docstatus, party):
    schedule_row(env)
    bank_line(env)
    hrms_bank_entry(env, docstatus, party)
    reason = env.schedule.prepare_payment(["L1"])["not_included"][0]["reason"]
    assert reason.startswith(BANK_PROBLEM)
    env.frappe.db.table(JE)["ACC-JV-9"]["docstatus"] = 2  # cancelled: M-Pesa may pay it
    assert env.schedule.prepare_payment(["L1"])["not_included"] == []


def test_an_m_pesa_salary_entry_is_not_a_bank_entry_of_the_payroll(env):
    hrms_bank_entry(env, docstatus=1)
    env.frappe.db.table(JEA)["ACC-JV-9-1"]["mpesa_b2b_payment_request"] = "MB2B-1"
    assert env.owed.payroll_bank_entries("PAY-1") == []
    env.frappe.db.table(JE)["ACC-JV-9"]["voucher_type"] = "Journal Entry"  # the accrual
    env.frappe.db.table(JEA)["ACC-JV-9-1"]["mpesa_b2b_payment_request"] = None
    assert env.owed.payroll_bank_entries("PAY-1") == []


def test_a_bank_salary_is_not_sent_once_hr_made_a_bank_entry_after_pay_selected(env):
    schedule_row(env)
    bank_line(env)
    env.schedule.confirm_payment(prepared(env, "L1")["token"])
    hrms_bank_entry(env)  # Wednesday: HR presses Make Bank Entry
    run_jobs(env, "schedule.send_batch")
    request = env.frappe.db.table(B2B)[line_of(env, "L1")["request"]]
    assert env.posted == []
    assert (request["status"], request["error_message"][:9]) == ("Failed", "Not sent:")
    assert BANK_PROBLEM in request["error_message"]


def test_a_b2b_salary_made_by_hand_is_refused_beside_a_bank_entry(env):
    hrms_bank_entry(env)
    doc = env.B2BDoc({"name": "MB2B-H1", "employee": "EMP-1", "salary_slip": "SAL-1", "amount": 1000,
                      "mpesa_settings": SETTINGS, "company": "Astral"})
    with pytest.raises(env.frappe.ValidationError, match=r"PAY-1 has a bank or cash entry not made by M-Pesa"):
        env.b2b.validate_request(doc, at_submit=True)


def test_the_b2c_job_and_the_send_refuse_a_salary_paid_by_bank_meanwhile(env):
    job = confirmed_phone(env)
    hrms_bank_entry(env)
    env.disb.create_b2c_request(job["ref"], job["settings"], job["b2c_disbursement"])
    assert not env.frappe.db.table(REQ)
    line = line_of(env, "L2")
    assert line["status"] == "Failed" and line["last_problem"].startswith(f"Not sent: {BANK_PROBLEM}")
    row = fake_frappe._dict(env.frappe.db.table(ROW)[job["ref"].name])
    line["status"] = "Sending"
    assert env.schedule.b2c_problem(row, job["b2c_disbursement"].name).startswith(BANK_PROBLEM)


# ---------------------------------------------------------------- a B2C payout is committed before it is sent
def made_requests(env, monkeypatch):
    """B2C Disbursement Requests as the job makes them (stored on insert, submitted unsent, as the
    controller leaves a scheduled one), the connector replaced, and what each commit held."""
    seq = itertools.count(1)
    plain = env.frappe.get_doc

    class Request(fake_frappe.FakeDoc):
        def insert(self, ignore_permissions=False):
            self._row.setdefault("name", f"B2C-{next(seq)}")
            self._row.setdefault("status", "Not Initiated")
            self._row.setdefault("docstatus", 0)
            return super().insert()

        def submit(self):
            self._row["docstatus"] = 1

    def get_doc(doctype, name=None, **kw):
        if isinstance(doctype, dict) and doctype.get("doctype") == REQ:
            row = dict(doctype)
            doc = Request(env.frappe, row.pop("doctype"), row)
            object.__setattr__(doc, "flags", fake_frappe._dict())
            return doc
        return plain(doctype, name, **kw)

    monkeypatch.setattr(env.frappe, "get_doc", get_doc)
    service = importlib.import_module("mpsa_suite.services.b2c_request_service")
    sent, commits = [], []

    def state():
        req = next(iter(env.frappe.db.table(REQ).values()), {})
        row = next(iter(env.frappe.db.table(ROW).values()), {})
        return (row.get("b2c_disbursement_request"), req.get("send_started_on"), req.get("status"))

    def send_b2c_request(doc, callback_url):
        sent.append((doc.name, list(commits)))
        env.frappe.db.set_value(REQ, doc.name, "status", "Initiated")
        return {"ResponseCode": "0"}

    monkeypatch.setattr(service, "get_b2c_connector",
                        lambda provider, settings: types.SimpleNamespace(send_b2c_request=send_b2c_request))
    monkeypatch.setattr(service, "with_token", lambda url: url)
    monkeypatch.setattr(env.frappe.db, "commit", lambda: commits.append(state()))
    return sent, commits


def test_a_scheduled_b2c_payout_and_its_rows_link_are_committed_before_it_is_sent(env, monkeypatch):
    job = confirmed_phone(env)
    sent, commits = made_requests(env, monkeypatch)
    env.disb.create_b2c_request(job["ref"], job["settings"], job["b2c_disbursement"])
    ((name, before),) = sent
    # 1: the payout and the row's link, nothing sent yet; 2: the send's marker; then the call
    assert before == [(name, None, "Not Initiated"), (name, NOW, "Not Initiated")]
    assert commits[-1] == (name, NOW, "Initiated")
    assert [j for j in env.frappe.enqueued if j[0].endswith("schedule.sync_b2c_line")]


def test_a_scheduled_b2c_payout_is_sent_once(env, monkeypatch):
    job = confirmed_phone(env)
    sent, _ = made_requests(env, monkeypatch)
    env.disb.create_b2c_request(job["ref"], job["settings"], job["b2c_disbursement"])
    name = sent[0][0]
    assert env.schedule.send_b2c(name) is False  # sent already (its marker, then Initiated)
    env.frappe.db.table(REQ)[name]["status"] = "Not Initiated"
    assert env.schedule.send_b2c(name) is False  # its marker: another worker's, or an interrupted send
    assert len(sent) == 1


def test_a_check_failing_at_the_send_fails_the_payout_unsent(env, monkeypatch):
    job = confirmed_phone(env)
    sent, _ = made_requests(env, monkeypatch)
    real_send_b2c = env.schedule.send_b2c
    monkeypatch.setattr(env.schedule, "send_b2c", lambda name: None)  # the job dies before the send
    env.disb.create_b2c_request(job["ref"], job["settings"], job["b2c_disbursement"])
    (request,) = env.frappe.db.table(REQ).values()
    env.frappe.db.table(LINE)["L2"]["planned_date"] = NOW.date() + datetime.timedelta(days=1)
    assert real_send_b2c(request["name"]) is False and sent == []
    assert (request["status"], request["error_message"]) == ("Failed", "Not sent: Not due until 19 Oct 2026. A "
                                                             "scheduled payment cannot be sent before its planned day.")
    assert line_of(env, "L2")["status"] == "Failed"


def sending_line(env, request=None, minutes=61, **row):
    schedule_row(env)
    env.frappe.db.table(DISB)["MPESA-B2C-1"] = {"name": "MPESA-B2C-1", "docstatus": 1}
    phone_line(env, "L2", status="Sending", b2c_disbursement="MPESA-B2C-1", b2c_row="ROW-1",
               approved_on=NOW - datetime.timedelta(minutes=minutes))
    env.frappe.db.table(ROW)["ROW-1"] = {"name": "ROW-1", "parent": "MPESA-B2C-1", "idx": 1, "payment_status": "Initiated",
                                         "b2c_disbursement_request": request["name"] if request else None,
                                         "mpesa_schedule_line": "L2", **row}
    if request:
        env.frappe.db.table(REQ)[request["name"]] = {"docstatus": 1, "status": "Not Initiated",
                                                     "b2c_payment_disbursement": "MPESA-B2C-1",
                                                     "b2c_payment_disbursement_reference": "ROW-1", **request}


def test_a_send_interrupted_after_its_marker_becomes_status_unknown_never_failed(env):
    sending_line(env, {"name": "B2C-1", "send_started_on": NOW - datetime.timedelta(minutes=11)})
    env.schedule.sweep()
    request = env.frappe.db.table(REQ)["B2C-1"]
    assert (request["status"], request["error_message"]) == ("Status Unknown", env.schedule.B2C_INTERRUPTED)
    line = line_of(env, "L2")
    assert line["status"] == "Unknown" and "Transaction Status check" in line["last_problem"]
    assert env.alerts
    env.frappe.session.user, env.frappe.user_roles = ACCOUNTS, {"Accounts User"}
    with pytest.raises(env.frappe.ValidationError, match="A Transaction Status check settles it"):
        env.schedule.plan_again("L2")


def test_the_lost_job_sweep_leaves_a_payout_whose_send_began(env):
    # its marker is 5 minutes old (the interrupted rule waits 10): never failed by the hour rule
    sending_line(env, {"name": "B2C-1", "send_started_on": NOW - datetime.timedelta(minutes=5)})
    env.schedule.sweep()
    assert env.frappe.db.table(REQ)["B2C-1"]["status"] == "Not Initiated"
    assert line_of(env, "L2")["status"] == "Sending"


def test_the_lost_job_sweep_fails_a_payout_that_never_started(env):
    sending_line(env, {"name": "B2C-1", "send_started_on": None})
    env.schedule.sweep()
    request = env.frappe.db.table(REQ)["B2C-1"]
    assert request["status"] == "Failed" and request["error_message"].startswith("Not sent: the job that sends it")
    assert env.frappe.db.table(ROW)["ROW-1"]["payment_status"] == "Failed"
    assert line_of(env, "L2")["status"] == "Failed"
    env.frappe.session.user, env.frappe.user_roles = ACCOUNTS, {"Accounts User"}
    env.schedule.plan_again("L2")  # nothing was paid
    assert line_of(env, "L2")["status"] == "Planned"


def test_the_lost_job_sweep_never_fails_a_row_that_names_a_payout(env):
    # the row got its payout after the sweep's first read (or the payout is gone): not failed here
    sending_line(env, None, b2c_disbursement_request="B2C-GONE")
    env.schedule._give_up_b2c("ROW-1")
    assert env.frappe.db.table(ROW)["ROW-1"]["payment_status"] == "Initiated"
    assert line_of(env, "L2")["status"] == "Sending"


def test_the_controller_sends_nothing_on_submit_for_a_scheduled_payout(env, monkeypatch):
    req_module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request")
    sent = []
    monkeypatch.setattr(req_module, "make_b2c_payment_request", sent.append)
    env.frappe.db.table(ROW)["ROW-1"] = {"name": "ROW-1", "mpesa_schedule_line": "L2"}
    env.frappe.db.table(ROW)["ROW-9"] = {"name": "ROW-9", "mpesa_schedule_line": None}
    request = object.__new__(req_module.B2CDisbursementRequest)
    request.__dict__.update(name="B2C-1", b2c_payment_disbursement_reference="ROW-1", flags=fake_frappe._dict())
    request.on_submit()
    request.__dict__.update(name="B2C-9", b2c_payment_disbursement_reference="ROW-9")
    request.on_submit()  # a payout made by hand is sent on submit, as before
    assert sent == ["B2C-9"]


# ---------------------------------------------------------------- cancelled while M-Pesa may have paid it
@pytest.mark.parametrize("request_state, line_status", [
    ({"status": "Initiated"}, "Unknown"),
    ({"status": "Status Unknown"}, "Unknown"),
    ({"status": "Not Initiated", "send_started_on": NOW}, "Unknown"),
    ({"status": "Not Initiated"}, "Failed"),
    ({"status": "Failed"}, "Failed"),
])
def test_a_cancelled_payout_fails_its_line_only_when_it_never_reached_m_pesa(env, request_state, line_status):
    sending_line(env, {"name": "B2C-1", "docstatus": 2, **request_state})
    env.frappe.db.table(ROW)["ROW-1"]["b2c_disbursement_request"] = "B2C-1"
    env.schedule.sync_b2c_line("ROW-1")
    assert line_of(env, "L2")["status"] == line_status


def test_a_line_whose_payout_was_cancelled_in_flight_is_planned_again_by_a_manager_only(env):
    sending_line(env, {"name": "B2C-1", "docstatus": 2, "status": "Initiated"})
    env.schedule.sync_b2c_line("ROW-1")
    line_of(env, "L2")["b2c_request"] = "B2C-1"
    env.frappe.session.user, env.frappe.user_roles = ACCOUNTS, {"Accounts User"}
    with pytest.raises(env.frappe.ValidationError, match="B2C-1 was cancelled while it was Initiated"):
        env.schedule.plan_again("L2")
    env.frappe.session.user, env.frappe.user_roles = "boss@example.com", {"Accounts Manager"}
    env.schedule.plan_again("L2")  # checked the statement: not paid
    assert line_of(env, "L2")["status"] == "Planned"


def b2c_request_doc(env, monkeypatch, **values):
    req_module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request")

    class Request(req_module.B2CDisbursementRequest):
        doctype = REQ
        meta = types.SimpleNamespace(has_field=lambda field: field in ("status", "send_started_on", "error_message"))

        def get(self, key, default=None):
            return self.__dict__.get(key, default)

        def update(self, values):
            self.__dict__.update(values)

    env.frappe.db.table(ROW)["ROW-1"] = {"name": "ROW-1", "mpesa_schedule_line": "L2"}
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "status": "Not Initiated", "send_started_on": None,
                                         "error_message": None, **values}
    doc = object.__new__(Request)
    doc.__dict__.update(name="B2C-1", b2c_payment_disbursement_reference="ROW-1", status="Not Initiated",
                        flags=fake_frappe._dict())  # the form's copy, older than the database
    return doc


@pytest.mark.parametrize("values, manager_only, kept", [
    ({"status": "Initiated"}, True, "Initiated"),
    ({"status": "Status Unknown"}, True, "Status Unknown"),
    ({"send_started_on": NOW}, True, "Status Unknown"),  # its send began: kept as may have paid
    ({}, False, "Not Initiated"),
    ({"status": "Failed"}, False, "Failed"),
])
def test_only_a_manager_cancels_a_scheduled_payout_that_may_have_reached_m_pesa(env, monkeypatch, values,
                                                                                 manager_only, kept):
    env.frappe.session.user, env.frappe.user_roles = "hr@example.com", {"HR Manager"}
    doc = b2c_request_doc(env, monkeypatch, **values)
    if manager_only:
        with pytest.raises(env.frappe.PermissionError):
            doc.before_cancel()
        env.frappe.user_roles = {"Accounts Manager"}
        doc = b2c_request_doc(env, monkeypatch, **values)
    doc.before_cancel()
    assert doc.status == kept


def test_a_paid_scheduled_payout_is_never_cancelled_or_deleted(env, monkeypatch):
    env.frappe.session.user, env.frappe.user_roles = "boss@example.com", {"System Manager"}
    doc = b2c_request_doc(env, monkeypatch, status="Paid")
    with pytest.raises(env.frappe.ValidationError, match="it is the record of that payment"):
        doc.before_cancel()
    doc.status = "Initiated"
    with pytest.raises(env.frappe.ValidationError, match="cannot be deleted"):
        doc.on_trash()
    doc.status = "Failed"
    doc.on_trash()


def test_a_cancelled_disbursement_sends_nothing(env):
    job = confirmed_phone(env)
    env.frappe.db.table(DISB)[job["b2c_disbursement"].name]["docstatus"] = 2
    env.disb.create_b2c_request(job["ref"], job["settings"], job["b2c_disbursement"])
    assert not env.frappe.db.table(REQ)
    assert "is not submitted (cancelled?)" in line_of(env, "L2")["last_problem"]


def test_a_row_is_paid_by_one_payout_and_never_by_one_typed_by_hand(env):
    job = confirmed_phone(env)
    ref = job["ref"]
    env.frappe.db.table(REQ)["B2C-HAND"] = {"name": "B2C-HAND", "docstatus": 1, "status": "Not Initiated",
                                            "b2c_payment_disbursement_reference": ref.name}
    row = fake_frappe._dict(env.frappe.db.table(ROW)[ref.name])
    assert env.schedule.b2c_problem(row, job["b2c_disbursement"].name) == "B2C-HAND already pays this row: a row is " \
                                                                           "paid once."
    request = fake_frappe._dict(name="B2C-HAND", b2c_payment_disbursement=job["b2c_disbursement"].name,
                                b2c_payment_disbursement_reference=ref.name, phone_number=PHONE)
    with pytest.raises(env.frappe.ValidationError, match="is not the payout of its row"):
        env.schedule.assert_b2c_line_due(request)
    typed = fake_frappe._dict(b2c_payment_disbursement_reference=ref.name, flags=fake_frappe._dict())
    with pytest.raises(env.frappe.ValidationError, match="paid with Pay Selected"):
        env.schedule.check_b2c_request(typed)
    typed.flags.mpesa_from_schedule = True  # only the job that makes the payout sets it
    env.schedule.check_b2c_request(typed)


# ---------------------------------------------------------------- nobody pays themselves
def test_a_payer_cannot_pay_their_own_salary(env):
    env.frappe.db.table("Employee")["EMP-1"]["user_id"] = PAYER
    schedule_row(env)
    bank_line(env)
    assert env.schedule.prepare_payment(["L1"])["not_included"][0]["reason"] == (
        "You are EMP-1: another person must pay you.")
    doc = env.B2BDoc({"name": "MB2B-H1", "employee": "EMP-1", "salary_slip": "SAL-1", "amount": 1000,
                      "mpesa_settings": SETTINGS, "company": "Astral"})
    with pytest.raises(env.frappe.PermissionError, match="You are EMP-1"):
        env.b2b.validate_request(doc)


def test_a_payment_to_an_employee_with_no_salary_slip_is_paid_by_a_manager(env):
    schedule_row(env)
    bank_line(env, reference_doctype=None, reference_name=None, amount=200000, entry_mode="Make the entry later")
    assert env.schedule.prepare_payment(["L1"])["not_included"][0]["reason"] == (
        "A payment to an employee with no Salary Slip is paid by an M-Pesa or Accounts Manager.")
    doc = env.B2BDoc({"name": "MB2B-H1", "employee": "EMP-1", "amount": 200000, "mpesa_settings": SETTINGS,
                      "company": "Astral"})
    env.b2b.validate_request(doc)  # a draft may be saved
    with pytest.raises(env.frappe.PermissionError, match="paid by an M-Pesa or Accounts Manager"):
        env.b2b.validate_request(doc, at_submit=True)
    with pytest.raises(env.frappe.PermissionError, match="paid by an M-Pesa or Accounts Manager"):
        env.b2b._check_payer(doc, sending=True)
    env.frappe.session.user, env.frappe.user_roles = "boss@example.com", {"M-Pesa Manager"}
    env.b2b.validate_request(doc, at_submit=True)
    assert env.schedule.prepare_payment(["L1"])["not_included"] == []


# ---------------------------------------------------------------- who sees salaries
def access(env):
    return importlib.import_module("mpsa_suite.money_out.salary_access")


@pytest.mark.parametrize("roles, sees", [
    ({"M-Pesa Viewer"}, False), ({"Accounts User"}, False), ({"M-Pesa Payer"}, True), ({"HR User"}, True),
    ({"Accounts Manager"}, True), ({"Accounts User", "HR Manager"}, True),
])
def test_salaries_are_shown_to_who_may_read_salary_slips_pay_them_or_manage(env, roles, sees):
    env.frappe.session.user, env.frappe.user_roles = "someone@example.com", roles
    salary = access(env)
    assert salary.can_see_salaries() is sees
    payroll_schedule = fake_frappe._dict(name="MPS-X", purpose="Payroll", lines=[])
    assert salary.schedule_has_permission(payroll_schedule, "read") is sees
    supplier_schedule = fake_frappe._dict(name="MPS-Y", purpose="General", lines=[{"party_type": "Supplier"}])
    assert salary.schedule_has_permission(supplier_schedule, "read") is True
    assert salary.b2b_has_permission(fake_frappe._dict(employee="EMP-1"), "read") is sees
    assert salary.b2b_has_permission(fake_frappe._dict(employee=None, supplier="ACME"), "read") is True


def test_a_role_given_read_on_salary_slips_sees_salaries_the_employees_own_does_not(env, monkeypatch):
    permissions = types.ModuleType("frappe.permissions")
    permissions.get_valid_perms = lambda doctype, user: [
        {"role": "Employee", "read": 1, "permlevel": 0, "if_owner": 0},
        {"role": "Payroll Clerk", "read": 1, "permlevel": 0, "if_owner": 0},
    ] if "Payroll Clerk" in env.frappe.user_roles else [{"role": "Employee", "read": 1}]
    monkeypatch.setitem(sys.modules, "frappe.permissions", permissions)
    env.frappe.session.user, env.frappe.user_roles = "clerk@example.com", {"Employee"}
    assert access(env).can_see_salaries() is False
    env.frappe.user_roles = {"Employee", "Payroll Clerk"}
    assert access(env).can_see_salaries() is True


def test_list_conditions_hide_salary_schedules_and_employee_payments(env):
    schedule_row(env)  # the payroll schedule
    schedule_row(env, "MPS-2", purpose="General", payroll_doctype=None, payroll_entry=None)
    bank_line(env, "L1", parent="MPS-2")  # a salary added by hand to a general schedule
    schedule_row(env, "MPS-3", purpose="General", payroll_doctype=None, payroll_entry=None)
    bank_line(env, "L2", parent="MPS-3", party_type="Supplier", party="ACME", reference_doctype=None,
              reference_name=None)
    env.request("MB2B-1")
    env.request("MB2B-2", employee="EMP-1", supplier=None)
    env.frappe.session.user, env.frappe.user_roles = "viewer@example.com", {"M-Pesa Viewer"}
    salary = access(env)

    def visible(doctype, condition):
        return [r[0] for r in env.frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE {condition} ORDER BY name")]

    assert visible(SCHEDULE, salary.schedule_query_conditions("viewer@example.com")) == ["MPS-3"]
    assert visible(B2B, salary.b2b_query_conditions("viewer@example.com")) == ["MB2B-1"]
    env.frappe.user_roles = {"M-Pesa Payer"}
    assert salary.schedule_query_conditions(PAYER) == "" and salary.b2b_query_conditions(PAYER) == ""


def test_the_due_report_shows_salary_lines_only_to_who_may_see_them(env):
    report = importlib.import_module("mpsa_suite.mpsa_suite.report.m_pesa_payments_due.m_pesa_payments_due")
    schedule_row(env)
    bank_line(env, "L1")
    bank_line(env, "L3", party_type="Supplier", party="ACME", payee_name="Acme Fuel Ltd",
              payee_account_doctype="Supplier M-Pesa Account", payee_account="MSA-1", destination="888880 / ACME-1",
              reference_doctype=None, reference_name=None, amount=3000)
    env.frappe.user_roles = {"M-Pesa Viewer"}
    assert [d["line"] for d in report.execute({"company": "Astral"})[1]] == ["L3"]
    env.frappe.user_roles = {"M-Pesa Payer"}
    assert [d["line"] for d in report.execute({"company": "Astral"})[1]] == ["L1", "L3"]


# ---------------------------------------------------------------- a Salary Slip with M-Pesa payments
def test_a_salary_slip_paid_by_m_pesa_cannot_be_cancelled(env):
    slip = fake_frappe._dict(name="SAL-2")
    env.payroll.slip_before_cancel(slip)  # nothing paid
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 2, "status": "Paid",
                                         "reference_doctype": SLIP, "reference_name": "SAL-2"}
    # Cancel All took the Paid payout with its disbursement: the money still left
    with pytest.raises(env.frappe.ValidationError, match="SAL-2 cannot be cancelled while M-Pesa payments of it.*B2C-1"):
        env.payroll.slip_before_cancel(slip)
    env.frappe.db.table(REQ)["B2C-1"]["status"] = "Failed"
    env.request("MB2B-1", status="Initiated", amount=30000, salary_slip="SAL-2", employee="EMP-2", supplier=None)
    with pytest.raises(env.frappe.ValidationError, match="MB2B-1"):
        env.payroll.slip_before_cancel(slip)
