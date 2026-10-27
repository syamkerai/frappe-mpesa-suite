"""Employees and payroll on the M-Pesa Payment Schedule (money_out.schedule, payroll), on the
in-memory site of b2b_fixture (SQL on SQLite), with the real B2B request and B2C disbursement
controllers behind it.

Covers: employee lines (bank paybill by B2B, phone by B2C) and their checks; a Salary Slip line;
B2B salaries through the B2B request path (employee and slip on the request); phone lines in one
B2C Payment Disbursement per group, rows carrying their line and the approved phone, the number
locked; the date rule and maker-checker on the B2C path (Pay Selected, the job that makes the
payout, the send); partial failures; the line following its B2C payout; Plan Again; the lost-job
sweep; the Payroll Entry button (one schedule, exclusions with reasons, at most one live, no
bank entry); the Payroll Entry cancel guard; B2C's guard seeing B2B salaries; the Utility card;
a site without HRMS.
"""

import datetime
import importlib
import itertools
import sys
import types

import pytest

import b2b_fixture
import fake_frappe
from test_schedule import Cache, _b2b_doc_class, _datetime, day, line_of, prepared, run_jobs  # noqa: F401

SCHEDULE, LINE, B2B = "M-Pesa Payment Schedule", "M-Pesa Payment Schedule Line", b2b_fixture.B2B
DISB, ROW, REQ = "B2C Payment Disbursement", "B2C Payment Disbursement Reference", "B2C Disbursement Request"
SLIP, PAYROLL, EMPACC = "Salary Slip", "Payroll Entry", "Employee M-Pesa Account"
SETTINGS = b2b_fixture.SETTINGS
TODAY, NOW = b2b_fixture.TODAY, b2b_fixture.NOW
ACCOUNTS, PAYER, OTHER = "accounts@example.com", "payer@example.com", "manager@example.com"
PHONE = "254722000111"


class Row:
    """A B2C Payment Disbursement Reference with its real validate."""

    def __init__(self, cls, **kw):
        object.__setattr__(self, "_cls", cls)
        self.__dict__.update(kw)

    def get(self, key, default=None):
        return self.__dict__.get(key, default)

    def __getattr__(self, key):
        if key.startswith("__"):
            raise AttributeError(key)
        method = getattr(self._cls, key, None)
        if callable(method):
            return types.MethodType(method, self)
        return None


def _b2c_doc_class(env, controller, row_controller):
    seq = itertools.count(1)

    class B2CDoc(controller):
        """A B2C Payment Disbursement with the real controller: insert runs validate and
        before_save, submit runs before_submit."""

        def __init__(self):
            self.__dict__.update(doctype=DISB, references=[], flags=fake_frappe._dict(), name=None, docstatus=0,
                                 company_currency="KES")

        def __getattr__(self, key):
            if key.startswith("__"):
                raise AttributeError(key)
            return None

        def update(self, values):
            self.__dict__.update(values)

        def get(self, key, default=None):
            return self.__dict__.get(key, default)

        def append(self, table, row):
            r = Row(row_controller, doctype=ROW, idx=len(self.references) + 1, b2c_disbursement_request=None,
                    payment_status="Not Initiated", **row)
            self.references.append(r)
            return r

        def _store(self):
            db = env.frappe.db
            db.table(DISB)[self.name] = {"name": self.name, "docstatus": self.docstatus,
                                         "payment_schedule": self.payment_schedule, "company": self.company,
                                         "mode_of_payment": self.mode_of_payment, "paid_from": self.paid_from,
                                         "paid_to": self.paid_to}
            for r in self.references:
                db.table(ROW)[r.name] = {k: v for k, v in r.__dict__.items() if not k.startswith("_")}

        def insert(self, ignore_permissions=False):
            self.name = f"MPESA-B2C-{next(seq)}"
            for r in self.references:
                r.name, r.parent, r.parenttype = f"{self.name}-R{r.idx}", self.name, DISB
            self.validate()
            self.before_save()
            self._store()
            env.inserted.append((DISB, env.frappe.session.user, dict(self.flags)))
            return self

        def submit(self):
            self.docstatus = 1
            self.before_submit()
            self._store()

    return B2CDoc


class ScheduleDoc(fake_frappe._dict):
    """frappe.new_doc for the schedule: insert runs validate_schedule and stores it."""

    def is_new(self):
        return not self.get("_saved")

    def append(self, table, row):
        line = RowDict(row, idx=len(self.setdefault("lines", [])) + 1, name=f"PL{len(self.get('lines', [])) + 1}")
        self["lines"].append(line)
        return line

    def insert(self):
        self.name = self.name or "MPS-PAY"
        self.env.schedule.validate_schedule(self)
        db = self.env.frappe.db
        db.table(SCHEDULE)[self.name] = {k: v for k, v in self.items() if k not in ("lines", "env", "flags")}
        db.table(SCHEDULE)[self.name].update(modified=NOW)
        for line in self.lines:
            db.table(LINE)[line.name] = dict(line, parent=self.name, parenttype=SCHEDULE, modified=NOW)
        self._saved = True
        return self


class RowDict(fake_frappe._dict):
    def set(self, key, value):
        self[key] = value


@pytest.fixture
def env(monkeypatch):
    with b2b_fixture.site(monkeypatch) as env:
        frappe = env.frappe
        frappe.cache = Cache()
        hashes = itertools.count(1)
        frappe.generate_hash = lambda *a, length=10, **kw: f"h{next(hashes):0{length - 1}d}"[:length]
        frappe.get_roles = lambda user=None: sorted(frappe.user_roles)
        frappe.local.message_log = []
        frappe.get_installed_apps = lambda: ["frappe", "erpnext", "hrms"]
        frappe.db.columns = frappe.db.columns | {(B2B, "salary_slip")}
        setup = types.ModuleType("erpnext.setup")
        setup_utils = types.ModuleType("erpnext.setup.utils")
        setup_utils.get_exchange_rate = lambda *a, **kw: 1
        monkeypatch.setitem(sys.modules, "erpnext.setup", setup)
        monkeypatch.setitem(sys.modules, "erpnext.setup.utils", setup_utils)
        env.schedule = importlib.import_module("mpsa_suite.money_out.schedule")
        env.payroll = importlib.import_module("mpsa_suite.money_out.payroll")
        env.balances = importlib.import_module("mpsa_suite.wallet.balances")
        env.pa = importlib.import_module("mpsa_suite.money_out.party_accounts")
        monkeypatch.setattr(env.pa, "alert", lambda message, doc, **kw: env.alerts.append((doc.name, message)))
        for module in (env.schedule, env.balances):
            monkeypatch.setattr(module, "get_datetime", _datetime)
        controller = importlib.import_module(
            "mpsa_suite.mpsa_suite.doctype.mpesa_b2b_payment_request.mpesa_b2b_payment_request")
        env.disb = importlib.import_module(
            "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement")
        row_module = importlib.import_module(
            "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement_reference.b2c_payment_disbursement_reference")
        monkeypatch.setattr(env.disb.B2CPaymentDisbursement, "_get_mpesa_settings",
                            lambda self: fake_frappe._dict(name=SETTINGS))
        B2BDoc = _b2b_doc_class(env, controller.MpesaB2BPaymentRequest)
        B2CDoc = _b2c_doc_class(env, env.disb.B2CPaymentDisbursement, row_module.B2CPaymentDisbursementReference)
        env.B2BDoc = B2BDoc
        plain_new_doc, plain_get_doc = frappe.new_doc, frappe.get_doc

        def new_doc(doctype):
            if doctype == B2B:
                return B2BDoc({})
            if doctype == DISB:
                return B2CDoc()
            if doctype == SCHEDULE:
                return ScheduleDoc(env=env, flags=fake_frappe._dict(), lines=[], doctype=SCHEDULE)
            return plain_new_doc(doctype)

        def get_doc(doctype, name=None, **kw):
            if doctype == B2B and isinstance(name, str):
                row = frappe.db.table(B2B).get(name)
                if row is None:
                    raise frappe.DoesNotExistError(name)
                return B2BDoc(row)
            return plain_get_doc(doctype, name, **kw)

        frappe.new_doc, frappe.get_doc = new_doc, get_doc
        env.clock = [1000.0]
        monkeypatch.setattr(env.schedule, "_clock", lambda: env.clock[0])
        monkeypatch.setattr(env.schedule.time, "sleep", lambda seconds: None)
        env.posted = []

        def post(settings, endpoint, payload, **kw):
            env.posted.append((kw["name"], payload))
            return {"OriginatorConversationID": f"OC_{kw['name']}", "ConversationID": f"AG_{kw['name']}",
                    "ResponseCode": "0", "ResponseDescription": "Accepted"}, None

        monkeypatch.setattr(env.b2b, "post", post)
        db = frappe.db
        db.table("Mpesa Settings")[SETTINGS].update(security_credential="Q1JFRA==", till_number=None, use_b2c=1,
                                                    payment_gateway_name=SETTINGS, bank_account=b2b_fixture.BANK_ACCOUNT)
        db.table("Company")["Astral"] = {"name": "Astral", "round_off_account": "Round Off - AIL",
                                         "round_off_cost_center": "Main - AIL"}
        db.table(PAYROLL)["PAY-1"] = {"name": "PAY-1", "docstatus": 1, "company": "Astral",
                                      "payroll_payable_account": "Payroll Payable - AIL", "cost_center": "HQ - AIL",
                                      "start_date": datetime.date(2026, 10, 1), "end_date": datetime.date(2026, 10, 31)}
        db.table("Payroll Settings")["Payroll Settings"] = {"process_payroll_accounting_entry_based_on_employee": 1}
        for emp, name, status in (("EMP-1", "Jane Wanjiru", "Active"), ("EMP-2", "Otieno Ouma", "Active"),
                                  ("EMP-3", "Achieng Mary", "Active"), ("EMP-4", "Left Person", "Left")):
            db.table("Employee")[emp] = {"name": emp, "employee_name": name, "status": status, "company": "Astral"}
        active = NOW - datetime.timedelta(days=2)
        db.table(EMPACC)["MEA-1"] = {"name": "MEA-1", "docstatus": 1, "employee": "EMP-1",
                                     "account_type": "Bank Account (via Bank Paybill)", "phone": None, "shortcode": "247247",
                                     "account_number": "0123456789", "bank": "Equity", "expected_name": "Equity Bank",
                                     "active_from": active, "is_default": 1, "name_mismatch": 0}
        db.table(EMPACC)["MEA-2"] = {"name": "MEA-2", "docstatus": 1, "employee": "EMP-2", "account_type": "Phone (B2C)",
                                     "phone": PHONE, "shortcode": None, "account_number": None, "bank": None,
                                     "expected_name": "Otieno Ouma", "active_from": active, "is_default": 1,
                                     "name_mismatch": 0}
        for slip, emp, net in (("SAL-1", "EMP-1", 50000.40), ("SAL-2", "EMP-2", 30000), ("SAL-3", "EMP-3", 20000),
                               ("SAL-4", "EMP-4", 15000)):
            db.table(SLIP)[slip] = {"name": slip, "docstatus": 1, "status": "Submitted", "employee": emp,
                                    "employee_name": db.table("Employee")[emp]["employee_name"], "company": "Astral",
                                    "currency": "KES", "payroll_entry": "PAY-1", "net_pay": net,
                                    "rounded_total": round(net), "start_date": None, "end_date": None}
        frappe.session.user = PAYER
        frappe.user_roles = {"M-Pesa Payer"}
        yield env


def schedule_row(env, name="MPS-1", **kw):
    row = {"name": name, "title": "Salaries October", "company": "Astral", "b2b_settings": SETTINGS,
           "b2c_settings": SETTINGS, "purpose": "Payroll", "payroll_doctype": PAYROLL, "payroll_entry": "PAY-1",
           "closed": 0, "status": "Open", "modified": NOW, **kw}
    env.frappe.db.table(SCHEDULE)[name] = row
    return row


def bank_line(env, name="L1", **kw):
    row = {"name": name, "parent": "MPS-1", "parenttype": SCHEDULE, "idx": int(name[1:]), "modified": NOW,
           "planned_date": TODAY, "party_type": "Employee", "party": "EMP-1", "payee_name": "Jane Wanjiru",
           "payee_account_doctype": EMPACC, "payee_account": "MEA-1", "route": "B2B Paybill",
           "destination": "247247 / 0123456789", "one_off_type": None, "one_off_shortcode": None,
           "one_off_account": None, "amount": 50000, "reference_doctype": SLIP, "reference_name": "SAL-1",
           "entry_mode": "Book automatically", "remarks": "Salary October", "status": "Planned", "request": None,
           "receipt": None, "paid_on": None, "entry_status": None, "last_problem": None, "prepared_by": ACCOUNTS,
           "changed_by": ACCOUNTS, "approved_by": None, "approved_on": None, "batch": None, "attempts": 0,
           "b2c_disbursement": None, "b2c_row": None, "b2c_request": None, **kw}
    env.frappe.db.table(LINE)[name] = row
    return row


def phone_line(env, name="L2", **kw):
    values = {"party": "EMP-2", "payee_name": "Otieno Ouma", "payee_account": "MEA-2", "route": "B2C Phone",
              "destination": PHONE, "amount": 30000, "reference_name": "SAL-2", **kw}
    return bank_line(env, name, **values)


def new_form(env, lines, **kw):
    values = {"doctype": SCHEDULE, "name": "MPS-NEW", "title": "Salaries", "company": "Astral",
              "b2b_settings": SETTINGS, "b2c_settings": SETTINGS, **kw}
    doc = ScheduleDoc(env=env, flags=fake_frappe._dict(), **values)
    doc.lines = [RowDict({"idx": i, "name": f"N{i}", "planned_date": TODAY, "party_type": "Employee", **line})
                 for i, line in enumerate(lines, 1)]
    return doc


def b2c_rows(env):
    return list(env.frappe.db.table(ROW).values())


# ---------------------------------------------------------------- employee lines on the form
def test_an_employee_line_takes_its_route_from_the_default_approved_account(env):
    env.frappe.session.user = ACCOUNTS
    doc = new_form(env, [{"party": "EMP-1", "amount": 50000, "reference_doctype": SLIP, "reference_name": "SAL-1"},
                         {"party": "EMP-2", "amount": 30000, "reference_doctype": SLIP, "reference_name": "SAL-2"}])
    env.schedule.validate_schedule(doc)
    bank, phone = doc.lines
    assert (bank.route, bank.destination, bank.payee_account, bank.payee_name, bank.entry_mode) == (
        "B2B Paybill", "247247 / 0123456789", "MEA-1", "Jane Wanjiru", "Book automatically")
    assert (phone.route, phone.destination, phone.payee_account_doctype, phone.entry_mode) == (
        "B2C Phone", PHONE, EMPACC, "Book automatically")
    assert (bank.prepared_by, phone.prepared_by) == (ACCOUNTS, ACCOUNTS)


def test_an_employee_without_an_approved_account_is_refused_on_the_form(env):
    with pytest.raises(env.frappe.ValidationError, match="EMP-3 has no approved M-Pesa account. Add one under Employee"):
        env.schedule.validate_schedule(new_form(env, [{"party": "EMP-3", "amount": 100, "reference_doctype": SLIP,
                                                       "reference_name": "SAL-3"}]))


@pytest.mark.parametrize("line, msg", [
    ({"party": "EMP-1", "reference_name": "SAL-2"}, "SAL-2 belongs to EMP-2, not EMP-1"),
    ({"party": "EMP-2", "reference_doctype": None, "reference_name": None}, "a payment to a phone \\(B2C\\) pays a Salary Slip"),
    ({"party": "EMP-2", "amount": 300000}, "M-Pesa B2C pays KES 10 to 250,000"),
    ({"party": "EMP-1", "party_type": "Supplier"}, "Supplier EMP-1 not found"),
    ({"party": "EMP-1", "reference_doctype": "Purchase Invoice", "reference_name": "PINV-1"},
     "PINV-1 is paid to its supplier"),
])
def test_salary_lines_are_checked(env, line, msg):
    values = {"amount": 1000, "reference_doctype": SLIP, "reference_name": "SAL-1", **line}
    with pytest.raises(env.frappe.ValidationError, match=msg):
        env.schedule.validate_schedule(new_form(env, [values]))


def test_a_slip_outside_a_payroll_entry_and_a_missing_b2c_shortcode_are_refused(env):
    env.frappe.db.table(SLIP)["SAL-1"]["payroll_entry"] = None
    with pytest.raises(env.frappe.ValidationError, match="not part of a Payroll Entry"):
        env.schedule.validate_schedule(new_form(env, [{"party": "EMP-1", "amount": 10, "reference_doctype": SLIP,
                                                       "reference_name": "SAL-1"}]))
    with pytest.raises(env.frappe.ValidationError, match="Pick Pay From \\(B2C\\)"):
        env.schedule.validate_schedule(new_form(env, [{"party": "EMP-2", "amount": 100, "reference_doctype": SLIP,
                                                       "reference_name": "SAL-2"}], b2c_settings=None))


def test_a_withheld_slip_is_a_problem_on_the_line_and_more_than_it_owes_is_refused(env):
    env.frappe.db.table(SLIP)["SAL-1"]["status"] = "Withheld"
    doc = new_form(env, [{"party": "EMP-1", "amount": 50000, "reference_doctype": SLIP, "reference_name": "SAL-1"}])
    env.schedule.validate_schedule(doc)
    assert doc.lines[0].last_problem.startswith("SAL-1 is withheld")
    env.frappe.db.table(SLIP)["SAL-1"]["status"] = "Submitted"
    with pytest.raises(env.frappe.ValidationError, match="plans KES 50,001 for SAL-1, but KES 50,000 is left to pay"):
        env.schedule.validate_schedule(new_form(env, [{"party": "EMP-1", "amount": 50001, "reference_doctype": SLIP,
                                                       "reference_name": "SAL-1"}]))


def test_a_bank_line_with_no_slip_waits_for_make_entry(env):
    doc = new_form(env, [{"party": "EMP-1", "amount": 500, "reference_doctype": None, "reference_name": None}])
    env.schedule.validate_schedule(doc)
    assert (doc.lines[0].route, doc.lines[0].entry_mode) == ("B2B Paybill", "Make the entry later")


def test_salary_slips_need_hrms(env):
    env.frappe.get_installed_apps = lambda: ["frappe", "erpnext"]
    with pytest.raises(env.frappe.ValidationError, match="paid when HRMS is installed"):
        env.schedule.validate_schedule(new_form(env, [{"party": "EMP-1", "amount": 10, "reference_doctype": SLIP,
                                                       "reference_name": "SAL-1"}]))


# ---------------------------------------------------------------- the B2B route for a bank paybill
def test_a_bank_salary_is_paid_by_a_b2b_request_to_the_employee(env):
    schedule_row(env)
    bank_line(env)
    answer = env.schedule.confirm_payment(prepared(env, "L1")["token"])
    request = env.frappe.db.table(B2B)[answer["sent"][0]["request"]]
    assert (request["employee"], request["employee_mpesa_account"], request["salary_slip"], request.get("supplier"),
            request["receiver_shortcode"], request["account_reference"], request["payroll_entry"]) == (
        "EMP-1", "MEA-1", "SAL-1", None, "247247", "0123456789", "PAY-1")
    assert "b2c-pay:Salary Slip:SAL-1" in env.locks
    run_jobs(env, "schedule.send_batch")
    name = line_of(env, "L1")["request"]
    assert env.posted[0][1]["PartyB"] == "247247" and env.posted[0][1]["AccountReference"] == "0123456789"
    result = {"ResultType": 0, "ResultCode": 0, "ResultDesc": "ok", "ConversationID": f"AG_{name}",
              "OriginatorConversationID": f"OC_{name}", "TransactionID": "QSAL1",
              "ResultParameters": {"ResultParameter": [
                  {"Key": "TransCompletedTime", "Value": "20261018101500"},
                  {"Key": "ReceiverPartyPublicName", "Value": "247247 - Equity Bank Limited"}]}}
    env.b2b.handle_result(result, name)
    run_jobs(env, "b2b.book_payment_entry")
    run_jobs(env, "schedule.sync_line")
    line = line_of(env, "L1")
    assert (line["status"], line["receipt"], line["entry_status"]) == ("Paid", "QSAL1", "Entry Made")
    assert env.frappe.db.table(EMPACC)["MEA-1"]["name_mismatch"] == 0


def test_a_bank_salary_is_not_sent_before_its_day(env):
    schedule_row(env)
    bank_line(env, planned_date=day(1))
    assert env.schedule.prepare_payment(["L1"])["not_included"][0]["reason"] == "Not due until 19 Oct 2026."


# ---------------------------------------------------------------- the B2C route for a phone
def test_phone_lines_are_paid_by_one_b2c_disbursement_with_the_approved_phone(env):
    env.frappe.db.table(SLIP)["SAL-3"]["employee"] = "EMP-2"  # a second slip of the phone employee
    schedule_row(env)
    phone_line(env, "L2")
    phone_line(env, "L3", amount=20000, reference_name="SAL-3")
    bank_line(env, "L1")
    summary = env.schedule.prepare_payment(["L1", "L2", "L3"])
    assert {(r["pay_from"], r["route"]) for r in summary["routes"]} == {(SETTINGS, "B2B Paybill"), (SETTINGS, "B2C Phone")}
    assert {b["sub_account"] for b in summary["balances"]} == {"Working", "Utility"}
    env.clock[0] += 6
    answer = env.schedule.confirm_payment(summary["token"])
    assert len(answer["sent"]) == 3 and answer["refused"] == []
    (disbursement,) = env.frappe.db.table(DISB).values()
    assert (disbursement["docstatus"], disbursement["payment_schedule"], disbursement["paid_to"]) == (
        1, "MPS-1", "Payroll Payable - AIL")
    rows = sorted(b2c_rows(env), key=lambda r: r["idx"])
    assert [(r["mpesa_schedule_line"], r["partyb"], r["allocated_amount"], r["payment_status"]) for r in rows] == [
        ("L2", PHONE, 30000, "Initiated"), ("L3", PHONE, 20000, "Initiated")]
    for name in ("L2", "L3"):
        line = line_of(env, name)
        assert (line["status"], line["b2c_disbursement"], line["approved_by"]) == ("Sending", disbursement["name"],
                                                                                   PAYER)
    assert len([j for j in env.frappe.enqueued if j[0].endswith("create_b2c_request")]) == 2
    (send,) = [j for j in env.frappe.enqueued if j[0].endswith("schedule.send_batch")]
    assert send[1]["requests"] == [line_of(env, "L1")["request"]]  # only the B2B request goes to send_batch


def test_maker_checker_and_the_date_rule_hold_for_phone_lines(env):
    schedule_row(env)
    phone_line(env, "L2", prepared_by=PAYER)
    phone_line(env, "L3", planned_date=day(1), reference_name="SAL-3", party="EMP-2")
    env.frappe.db.table(SLIP)["SAL-3"]["employee"] = "EMP-2"
    result = env.schedule.prepare_payment(["L2", "L3"])
    assert [n["reason"] for n in result["not_included"]] == [
        "You prepared or last changed this line: another person must pay it.", "Not due until 19 Oct 2026."]
    assert result["token"] is None and not env.frappe.db.table(DISB)


def test_one_refused_phone_line_leaves_the_others_paid(env):
    env.frappe.db.table(SLIP)["SAL-3"]["employee"] = "EMP-2"
    schedule_row(env)
    phone_line(env, "L2")
    phone_line(env, "L3", amount=20000, reference_name="SAL-3")
    token = prepared(env, "L2", "L3")["token"]
    line_of(env, "L3")["changed_by"] = PAYER  # changed by the payer after the summary
    answer = env.schedule.confirm_payment(token)
    assert [s["line"] for s in answer["sent"]] == ["L2"]
    assert answer["refused"][0]["line"] == "L3" and line_of(env, "L3")["status"] == "Planned"
    assert [r["mpesa_schedule_line"] for r in b2c_rows(env)] == ["L2"]


def test_a_failing_group_refuses_its_lines_and_keeps_the_b2b_ones(env):
    schedule_row(env)
    bank_line(env, "L1")
    phone_line(env, "L2")
    token = prepared(env, "L1", "L2")["token"]
    env.frappe.db.table(PAYROLL)["PAY-1"]["payroll_payable_account"] = None  # its disbursement cannot be made
    answer = env.schedule.confirm_payment(token)
    assert [s["line"] for s in answer["sent"]] == ["L1"]
    assert answer["refused"][0]["line"] == "L2"
    assert answer["refused"][0]["reason"] == "Payroll Entry PAY-1 has no Payroll Payable Account."
    line = line_of(env, "L2")
    assert (line["status"], line["b2c_disbursement"], line["last_problem"]) == (
        "Planned", None, "Payroll Entry PAY-1 has no Payroll Payable Account.")
    assert not env.frappe.db.table(DISB)


def test_a_b2c_payout_made_by_hand_meanwhile_counts_against_the_slip(env):
    schedule_row(env)
    phone_line(env, "L2")
    env.b2c.append({"dt": SLIP, "dn": "SAL-2", "parent": "MPESA-B2C-OLD", "allocated_amount": 30000,
                    "request_status": "Initiated"})
    reason = env.schedule.prepare_payment(["L2"])["not_included"][0]["reason"]
    assert "KES 30,000 is more than is left to pay" in reason and "B2C MPESA-B2C-OLD" in reason


def test_the_b2c_row_refuses_another_number_and_rows_made_by_hand(env):
    schedule_row(env)
    phone_line(env, "L2")
    row_module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement_reference.b2c_payment_disbursement_reference")
    row = Row(row_module.B2CPaymentDisbursementReference, idx=1, party="EMP-2", reference_doctype=SLIP,
              reference_name="SAL-2", allocated_amount=30000, outstanding_amount=30000, partyb="0733000000",
              mpesa_schedule_line="L2")
    with pytest.raises(env.frappe.ValidationError, match="the number must be the employee's approved phone"):
        row.validate()
    row.partyb = "0722000111"
    row.validate()
    assert row.partyb == PHONE
    row.allocated_amount = 29000
    with pytest.raises(env.frappe.ValidationError, match="the amount must be the schedule line's"):
        row.validate()
    doc = fake_frappe._dict(references=[row], payment_schedule=None, flags=fake_frappe._dict())
    with pytest.raises(env.frappe.ValidationError, match="paid with Pay Selected"):
        env.schedule.check_b2c_disbursement(doc)


def confirmed_phone(env, **kw):
    schedule_row(env)
    phone_line(env, "L2", **kw)
    env.schedule.confirm_payment(prepared(env, "L2")["token"])
    (job,) = [j for j in env.frappe.enqueued if j[0].endswith("create_b2c_request")]
    return job[1]


def test_the_job_that_makes_the_payout_refuses_a_line_not_due_or_an_account_blocked(env):
    job = confirmed_phone(env)
    env.frappe.db.table(LINE)["L2"]["planned_date"] = day(1)  # moved under it (by the database)
    env.disb.create_b2c_request(job["ref"], job["settings"], job["b2c_disbursement"])
    row = env.frappe.db.table(ROW)[job["ref"].name]
    assert row["payment_status"] == "Failed" and not env.frappe.db.table(REQ)
    line = line_of(env, "L2")
    assert line["status"] == "Failed" and line["last_problem"].startswith("Not sent: Not due until 19 Oct 2026")


def test_the_job_refuses_a_blocked_phone_account(env):
    job = confirmed_phone(env)
    env.frappe.db.table(EMPACC)["MEA-2"].update(name_mismatch=1, last_receiver_name="254722000111 - SOMEONE")
    env.disb.create_b2c_request(job["ref"], job["settings"], job["b2c_disbursement"])
    assert "blocked" in line_of(env, "L2")["last_problem"]


def test_the_job_makes_the_payout_as_the_payer_and_the_send_checks_the_date_again(env, monkeypatch):
    job = confirmed_phone(env)
    made = []

    class Request(fake_frappe.FakeDoc):
        def submit(self):
            made.append((self._row.get("remarks"), self.flags.get("ignore_permissions")))
            self._row["docstatus"] = 1

    plain = env.frappe.get_doc

    def get_doc(doctype, name=None, **kw):
        if isinstance(doctype, dict) and doctype.get("doctype") == REQ:
            row = dict(doctype)
            doc = Request(env.frappe, row.pop("doctype"), row)
            object.__setattr__(doc, "flags", fake_frappe._dict())
            return doc
        return plain(doctype, name, **kw)

    monkeypatch.setattr(env.frappe, "get_doc", get_doc)
    env.disb.create_b2c_request(job["ref"], job["settings"], job["b2c_disbursement"])
    assert made == [("Salary October", True)]
    (request,) = env.frappe.db.table(REQ).values()
    assert (request["phone_number"], request["amount"], request["party"]) == (PHONE, 30000, "EMP-2")
    assert env.frappe.db.table(ROW)[job["ref"].name]["b2c_disbursement_request"] == request["name"]
    # the send itself: refused before the planned day, or to any other phone
    request.update(b2c_payment_disbursement=job["b2c_disbursement"].name,
                   b2c_payment_disbursement_reference=job["ref"].name)
    env.schedule.assert_b2c_line_due(fake_frappe._dict(request))
    env.frappe.db.table(LINE)["L2"]["planned_date"] = day(1)
    with pytest.raises(env.frappe.ValidationError, match="Not due until 19 Oct 2026"):
        env.schedule.assert_b2c_line_due(fake_frappe._dict(request))
    env.frappe.db.table(LINE)["L2"]["planned_date"] = TODAY
    with pytest.raises(env.frappe.ValidationError, match="does not match"):
        env.schedule.assert_b2c_line_due(fake_frappe._dict(request, phone_number="254733000000"))


@pytest.mark.parametrize("request_state, row_status, line_status, entry", [
    ({"status": "Not Initiated", "docstatus": 1}, "Initiated", "Sending", None),
    ({"status": "Initiated", "docstatus": 1}, "Initiated", "Sent", None),
    ({"status": "Paid", "docstatus": 1, "accounting_booked": 1, "transaction_id": "QB2C1"}, "Paid", "Paid", "Entry Made"),
    ({"status": "Paid", "docstatus": 1, "accounting_booked": 0, "accounting_error": "closed period",
      "transaction_id": "QB2C1"}, "Paid", "Paid", "Entry Not Made"),
    ({"status": "Failed", "docstatus": 1, "error_message": "Initiator invalid"}, "Failed", "Failed", None),
    ({"status": "Status Unknown", "docstatus": 1}, "Initiated", "Unknown", None),
    (None, "Failed", "Failed", None),
])
def test_a_phone_line_follows_its_b2c_payout(env, request_state, row_status, line_status, entry):
    schedule_row(env)
    phone_line(env, "L2", status="Sending", b2c_disbursement="MPESA-B2C-1", b2c_row="ROW-1")
    env.frappe.db.table(ROW)["ROW-1"] = {"name": "ROW-1", "parent": "MPESA-B2C-1", "payment_status": row_status,
                                         "b2c_disbursement_request": "B2C-1" if request_state else None,
                                         "mpesa_schedule_line": "L2"}
    if request_state:
        env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "transaction_completed_datetime": NOW,
                                             "b2c_payment_disbursement_reference": "ROW-1", **request_state}
    env.schedule.sync_b2c_line("ROW-1")
    line = line_of(env, "L2")
    assert (line["status"], line["entry_status"]) == (line_status, entry)
    if line_status == "Paid":
        assert line["receipt"] == "QB2C1" and line["b2c_request"] == "B2C-1"


def test_plan_again_for_a_failed_phone_line_and_not_for_an_unknown_or_paid_one(env):
    schedule_row(env)
    phone_line(env, "L2", status="Unknown", b2c_disbursement="MPESA-B2C-1", b2c_row="ROW-1", b2c_request="B2C-1")
    env.frappe.db.table(ROW)["ROW-1"] = {"name": "ROW-1", "payment_status": "Initiated",
                                         "b2c_disbursement_request": "B2C-1"}
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "status": "Status Unknown", "docstatus": 1}
    env.frappe.session.user, env.frappe.user_roles = ACCOUNTS, {"Accounts User"}
    with pytest.raises(env.frappe.ValidationError, match="A Transaction Status check settles it"):
        env.schedule.plan_again("L2")
    line_of(env, "L2")["status"] = "Failed"
    env.frappe.db.table(REQ)["B2C-1"]["status"] = "Paid"
    with pytest.raises(env.frappe.ValidationError, match="B2C-1 is Paid"):
        env.schedule.plan_again("L2")
    env.frappe.db.table(REQ)["B2C-1"]["status"] = "Failed"
    env.schedule.plan_again("L2")
    line = line_of(env, "L2")
    assert (line["status"], line["b2c_disbursement"], line["b2c_row"], line["b2c_request"], line["changed_by"]) == (
        "Planned", None, None, None, ACCOUNTS)


def test_a_phone_line_whose_payout_was_never_made_is_failed_after_an_hour(env):
    schedule_row(env)
    phone_line(env, "L2", status="Sending", b2c_disbursement="MPESA-B2C-1", b2c_row="ROW-1",
               approved_on=NOW - datetime.timedelta(minutes=61))
    phone_line(env, "L3", status="Sending", b2c_disbursement="MPESA-B2C-1", b2c_row="ROW-2",
               approved_on=NOW - datetime.timedelta(minutes=5), reference_name="SAL-3")
    for row, line in (("ROW-1", "L2"), ("ROW-2", "L3")):
        env.frappe.db.table(ROW)[row] = {"name": row, "parent": "MPESA-B2C-1", "idx": int(row[-1]),
                                         "payment_status": "Initiated", "b2c_disbursement_request": None,
                                         "mpesa_schedule_line": line}
    env.schedule.sweep()
    assert env.frappe.db.table(ROW)["ROW-1"]["payment_status"] == "Failed"
    assert line_of(env, "L2")["status"] == "Failed" and "nothing was sent" in line_of(env, "L2")["last_problem"]
    assert line_of(env, "L3")["status"] == "Sending"  # its job may still run


def test_close_and_delete_know_the_b2c_lines(env):
    schedule_row(env)
    phone_line(env, "L2", status="Sending", b2c_disbursement="MPESA-B2C-1", b2c_row="ROW-1")
    bank_line(env, "L1")
    env.frappe.user_roles = {"Accounts User"}
    assert env.schedule.close_schedule("MPS-1") == {"cancelled": 1}
    assert line_of(env, "L2")["status"] == "Sending"
    with pytest.raises(env.frappe.ValidationError, match="cannot be deleted"):
        env.schedule.check_delete(fake_frappe._dict(name="MPS-1"))


def test_the_retry_of_a_scheduled_b2c_payout_is_refused(env):
    req_module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request")
    env.frappe.db.table(ROW)["ROW-1"] = {"name": "ROW-1", "mpesa_schedule_line": "L2"}
    request = object.__new__(req_module.B2CDisbursementRequest)
    request.__dict__.update(name="B2C-1", b2c_payment_disbursement_reference="ROW-1", reference_doctype=SLIP,
                            reference_name="SAL-2", docstatus=1, status="Failed", flags=fake_frappe._dict())
    request.reload = lambda: None
    env.frappe.user_roles = {"M-Pesa Manager"}
    with pytest.raises(env.frappe.ValidationError, match="plan its line again"):
        request.retry_failed_payment()


# ---------------------------------------------------------------- B2C's own guard sees B2B salaries
def test_b2cs_guard_refuses_a_slip_with_a_b2b_salary_on_its_way(env):
    env.request("MB2B-9", status="Initiated", amount=30000, salary_slip="SAL-2", employee="EMP-2", supplier=None,
                transaction_id=None)
    disbursement = object.__new__(env.disb.B2CPaymentDisbursement)
    row = fake_frappe._dict(reference_doctype=SLIP, reference_name="SAL-2", allocated_amount=30000,
                            b2c_disbursement_request=None)
    disbursement.__dict__.update(name="MPESA-B2C-9", references=[row])
    with pytest.raises(env.frappe.ValidationError, match="already has a payout in progress .*MB2B-9"):
        disbursement.validate_no_double_payout()


def test_a_b2b_salary_counts_b2c_payouts_on_their_way_not_paid_ones(env):
    env.b2c.append({"dt": SLIP, "dn": "SAL-1", "parent": "MPESA-B2C-1", "allocated_amount": 20000,
                    "request_status": "Initiated"})
    env.b2c.append({"dt": SLIP, "dn": "SAL-1", "parent": "MPESA-B2C-0", "allocated_amount": 5000,
                    "request_status": "Paid"})  # in what the slip owes already
    result = env.owed.slip_left_to_pay("SAL-1")
    assert (result.owed, [p.name for p in result.parts], result.left) == (50000, ["MPESA-B2C-1"], 30000)


# ---------------------------------------------------------------- the Payroll Entry's button
def test_one_schedule_per_payroll_with_the_excluded_and_their_reasons(env):
    env.frappe.session.user, env.frappe.user_roles = ACCOUNTS, {"Accounts User", "HR User"}  # sees salaries
    env.frappe.db.table(SLIP)["SAL-5"] = dict(env.frappe.db.table(SLIP)["SAL-1"], name="SAL-5", employee="EMP-5",
                                              employee_name="Held", status="Withheld")
    env.frappe.db.table("Employee")["EMP-5"] = {"name": "EMP-5", "employee_name": "Held", "status": "Active",
                                                "company": "Astral"}
    answer = env.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS, b2c_settings=SETTINGS,
                                            planned_date=day(2).isoformat())
    assert (answer["lines"], answer["total"]) == (2, 80000)
    schedule = env.frappe.db.table(SCHEDULE)[answer["name"]]
    assert (schedule["purpose"], schedule["payroll_entry"], schedule["payroll_doctype"]) == ("Payroll", "PAY-1", PAYROLL)
    lines = sorted((r for r in env.frappe.db.table(LINE).values() if r["parent"] == answer["name"]),
                   key=lambda r: r["idx"])
    assert [(r["party"], r["route"], r["amount"], r["reference_name"], r["planned_date"], r["prepared_by"]) for r in
            lines] == [("EMP-1", "B2B Paybill", 50000, "SAL-1", day(2), ACCOUNTS),
                       ("EMP-2", "B2C Phone", 30000, "SAL-2", day(2), ACCOUNTS)]
    excluded = schedule["excluded"]
    assert "EMP-3 Achieng Mary (SAL-3): no approved M-Pesa account" in excluded
    assert "EMP-4 Left Person (SAL-4): the employee is Left" in excluded
    assert "EMP-5 Held (SAL-5): the salary is withheld" in excluded
    with pytest.raises(env.frappe.ValidationError, match=f"PAY-1 already has M-Pesa Payment Schedule {answer['name']}"):
        env.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS, b2c_settings=SETTINGS)


def test_a_payroll_paid_partly_by_bank_or_not_submitted_is_refused(env):
    env.frappe.user_roles = {"Accounts User", "HR User"}
    env.je("ACC-JV-1", "CHQ-1", [{"account": "Payroll Payable - AIL", "debit_in_account_currency": 100,
                                  "reference_type": PAYROLL, "reference_name": "PAY-1"}], voucher_type="Bank Entry")
    with pytest.raises(env.frappe.ValidationError, match="already has a bank or cash entry not made by M-Pesa \\(ACC-JV-1\\)"):
        env.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS, b2c_settings=SETTINGS)
    env.frappe.db.table(JEA_TABLE)["ACC-JV-1-1"]["mpesa_b2b_payment_request"] = "MB2B-1"  # an M-Pesa salary entry
    assert env.payroll.bank_entries("PAY-1") == []
    env.frappe.db.table(PAYROLL)["PAY-1"]["docstatus"] = 0
    with pytest.raises(env.frappe.ValidationError, match="Submit PAY-1 first"):
        env.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS)
    env.frappe.user_roles = {"M-Pesa Payer"}
    with pytest.raises(env.frappe.PermissionError):
        env.payroll.from_payroll_entry("PAY-1")
    env.frappe.user_roles = {"System Manager"}  # configures, does not prepare (decision 1)
    with pytest.raises(env.frappe.PermissionError):
        env.payroll.from_payroll_entry("PAY-1")


def test_amounts_are_what_is_left_in_whole_shillings_and_a_missing_shortcode_excludes(env):
    env.frappe.user_roles = {"Accounts User", "HR User"}
    env.request("MB2B-0", status="Paid", amount=20000, salary_slip="SAL-1", employee="EMP-1", supplier=None)
    answer = env.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS)  # no B2C shortcode
    lines = [r for r in env.frappe.db.table(LINE).values() if r["parent"] == answer["name"]]
    assert [(r["party"], r["amount"]) for r in lines] == [("EMP-1", 30000)]
    assert any("EMP-2" in x and "pick Pay From (B2C)" in x for x in answer["excluded"])


def test_a_manual_schedule_cannot_claim_a_payroll(env):
    doc = new_form(env, [{"party": "EMP-1", "amount": 100, "reference_doctype": SLIP, "reference_name": "SAL-1"}],
                   purpose="Payroll", payroll_doctype=PAYROLL, payroll_entry="PAY-1", excluded="x")
    env.schedule.validate_schedule(doc)
    assert (doc.purpose, doc.payroll_entry, doc.excluded) == ("General", None, None)


# ---------------------------------------------------------------- the Payroll Entry's cancel
def test_a_payroll_with_mpesa_salaries_paid_or_on_their_way_cannot_be_cancelled(env):
    pe = fake_frappe._dict(name="PAY-1")
    env.payroll.before_cancel(pe)  # nothing paid, nothing planned
    env.request("MB2B-1", status="Paid", amount=50000, salary_slip="SAL-1", employee="EMP-1", supplier=None)
    with pytest.raises(env.frappe.ValidationError, match="paid or on their way: MB2B-1"):
        env.payroll.before_cancel(pe)
    env.frappe.db.table(B2B)["MB2B-1"].update(status="Failed")
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 1, "status": "Initiated",
                                         "reference_doctype": SLIP, "reference_name": "SAL-2"}
    with pytest.raises(env.frappe.ValidationError, match="B2C-1"):
        env.payroll.before_cancel(pe)
    env.frappe.db.table(REQ)["B2C-1"]["status"] = "Failed"
    schedule_row(env)
    bank_line(env)
    with pytest.raises(env.frappe.ValidationError, match="has M-Pesa Payment Schedule MPS-1.*delete it first"):
        env.payroll.before_cancel(pe)
    env.frappe.get_installed_apps = lambda: ["frappe", "erpnext"]
    env.payroll.before_cancel(pe)  # a site without HRMS: never refused here


# ---------------------------------------------------------------- the Payments Due report
def test_the_due_report_shows_a_utility_card_for_phone_lines(env):
    report = importlib.import_module("mpsa_suite.mpsa_suite.report.m_pesa_payments_due.m_pesa_payments_due")
    schedule_row(env)
    bank_line(env, "L1")
    phone_line(env, "L2")
    env.frappe.db.table(REQ)["B2C-0"] = {"name": "B2C-0", "mpesa_settings": SETTINGS, "utility_acct_avlbl_funds": 75000,
                                         "transaction_completed_datetime": NOW - datetime.timedelta(minutes=30),
                                         "docstatus": 1, "status": "Paid", "amount": 10}
    _, data, _, _, cards = report.execute({"company": "Astral"})
    assert [(d["line"], d["pay_from"], d["route"]) for d in data] == [("L1", SETTINGS, "B2B Paybill"),
                                                                      ("L2", SETTINGS, "B2C Phone")]
    utility = next(c for c in cards if c["label"].startswith("Utility"))
    assert utility["value"] == 75000
    _, only_phone, *_ = report.execute({"company": "Astral", "route": "B2C Phone", "pay_from": SETTINGS})
    assert [d["line"] for d in only_phone] == ["L2"]


# ---------------------------------------------------------------- a site without HRMS
def _migrate(monkeypatch):
    custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    custom_field.create_custom_fields = lambda fields, update=False: None
    for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, custom_field.__name__, custom_field)
    return importlib.import_module("mpsa_suite.mpsa_suite.migrate")


def test_without_hrms_no_salary_columns_are_read_and_no_hrms_fields_are_made(env, monkeypatch):
    env.frappe.db.columns = env.frappe.db.columns - {(B2B, "salary_slip")}
    assert env.owed.b2b_slip_open("SAL-1") == []
    migrate = _migrate(monkeypatch)
    install = importlib.import_module("mpsa_suite.install")
    for name in ("ensure_roles", "ensure_suite_settings", "ensure_lookup_indexes"):
        monkeypatch.setattr(install, name, lambda: None)
    for name in ("create_custom_erpnext_fields", "create_erpnext_property_setters", "create_payment_entry_mpesa_fields",
                 "backfill_b2b_entry_status", "update_settings_module"):
        monkeypatch.setattr(migrate, name, lambda: None)
    made = []
    monkeypatch.setattr(migrate, "create_custom_hrms_fields", lambda: made.append("hrms"))
    env.frappe.get_installed_apps = lambda: ["frappe", "erpnext"]
    migrate.after_migrate()
    assert made == []
    env.frappe.get_installed_apps = lambda: ["frappe", "erpnext", "hrms"]
    migrate.after_migrate()
    assert made == ["hrms"]


def test_the_salary_fields_hrms_and_erpnext_get(env, monkeypatch):
    migrate = _migrate(monkeypatch)
    made = []
    monkeypatch.setattr(migrate, "create_fields", lambda fields: made.append(fields))
    migrate.create_custom_hrms_fields()
    (hrms,) = made
    assert [f["fieldname"] for f in hrms["Mpesa B2B Payment Request"]] == [
        "salary_slip", "payroll_entry", "expense_claim", "employee_advance"]
    assert hrms["Mpesa B2B Payment Request"][0]["options"] == "Salary Slip"
    made.clear()
    migrate.create_custom_erpnext_fields()
    first = made[0]
    assert "mpesa_b2b_payment_request" in [f["fieldname"] for f in first["Journal Entry Account"]]


JEA_TABLE = b2b_fixture.JEA


def test_a_b2c_result_checks_the_name_once_and_queues_the_lines_update(env):
    response = importlib.import_module("mpsa_suite.services.b2c_response_service")
    schedule_row(env)
    phone_line(env, "L2", status="Sent", b2c_disbursement="MPESA-B2C-1", b2c_row="ROW-1")
    env.frappe.db.table(ROW)["ROW-1"] = {"name": "ROW-1", "parent": "MPESA-B2C-1", "payment_status": "Initiated",
                                         "b2c_disbursement_request": "B2C-1", "mpesa_schedule_line": "L2"}
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 1, "status": "Paid", "transaction_id": "QB2C1",
                                         "transaction_completed_datetime": NOW, "b2c_payment_disbursement": "MPESA-B2C-1",
                                         "b2c_payment_disbursement_reference": "ROW-1",
                                         "receiver_public_name": "254722000111 - SOMEONE ELSE"}
    response.update_b2c_reference_status("B2C-1")
    assert env.frappe.db.table(ROW)["ROW-1"]["payment_status"] == "Paid"
    account = env.frappe.db.table(EMPACC)["MEA-2"]
    assert account["name_mismatch"] == 1 and len(env.alerts) == 1  # the phone is blocked until a manager confirms
    assert [j for j in env.frappe.enqueued if j[0].endswith("schedule.sync_b2c_line")]
    response.update_b2c_reference_status("B2C-1")  # the accounting sweep replays it: no second alert
    assert len(env.alerts) == 1
