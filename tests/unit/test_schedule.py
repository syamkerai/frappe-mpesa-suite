"""The M-Pesa Payment Schedule (money_out.schedule), on the in-memory site of b2b_fixture (SQL on
SQLite), with the real B2B request controller and checks behind it.

Covers: the date rule on every path (confirm_payment, the request's submit, send_b2b and
send_deferred, with the site's day mocked); a schedule line only from Pay Selected; lines that
left Planned cannot change and the server's fields are restored; destinations from the approved
account; planned lines in left_to_pay, against double planning and direct payments; maker-checker
on and off; the token (5 seconds, single use, same user, refused after a change); the summary
per payee, shortcode and balance; confirm -> send_batch -> send_deferred with its committed
marker -> Result -> booking -> line Paid; moving dates; Plan Again; Close; the interrupted-send
sweep; the approximate balance.
"""

import datetime
import importlib
import itertools
import types

import pytest

import b2b_fixture
import fake_frappe

SCHEDULE, LINE, B2B = "M-Pesa Payment Schedule", "M-Pesa Payment Schedule Line", b2b_fixture.B2B
PI = "Purchase Invoice"
TODAY, NOW = b2b_fixture.TODAY, b2b_fixture.NOW
ACCOUNTS, PAYER, OTHER = "accounts@example.com", "payer@example.com", "manager@example.com"
ACK = {"OriginatorConversationID": "OCID-9", "ConversationID": "AG_9", "ResponseCode": "0",
       "ResponseDescription": "Accept the service request successfully."}


class Cache:
    """frappe.cache: values kept as set, with their expiry recorded."""

    def __init__(self):
        self.values, self.expiry = {}, {}

    def get_value(self, key, *a, **kw):
        return self.values.get(key)

    def set_value(self, key, value, expires_in_sec=None, **kw):
        self.values[key], self.expiry[key] = value, expires_in_sec

    def delete_value(self, key):
        self.values.pop(key, None)


def _datetime(value):
    if isinstance(value, str):
        return datetime.datetime.fromisoformat(value)
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return datetime.datetime.combine(value, datetime.time())
    return value


def _b2b_doc_class(env, controller):
    seq = itertools.count(1)

    class B2BDoc(fake_frappe.FakeDoc, controller):
        """A B2B request with the real controller: insert runs validate, submit runs
        before_submit and on_submit, cancel runs before_cancel."""

        def __init__(self, row):
            fake_frappe.FakeDoc.__init__(self, env.frappe, B2B, row)
            object.__setattr__(self, "flags", fake_frappe._dict())
            object.__setattr__(self, "meta", types.SimpleNamespace(has_field=lambda field: True))

        def update(self, values):
            self._row.update(values)

        def set(self, key, value):
            self._row[key] = value

        def insert(self, ignore_permissions=False):
            self._row.setdefault("name", f"MB2B-S{next(seq)}")
            self._row.setdefault("docstatus", 0)
            self._row.setdefault("status", "Not Initiated")
            self._row.setdefault("command_id", "BusinessPayBill")
            self._row.setdefault("creation", NOW)
            self.validate()
            env.frappe.db.table(B2B)[self._row["name"]] = self._row
            env.inserted.append((B2B, env.frappe.session.user, dict(self.flags)))
            return self

        def submit(self):
            self._row["docstatus"] = 1
            self.before_submit()
            env.frappe.db.table(B2B)[self._row["name"]] = self._row
            self.on_submit()

        def cancel(self):
            self.before_cancel()
            self._row["docstatus"] = 2

    return B2BDoc


@pytest.fixture
def env(monkeypatch):
    with b2b_fixture.site(monkeypatch) as env:
        frappe = env.frappe
        frappe.cache = Cache()
        hashes = itertools.count(1)
        frappe.generate_hash = lambda *a, length=10, **kw: f"h{next(hashes):0{length - 1}d}"[:length]
        frappe.get_roles = lambda user=None: sorted(frappe.user_roles)
        frappe.local.message_log = []
        env.schedule = importlib.import_module("mpsa_suite.money_out.schedule")
        env.balances = importlib.import_module("mpsa_suite.wallet.balances")
        for module in (env.schedule, env.balances):  # SQLite gives datetimes back as text
            monkeypatch.setattr(module, "get_datetime", _datetime)
        controller = importlib.import_module(
            "mpsa_suite.mpsa_suite.doctype.mpesa_b2b_payment_request.mpesa_b2b_payment_request")
        B2BDoc = _b2b_doc_class(env, controller.MpesaB2BPaymentRequest)
        env.B2BDoc = B2BDoc
        plain_new_doc, plain_get_doc = frappe.new_doc, frappe.get_doc
        frappe.new_doc = lambda doctype: B2BDoc({}) if doctype == B2B else plain_new_doc(doctype)

        def get_doc(doctype, name=None, **kw):
            if doctype == B2B and isinstance(name, str):
                row = frappe.db.table(B2B).get(name)
                if row is None:
                    raise frappe.DoesNotExistError(name)
                return B2BDoc(row)
            return plain_get_doc(doctype, name, **kw)

        frappe.get_doc = get_doc
        env.clock = [1000.0]
        monkeypatch.setattr(env.schedule, "_clock", lambda: env.clock[0])
        monkeypatch.setattr(env.schedule.time, "sleep", lambda seconds: env.log.append(("sleep", seconds)))
        env.posted = []

        def post(settings, endpoint, payload, **kw):
            env.log.append(("post", kw["name"]))
            env.posted.append((kw["name"], payload))
            return dict(ACK, ConversationID=f"AG_{kw['name']}", OriginatorConversationID=f"OC_{kw['name']}"), None

        monkeypatch.setattr(env.b2b, "post", post)
        frappe.db.table("Mpesa Settings")[b2b_fixture.SETTINGS].update(security_credential="Q1JFRA==", till_number=None)
        frappe.db.table("Supplier")["ACME"]["supplier_name"] = "Acme Fuel Ltd"
        frappe.session.user = PAYER
        frappe.user_roles = {"M-Pesa Payer"}
        yield env


def schedule_row(env, name="MPS-1", **kw):
    row = {"name": name, "title": "October suppliers", "company": "Astral", "b2b_settings": b2b_fixture.SETTINGS,
           "closed": 0, "status": "Open", "modified": NOW, **kw}
    env.frappe.db.table(SCHEDULE)[name] = row
    return row


def line_row(env, name="L1", parent="MPS-1", **kw):
    row = {"name": name, "parent": parent, "parenttype": SCHEDULE, "idx": int(name[1:]) if name[1:].isdigit() else 1,
           "modified": NOW, "planned_date": TODAY, "party_type": "Supplier", "party": "ACME",
           "payee_name": "Acme Fuel Ltd", "payee_account_doctype": "Supplier M-Pesa Account", "payee_account": "MSA-1",
           "route": "B2B Paybill", "destination": "888880 / ACME-1", "one_off_type": None, "one_off_shortcode": None,
           "one_off_account": None, "amount": 3000, "reference_doctype": PI, "reference_name": "PINV-1",
           "entry_mode": "Book automatically", "remarks": None, "status": "Planned", "request": None, "receipt": None,
           "paid_on": None, "entry_status": None, "last_problem": None, "prepared_by": ACCOUNTS,
           "changed_by": ACCOUNTS, "approved_by": None, "approved_on": None, "batch": None, "attempts": 0, **kw}
    env.frappe.db.table(LINE)[name] = row
    return row


def day(offset: int) -> datetime.date:
    return TODAY + datetime.timedelta(days=offset)


def line_of(env, name="L1"):
    return env.frappe.db.table(LINE)[name]


def prepared(env, *lines):
    """prepare_payment, then 6 seconds later."""
    result = env.schedule.prepare_payment(list(lines))
    env.clock[0] += 6
    return result


def run_jobs(env, method_end: str):
    """Run the jobs queued for a method (as the worker would), and clear them."""
    jobs = [j for j in env.frappe.enqueued if j[0].endswith(method_end)]
    env.frappe.enqueued[:] = [j for j in env.frappe.enqueued if not j[0].endswith(method_end)]
    module = importlib.import_module(jobs[0][0].rsplit(".", 1)[0]) if jobs else None
    for method, kw in jobs:
        kw = {k: v for k, v in kw.items()
              if k not in ("queue", "timeout", "enqueue_after_commit", "job_id", "deduplicate")}
        getattr(module, method.rsplit(".", 1)[1])(**kw)
    return jobs


# ---------------------------------------------------------------- the date rule, on every path
@pytest.mark.parametrize("offset, due", [(-1, True), (0, True), (1, False)])
def test_a_line_is_due_from_its_planned_day_on(env, offset, due):
    schedule_row(env)
    line_row(env, planned_date=day(offset), request="MB2B-X", status="Sending")
    doc = fake_frappe._dict(name="MB2B-X", schedule_line="L1", payment_schedule="MPS-1")
    if due:
        env.schedule.assert_line_due(doc)
    else:
        with pytest.raises(env.frappe.ValidationError, match="Not due until 19 Oct 2026"):
            env.schedule.assert_line_due(doc)
    assert ("row", LINE, "L1", True) in env.log  # under a lock on the line


def test_the_planned_day_is_the_sites_day(env, monkeypatch):
    # nowdate() is the site's time zone: when the site's day is the 19th, a line of the 19th is due
    schedule_row(env)
    line_row(env, planned_date=day(1), request="MB2B-X", status="Sending")
    doc = fake_frappe._dict(name="MB2B-X", schedule_line="L1", payment_schedule="MPS-1")
    monkeypatch.setattr(env.schedule, "nowdate", lambda: day(1).isoformat())
    env.schedule.assert_line_due(doc)
    monkeypatch.setattr(env.schedule, "nowdate", lambda: TODAY.isoformat())
    with pytest.raises(env.frappe.ValidationError, match="Not due until"):
        env.schedule.assert_line_due(doc)


def test_a_line_belongs_to_its_own_request_only(env):
    schedule_row(env)
    line_row(env, request="MB2B-OTHER", status="Sending")
    with pytest.raises(env.frappe.ValidationError, match="is not the payment of its schedule line"):
        env.schedule.assert_line_due(fake_frappe._dict(name="MB2B-X", schedule_line="L1", payment_schedule="MPS-1"))
    with pytest.raises(env.frappe.ValidationError, match="belongs to MPS-1, not MPS-2"):
        env.schedule.assert_line_due(fake_frappe._dict(name="MB2B-OTHER", schedule_line="L1",
                                                       payment_schedule="MPS-2"))


def test_confirm_refuses_a_line_not_yet_due(env):
    env.pi()
    schedule_row(env)
    line_row(env, planned_date=day(1))
    result = env.schedule.prepare_payment(["L1"])
    assert result["token"] is None and result["not_included"][0]["reason"] == "Not due until 19 Oct 2026."
    # a token made while it was due, for a line moved to tomorrow since: refused at confirm too
    line_of(env)["planned_date"] = TODAY
    token = env.schedule.prepare_payment(["L1"])["token"]
    line_of(env)["planned_date"] = day(1)
    env.clock[0] += 6
    answer = env.schedule.confirm_payment(token)
    assert answer["sent"] == [] and answer["refused"][0]["reason"] == "Not due until 19 Oct 2026."
    assert not env.frappe.db.table(B2B) and line_of(env)["last_problem"] == "Not due until 19 Oct 2026."


def test_the_request_submit_refuses_a_line_not_yet_due_even_with_the_schedules_flags(env):
    # a console insert with the flags set by hand still meets the date rule at submit
    env.pi()
    schedule_row(env)
    line_row(env, planned_date=day(1))
    doc = env.B2BDoc({"mpesa_settings": b2b_fixture.SETTINGS, "company": "Astral", "amount": 3000, "remarks": "x",
                      "supplier": "ACME", "supplier_mpesa_account": "MSA-1", "purchase_invoice": "PINV-1",
                      "payment_schedule": "MPS-1", "schedule_line": "L1"})
    doc.flags.mpesa_from_schedule = doc.flags.mpesa_send_later = True
    doc.insert()
    line_of(env)["request"] = doc.name
    with pytest.raises(env.frappe.ValidationError, match="Send via M-Pesa button"):
        doc.submit()  # not even Pay Selected's flags make a plain submit the deliberate step
    with pytest.raises(env.frappe.ValidationError, match="Not due until 19 Oct 2026"), _deliberate(doc):
        doc.submit()


def test_send_b2b_refuses_a_line_not_yet_due_and_never_sends_a_schedule_request(env):
    schedule_row(env)
    line_row(env, planned_date=day(1), request="MB2B-1", status="Sending")
    env.request("MB2B-1", status="Not Initiated", transaction_id=None, payment_schedule="MPS-1", schedule_line="L1")
    with pytest.raises(env.frappe.ValidationError, match="Not due until"):
        env.b2b.send_b2b("MB2B-1")
    line_of(env)["planned_date"] = TODAY
    with pytest.raises(env.frappe.ValidationError, match="it is sent by the schedule"):
        env.b2b.send_b2b("MB2B-1")  # only send_deferred, which commits its marker first (R1)
    assert env.posted == []


def test_send_deferred_marks_a_line_not_yet_due_failed_without_sending(env):
    env.pi()
    schedule_row(env)
    line_row(env, planned_date=day(1), request="MB2B-1", status="Sending")
    row = env.request("MB2B-1", status="Not Initiated", transaction_id=None, payment_schedule="MPS-1",
                      schedule_line="L1", purchase_invoice="PINV-1", amount=3000)
    env.b2b.send_deferred("MB2B-1")
    assert env.posted == []
    assert row["status"] == "Failed" and row["error_message"].startswith("Not sent: Not due until 19 Oct 2026")
    assert row["send_started_on"]  # the marker stays: it is never sent again
    assert (line_of(env)["status"], line_of(env)["last_problem"]) == ("Failed", row["error_message"])


# ---------------------------------------------------------------- a schedule line only from Pay Selected
def _deliberate(doc):
    """As if Send via M-Pesa (send_confirm.confirm_send) submitted it."""
    return importlib.import_module("mpsa_suite.money_out.send_confirm").deliberate("Send via M-Pesa", doc)


@pytest.mark.parametrize("field", ["schedule_line", "payment_schedule"])
def test_a_request_carrying_a_schedule_line_without_the_schedule_is_refused(env, field):
    # an API insert with docstatus 1 runs before_submit (F1), and flags cannot come through the API
    env.pi()
    schedule_row(env)
    line_row(env)
    doc = env.B2BDoc({"name": "MB2B-API", "docstatus": 1, "mpesa_settings": b2b_fixture.SETTINGS, "amount": 3000,
                      "supplier": "ACME", "supplier_mpesa_account": "MSA-1", "purchase_invoice": "PINV-1",
                      field: "L1" if field == "schedule_line" else "MPS-1"})
    with pytest.raises(env.frappe.ValidationError, match="Send via M-Pesa button"):
        doc.before_submit()
    with _deliberate(doc), pytest.raises(env.frappe.ValidationError, match="paid with Pay Selected in M-Pesa Payments"):
        doc.before_submit()
    doc._row["docstatus"] = 0
    with pytest.raises(env.frappe.ValidationError, match="paid with Pay Selected"):
        doc.validate()


# ---------------------------------------------------------------- the schedule form (validate_schedule)
class Row(fake_frappe._dict):
    def set(self, key, value):
        self[key] = value


class Form(fake_frappe._dict):
    def is_new(self):
        return bool(self.get("_new"))


def form(env, lines, name="MPS-1", new=False, **kw):
    if not new:
        schedule_row(env, name)
    values = {"name": name, "title": "October suppliers", "company": "Astral", "b2b_settings": b2b_fixture.SETTINGS,
              "purpose": None, "_new": new, **kw}
    doc = Form(values)
    doc.lines = [Row({"idx": i, **line}) for i, line in enumerate(lines, 1)]
    return doc


def new_line(**kw):
    return {"name": kw.pop("name", "N1"), "planned_date": TODAY, "party_type": "Supplier", "party": "ACME",
            "amount": 3000, "reference_doctype": PI, "reference_name": "PINV-1", **kw}


def test_a_new_line_takes_its_destination_from_the_approved_account_and_who_prepared_it(env):
    env.pi()
    doc = form(env, [new_line(payee_account=None, route="B2B Till", destination="999999", status="Paid",
                              request="MB2B-FORGED", prepared_by="someone@else")], new=True)
    env.frappe.session.user = ACCOUNTS
    env.schedule.validate_schedule(doc)
    row = doc.lines[0]
    assert (row.payee_account, row.route, row.destination, row.payee_name) == (
        "MSA-1", "B2B Paybill", "888880 / ACME-1", "Acme Fuel Ltd")
    assert (row.status, row.request, row.prepared_by, row.changed_by) == ("Planned", None, ACCOUNTS, ACCOUNTS)
    assert (doc.total_planned, doc.status, doc.purpose) == (3000, "Open", "General")


def test_a_new_line_cannot_be_planned_before_today(env):
    env.pi()
    with pytest.raises(env.frappe.ValidationError, match="Row 1: Pay On cannot be before today."):
        env.schedule.validate_schedule(form(env, [new_line(planned_date=day(-1))], new=True))


def test_an_overdue_line_saved_earlier_may_stay_overdue(env):
    env.pi()
    line_row(env, planned_date=day(-3))
    doc = form(env, [dict(line_of(env), remarks="kept")])
    env.schedule.validate_schedule(doc)
    assert doc.lines[0].planned_date == day(-3)


def test_a_planned_line_restores_the_servers_fields(env):
    env.pi()
    line_row(env)
    forged = dict(line_of(env), status="Paid", request="MB2B-FORGED", receipt="QFORGED", approved_by=PAYER,
                  prepared_by=PAYER)
    doc = form(env, [forged])
    env.schedule.validate_schedule(doc)
    row = doc.lines[0]
    assert (row.status, row.request, row.receipt, row.approved_by, row.prepared_by) == (
        "Planned", None, None, None, ACCOUNTS)


@pytest.mark.parametrize("status", ["Sending", "Sent", "Paid", "Failed", "Unknown"])
def test_a_line_sent_to_mpesa_cannot_be_changed_or_deleted(env, status):
    env.pi()
    line_row(env, status=status, request="MB2B-1", approved_on=datetime.datetime(2026, 10, 23, 9, 0))
    with pytest.raises(env.frappe.ValidationError,
                       match="Row 1 was sent to M-Pesa on 23 Oct 2026; it cannot be changed."):
        env.schedule.validate_schedule(form(env, [dict(line_of(env), amount=5000)]))
    with pytest.raises(env.frappe.ValidationError, match="it cannot be deleted"):
        env.schedule.validate_schedule(form(env, []))
    doc = form(env, [dict(line_of(env), status="Planned", request=None, receipt="QFORGED")])
    env.schedule.validate_schedule(doc)  # unchanged: saved, with the server's fields as they are
    assert (doc.lines[0].status, doc.lines[0].request, doc.lines[0].receipt) == (status, "MB2B-1", None)


def test_a_line_cannot_be_saved_or_deleted_on_its_own(env):
    # the REST API and frappe.client.save save or delete a row by itself, skipping validate_schedule;
    # Frappe calls the row's own hooks only then, never when the schedule is saved or deleted
    module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.m_pesa_payment_schedule_line.m_pesa_payment_schedule_line")
    row = module.MPesaPaymentScheduleLine()
    for hook in (row.validate, row.on_trash):
        with pytest.raises(env.frappe.ValidationError, match="Change payment schedule lines on their schedule."):
            hook()


def test_a_line_of_another_schedule_cannot_be_moved_in(env):
    # Frappe updates rows by name only: saved here, the other schedule's paid line would move and be reset
    env.pi()
    schedule_row(env, "MPS-OTHER")
    line_row(env, "L9", parent="MPS-OTHER", status="Paid", request="MB2B-9", receipt="QPAID1")
    moved = {k: v for k, v in line_of(env, "L9").items() if k not in ("idx", "parent")}
    with pytest.raises(env.frappe.ValidationError, match="Row 1 is a line of MPS-OTHER: it cannot be moved to MPS-1."):
        env.schedule.validate_schedule(form(env, [moved]))
    line = line_of(env, "L9")
    assert (line["parent"], line["status"], line["request"], line["receipt"]) == ("MPS-OTHER", "Paid", "MB2B-9",
                                                                                  "QPAID1")


def test_a_document_cannot_get_more_planned_than_it_has_left(env):
    env.pi(outstanding_amount=10000)
    env.request(purchase_invoice="PINV-1", amount=2000, status="Initiated")
    schedule_row(env, "MPS-OTHER")
    line_row(env, "L9", parent="MPS-OTHER", amount=3000)  # planned on another schedule
    lines = [new_line(name="N1", amount=4000), new_line(name="N2", amount=2000)]
    with pytest.raises(env.frappe.ValidationError,
                       match=r"Row 1, 2 plans KES 6,000 for PINV-1, but KES 5,000 is left to pay. PINV-1 owes 10,000, "
                             r"less MB2B-1 Initiated 2,000; planned on MPS-OTHER for 18 Oct 3,000"):
        env.schedule.validate_schedule(form(env, lines, new=True, name="MPS-NEW"))
    env.schedule.validate_schedule(form(env, [new_line(amount=5000)], new=True, name="MPS-NEW"))


def test_a_line_whose_document_belongs_to_another_supplier_is_refused(env):
    env.pi(supplier="Other Ltd")
    with pytest.raises(env.frappe.ValidationError, match="Row 1: PINV-1 belongs to Other Ltd, not ACME."):
        env.schedule.validate_schedule(form(env, [new_line()], new=True))


def test_a_number_not_saved_on_a_supplier_needs_the_switch_and_a_manager(env):
    one_off = new_line(party=None, party_type=None, reference_doctype=None, reference_name=None,
                       one_off_type="Paybill", one_off_shortcode="888888", one_off_account="ACC-9", payee_name="KPLC")
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.ValidationError, match="Allow B2B to Numbers Not Saved on a Supplier"):
        env.schedule.validate_schedule(form(env, [one_off], new=True))
    env.frappe.db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"]["allow_one_off_b2b"] = 1
    with pytest.raises(env.frappe.PermissionError):
        env.schedule.validate_schedule(form(env, [one_off], new=True))
    env.frappe.user_roles = {"M-Pesa Manager"}
    doc = form(env, [one_off], new=True)
    env.schedule.validate_schedule(doc)
    assert (doc.lines[0].route, doc.lines[0].destination, doc.lines[0].entry_mode) == (
        "B2B Paybill", "888888 / ACC-9", "Make the entry later")


def test_a_line_without_pay_from_is_refused(env):
    env.pi()
    with pytest.raises(env.frappe.ValidationError, match="Pick Pay From"):
        env.schedule.validate_schedule(form(env, [new_line()], new=True, b2b_settings=None))


def test_problems_that_may_clear_by_the_pay_day_are_shown_not_refused(env):
    env.pi(on_hold=1)
    doc = form(env, [new_line(planned_date=day(5))], new=True)
    env.schedule.validate_schedule(doc)
    assert doc.lines[0].last_problem == "PINV-1 is on hold. Release the hold before paying."


# ---------------------------------------------------------------- planned lines count in left_to_pay
def test_planned_lines_count_against_the_document(env):
    env.pi(outstanding_amount=10000)
    schedule_row(env)
    line_row(env, amount=3000, planned_date=day(4))
    line_row(env, "L2", amount=1000, status="Failed")  # not planned: does not count
    line_row(env, "L3", amount=500, status="Sending", request="MB2B-7")  # counts as its request
    result = env.owed.left_to_pay(PI, "PINV-1")
    assert [(p.kind, p.name, p.amount) for p in result.parts] == [("planned", "MPS-1", 3000)]
    assert result.parts[0].label == "planned on MPS-1 for 22 Oct 3,000" and result.left == 7000
    assert env.owed.left_to_pay(PI, "PINV-1", exclude_line="L1").left == 10000
    assert env.owed.left_to_pay(PI, "PINV-1", exclude_schedule="MPS-1").left == 10000
    assert env.owed.summary(result)["planned"] == 3000


def test_a_direct_request_cannot_pay_what_a_schedule_has_planned(env):
    env.pi(outstanding_amount=10000)
    schedule_row(env)
    line_row(env, amount=8000, planned_date=day(4))
    draft = env.request("MB2B-NEW", docstatus=0, status="Not Initiated", transaction_id=None, amount=3000,
                        purchase_invoice="PINV-1", supplier=None, company=None)
    with pytest.raises(env.frappe.ValidationError,
                       match=r"KES 3,000 is more than is left to pay. PINV-1 owes 10,000, less planned on MPS-1 for "
                             r"22 Oct 8,000: 2,000 is left to pay."):
        env.b2b.validate_request(env.frappe.get_doc(B2B, draft["name"]))


def test_planned_lines_are_locking_reads_under_the_document_lock(env):
    env.pi()
    schedule_row(env)
    line_row(env)
    env.log.clear()
    env.owed.left_to_pay(PI, "PINV-1", for_update=True)
    assert ("sql", (LINE,), True) in env.log
    reads = [e for e in env.log if e[0] in ("sql", "row")]
    assert reads[-1][:2] == ("row", PI)  # the document row still last (R4)


# ---------------------------------------------------------------- maker-checker
@pytest.mark.parametrize("who", ["prepared_by", "changed_by"])
def test_who_prepared_or_last_changed_a_line_cannot_pay_it(env, who):
    env.pi()
    schedule_row(env)
    line = line_row(env, **{who: PAYER})
    check = env.schedule.line_problem(fake_frappe._dict(line))
    assert check.problem == "You prepared or last changed this line: another person must pay it."
    assert env.schedule.line_problem(fake_frappe._dict(line), payer=OTHER).problem is None


def test_maker_checker_can_be_switched_off_for_one_person_teams(env, monkeypatch):
    env.pi()
    schedule_row(env)
    line = line_row(env, prepared_by=PAYER, changed_by=PAYER)
    common = importlib.import_module("mpsa_suite.money_in.common")
    monkeypatch.setattr(common, "suite", lambda: fake_frappe._dict(schedule_maker_checker=0))
    assert env.schedule.line_problem(fake_frappe._dict(line)).problem is None
    monkeypatch.setattr(common, "suite", lambda: fake_frappe._dict(schedule_maker_checker=None))
    assert env.schedule.maker_checker_on()  # not yet migrated: on


def test_maker_checker_is_checked_again_at_confirm(env):
    env.pi()
    schedule_row(env)
    line_row(env)
    token = prepared(env, "L1")["token"]
    line_of(env)["changed_by"] = PAYER  # the payer changed it after preparing the payment
    answer = env.schedule.confirm_payment(token)
    assert answer["refused"][0]["reason"] == "You prepared or last changed this line: another person must pay it."


# ---------------------------------------------------------------- the token
def test_confirm_works_only_five_seconds_after_the_summary(env):
    env.pi()
    schedule_row(env)
    line_row(env)
    token = env.schedule.prepare_payment(["L1"])["token"]
    env.clock[0] += 4.9
    with pytest.raises(env.frappe.ValidationError, match="Confirm works 5 seconds after it opens"):
        env.schedule.confirm_payment(token)
    env.clock[0] += 0.2
    assert len(env.schedule.confirm_payment(token)["sent"]) == 1  # too early did not use it up


def test_a_token_is_used_once(env):
    env.pi()
    schedule_row(env)
    line_row(env)
    token = prepared(env, "L1")["token"]
    env.schedule.confirm_payment(token)
    with pytest.raises(env.frappe.ValidationError, match="expired or was used already"):
        env.schedule.confirm_payment(token)
    assert len(env.frappe.db.table(B2B)) == 1
    assert ("lock", f"pay-token:{token}") in env.log  # checked and used under its lock


def test_a_token_lives_ten_minutes_and_belongs_to_its_user(env):
    env.pi()
    schedule_row(env)
    line_row(env)
    token = prepared(env, "L1")["token"]
    key = env.schedule._token_key(token)
    assert env.frappe.cache.expiry[key] == 600 and env.frappe.cache.values[key]["user"] == PAYER
    env.frappe.session.user = OTHER
    env.frappe.user_roles = {"M-Pesa Manager"}
    with pytest.raises(env.frappe.ValidationError, match="made for another user"):
        env.schedule.confirm_payment(token)
    env.frappe.cache.values.pop(key)  # expired in Redis
    env.frappe.session.user = PAYER
    with pytest.raises(env.frappe.ValidationError, match="expired or was used already"):
        env.schedule.confirm_payment(token)


def test_a_line_changed_after_the_summary_is_refused(env):
    env.pi()
    schedule_row(env)
    line_row(env, "L1")
    line_row(env, "L2", amount=1000)
    token = prepared(env, "L1", "L2")["token"]
    line_of(env, "L2")["amount"] = 2000
    answer = env.schedule.confirm_payment(token)
    assert [s["line"] for s in answer["sent"]] == ["L1"]
    assert answer["refused"] == [{"line": "L2", "reason": env.schedule.CHANGED, "payee": "Acme Fuel Ltd",
                                  "amount": 2000, "schedule": "MPS-1"}]


def test_paying_needs_a_payout_role(env):
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.PermissionError):
        env.schedule.prepare_payment(["L1"])
    with pytest.raises(env.frappe.PermissionError):
        env.schedule.confirm_payment("x")


# ---------------------------------------------------------------- the summary
def test_the_summary_groups_by_payee_and_shortcode_and_lists_what_is_left_out(env):
    env.pi(outstanding_amount=20000)
    env.pi("PINV-2", outstanding_amount=500)
    env.frappe.db.table("Supplier")["BETA"] = {"name": "BETA", "on_hold": 0, "supplier_name": "Beta Gas"}
    env.frappe.db.table("Supplier M-Pesa Account")["MSA-2"] = dict(
        env.frappe.db.table("Supplier M-Pesa Account")["MSA-1"], name="MSA-2", supplier="BETA",
        account_type="Till (Buy Goods)", shortcode="555555", account_number=None)
    schedule_row(env)
    line_row(env, "L1", amount=3000)
    line_row(env, "L2", amount=2000)
    line_row(env, "L3", party="BETA", payee_name="Beta Gas", payee_account="MSA-2", route="B2B Till",
             destination="555555", reference_doctype=None, reference_name=None, amount=1500)
    line_row(env, "L4", amount=900, reference_name="PINV-2")  # more than PINV-2 has left
    line_row(env, "L5", planned_date=day(2))
    result = env.schedule.prepare_payment(["L1", "L2", "L3", "L4", "L5"])
    assert (result["count"], result["total"], result["delay"]) == (3, 6500, 5)
    assert result["parties"] == [
        {"payee": "Acme Fuel Ltd", "party_type": "Supplier", "party": "ACME", "route": "B2B Paybill",
         "destination": "888880 / ACME-1", "lines": 2, "amount": 5000},
        {"payee": "Beta Gas", "party_type": "Supplier", "party": "BETA", "route": "B2B Till", "destination": "555555",
         "lines": 1, "amount": 1500},
    ]
    assert [(r["route"], r["lines"], r["amount"], r["shortcode"]) for r in result["routes"]] == [
        ("B2B Paybill", 2, 5000, "600999"), ("B2B Till", 1, 1500, "600999")]
    assert [(n["line"], n["reason"].split(".")[0]) for n in result["not_included"]] == [
        ("L4", "KES 900 is more than is left to pay"), ("L5", "Not due until 20 Oct 2026")]
    assert "No Working balance is known for Astral 600999: press Refresh Balances." in result["warnings"]


def test_the_summary_shows_where_each_amount_goes(env):
    # two numbers not saved on a supplier with the same payee name: one row each, with its paybill
    env.frappe.db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"]["allow_one_off_b2b"] = 1
    env.frappe.session.user, env.frappe.user_roles = OTHER, {"M-Pesa Manager"}
    schedule_row(env)
    one_off = {"party": None, "party_type": None, "payee_name": "KPLC", "payee_account": None,
               "payee_account_doctype": None, "one_off_type": "Paybill", "reference_doctype": None,
               "reference_name": None, "entry_mode": "Make the entry later"}
    line_row(env, "L1", one_off_shortcode="888888", one_off_account="ACC-9", destination="888888 / ACC-9", amount=700,
             **one_off)
    line_row(env, "L2", one_off_shortcode="777777", one_off_account="ACC-9", destination="777777 / ACC-9", amount=300,
             **one_off)
    result = env.schedule.prepare_payment(["L1", "L2"])
    assert [(p["payee"], p["party"], p["destination"], p["amount"]) for p in result["parties"]] == [
        ("KPLC", None, "777777 / ACC-9", 300), ("KPLC", None, "888888 / ACC-9", 700)]


def test_the_summary_warns_when_the_balance_is_short(env):
    env.pi(outstanding_amount=20000)
    schedule_row(env)
    line_row(env, amount=4000)
    env.frappe.db.table("Mpesa Settings")[b2b_fixture.SETTINGS]["account_balance"] = (
        "Working Account|KES|5000.00|3500.00|0.00|0.00&Utility Account|KES|900.00|900.00|0.00|0.00")
    env.request("MB2B-9", status="Initiated", transaction_id=None, amount=1000, purchase_invoice=None)  # in flight
    result = env.schedule.prepare_payment(["L1"])
    (balance,) = result["balances"]
    assert (balance["sub_account"], balance["amount"], balance["pending"], balance["after"], balance["short"]) == (
        "Working", 3500, 1000, 2500, 1500)
    assert any("short by KES 1,500" in w for w in result["warnings"])


def test_a_supplier_with_earlier_payouts_needs_a_fresh_summary_when_more_come_in(env):
    env.pi()
    schedule_row(env)
    line_row(env)
    env.request("MB2B-0001", amount=500, transaction_id="R1", entry_status="Entry Not Made")
    result = prepared(env, "L1")
    assert any("already got M-Pesa payments that are not on any invoice: MB2B-0001" in w for w in result["warnings"])
    env.request("MB2B-0002", amount=700, transaction_id="R2", entry_status="Entry Not Made")
    answer = env.schedule.confirm_payment(result["token"])
    assert "got M-Pesa payments with no invoice after you pressed Pay Selected" in answer["refused"][0]["reason"]


# ---------------------------------------------------------------- confirm, send, Result, entry
def test_confirm_submits_the_requests_and_sends_them_from_a_job_after_commit(env):
    env.pi(outstanding_amount=10000)
    env.request("MB2B-0001", amount=500, transaction_id="R1", entry_status="Entry Not Made")  # seen in the summary
    schedule_row(env)
    line_row(env, "L1", amount=3000, remarks="Fuel October")
    token = prepared(env, "L1")["token"]
    answer = env.schedule.confirm_payment(token)
    ((sent,),) = [answer["sent"]]
    request = env.frappe.db.table(B2B)[sent["request"]]
    assert (request["docstatus"], request["status"], request["payment_schedule"], request["schedule_line"]) == (
        1, "Not Initiated", "MPS-1", "L1")
    assert (request["supplier"], request["receiver_shortcode"], request["account_reference"], request["amount"]) == (
        "ACME", "888880", "ACME-1", 3000)
    assert request["remarks"] == "Fuel October" and request["checked_earlier_payouts"] == 1
    assert env.posted == []  # nothing is sent by confirm itself
    line = line_of(env)
    assert (line["status"], line["request"], line["approved_by"], line["attempts"], line["batch"]) == (
        "Sending", sent["request"], PAYER, 1, answer["batch"])
    assert env.frappe.db.table(SCHEDULE)["MPS-1"]["total_in_progress"] == 3000
    (job,) = [j for j in env.frappe.enqueued if j[0].endswith("schedule.send_batch")]
    assert job[1]["enqueue_after_commit"] and job[1]["queue"] == "long" and job[1]["requests"] == [sent["request"]]
    assert job[1]["job_id"] == f"mpesa_schedule_send_{answer['batch']}"  # the sweep sees it is still alive
    # the request's own checks ran again at submit under the invoice's lock, its own line not counted
    assert "b2c-pay:Purchase Invoice:PINV-1" in env.locks


def test_send_deferred_commits_its_marker_before_the_call(env):
    env.pi()
    schedule_row(env)
    line_row(env)
    env.schedule.confirm_payment(prepared(env, "L1")["token"])
    env.log.clear()
    run_jobs(env, "schedule.send_batch")
    name = line_of(env)["request"]
    request = env.frappe.db.table(B2B)[name]
    assert request["send_started_on"] and request["status"] == "Initiated"
    events = [e for e in env.log if e[0] in ("commit", "post")]
    assert events[0] == ("commit",) and events[1] == ("post", name)  # the marker is committed first (R1)
    run_jobs(env, "schedule.sync_line")
    assert line_of(env)["status"] == "Sent"
    env.b2b.send_deferred(name)  # again: never sent twice
    assert len(env.posted) == 1


def test_send_deferred_takes_the_documents_lock_before_the_rows(env):
    # R4, as confirm and the booking job do: they hold the document's lock, then lock its payouts
    env.pi()
    schedule_row(env)
    line_row(env)
    env.schedule.confirm_payment(prepared(env, "L1")["token"])
    name = line_of(env)["request"]
    env.locks.clear()  # a new worker holds none
    env.log.clear()
    assert env.b2b.send_deferred(name) is True
    after = env.log[env.log.index(("commit",)) + 1:]  # after the marker's commit
    doc_lock = after.index(("lock", "b2c-pay:Purchase Invoice:PINV-1"))
    assert doc_lock < after.index(("row", B2B, name, True)) < after.index(("row", LINE, "L1", True))
    assert after.index(("row", LINE, "L1", True)) < after.index(("post", name))


def test_send_deferred_claims_with_a_locking_read(env, monkeypatch):
    # another worker committed its marker after this job's snapshot: a plain read would miss it (R1)
    env.pi()
    schedule_row(env)
    line_row(env)
    env.schedule.confirm_payment(prepared(env, "L1")["token"])
    name = line_of(env)["request"]
    row = env.frappe.db.table(B2B)[name]
    stale = dict(row)
    row["send_started_on"] = NOW
    plain_get_doc = env.frappe.get_doc
    monkeypatch.setattr(env.frappe, "get_doc", lambda doctype, docname=None, **kw: (
        env.B2BDoc(dict(stale)) if doctype == B2B and not kw.get("for_update") else plain_get_doc(doctype, docname,
                                                                                                    **kw)))
    assert env.b2b.send_deferred(name) is False
    assert env.posted == [] and (row["status"], row["send_started_on"]) == ("Not Initiated", NOW)


def test_a_failed_check_keeps_what_was_committed_meanwhile(env, monkeypatch):
    # a manager's cancel took the row between the marker's commit and the lock: it stays Unknown
    env.pi()
    schedule_row(env)
    line_row(env)
    env.schedule.confirm_payment(prepared(env, "L1")["token"])
    name = line_of(env)["request"]
    real = env.b2b.lock_for_send

    def cancelled_first(doctype, docname):
        env.frappe.db.table(B2B)[docname].update(docstatus=2, status="Unknown")
        return real(doctype, docname)

    monkeypatch.setattr(env.b2b, "lock_for_send", cancelled_first)
    assert env.b2b.send_deferred(name) is False
    row = env.frappe.db.table(B2B)[name]
    assert (row["docstatus"], row["status"]) == (2, "Unknown") and env.posted == []


def test_send_batch_waits_between_payments(env, monkeypatch):
    calls = []
    monkeypatch.setattr(env.b2b, "send_deferred", lambda name: calls.append(name) or True)
    env.schedule.send_batch(["A", "B", "C"])
    assert calls == ["A", "B", "C"] and [e for e in env.log if e[0] == "sleep"] == [("sleep", 2), ("sleep", 2)]


def test_send_batch_waits_only_after_a_payment_was_posted(env, monkeypatch):
    # already sent or cancelled ones (a batch the sweep queued again) take no time
    posted = {"A": False, "B": False, "C": True, "D": True}
    calls = []
    monkeypatch.setattr(env.b2b, "send_deferred", lambda name: calls.append(name) or posted[name])
    env.schedule.send_batch(["A", "B", "C", "D"])
    assert calls == ["A", "B", "C", "D"] and [e for e in env.log if e[0] == "sleep"] == [("sleep", 2)]


def test_a_send_that_fails_its_checks_is_failed_and_can_be_planned_again(env):
    env.pi()
    schedule_row(env)
    line_row(env)
    env.schedule.confirm_payment(prepared(env, "L1")["token"])
    env.frappe.db.table("Supplier")["ACME"].update(on_hold=1, hold_type="All", release_date=None)
    run_jobs(env, "schedule.send_batch")
    name = line_of(env)["request"]
    assert env.posted == []
    assert env.frappe.db.table(B2B)[name]["error_message"] == "Not sent: ACME is on hold for payments."
    assert line_of(env)["status"] == "Failed"
    env.frappe.session.user, env.frappe.user_roles = ACCOUNTS, {"Accounts User"}
    env.schedule.plan_again("L1")
    line = line_of(env)
    assert (line["status"], line["request"], line["changed_by"]) == ("Planned", None, ACCOUNTS)
    assert any("planned row 1" in c[2] and name in c[2] for c in env.frappe.comments)


def test_paid_result_books_and_the_line_shows_paid_with_its_entry(env):
    env.pi(outstanding_amount=10000)
    schedule_row(env)
    line_row(env)
    env.schedule.confirm_payment(prepared(env, "L1")["token"])
    run_jobs(env, "schedule.send_batch")
    name = line_of(env)["request"]
    result = {"ResultType": 0, "ResultCode": 0, "ResultDesc": "ok", "ConversationID": f"AG_{name}",
              "OriginatorConversationID": f"OC_{name}", "TransactionID": "QRS1TUV2WX",
              "ResultParameters": {"ResultParameter": [
                  {"Key": "TransCompletedTime", "Value": "20261018101500"},
                  {"Key": "ReceiverPartyPublicName", "Value": "888880 - ACME"}]}}
    env.b2b.handle_result(result, name)
    assert env.frappe.db.table(B2B)[name]["status"] == "Paid"
    run_jobs(env, "b2b.book_payment_entry")
    run_jobs(env, "schedule.sync_line")
    line = line_of(env)
    assert (line["status"], line["receipt"], line["entry_status"]) == ("Paid", "QRS1TUV2WX", "Entry Made")
    run_jobs(env, "schedule.update_totals")
    assert env.frappe.db.table(SCHEDULE)["MPS-1"]["status"] == "Paid"


@pytest.mark.parametrize("request_state, line_status", [
    ({"docstatus": 1, "status": "Not Initiated"}, "Sending"),
    ({"docstatus": 1, "status": "Initiated"}, "Sent"),
    ({"docstatus": 1, "status": "Unknown", "error_message": "No answer from M-Pesa."}, "Unknown"),
    ({"docstatus": 1, "status": "Failed", "error_message": "Bad"}, "Failed"),
    ({"docstatus": 2, "status": "Unknown"}, "Failed"),  # an admin cancelled it after checking the statement
    ({"docstatus": 2, "status": "Paid", "transaction_id": "QLATE1"}, "Paid"),  # paid after the cancel (R2)
])
def test_the_line_follows_its_request(env, request_state, line_status):
    schedule_row(env)
    line_row(env, status="Sending", request="MB2B-1")
    env.request("MB2B-1", schedule_line="L1", payment_schedule="MPS-1", **{"transaction_id": None, **request_state})
    env.schedule.sync_line("MB2B-1")
    assert line_of(env)["status"] == line_status


def test_a_line_planned_again_ignores_its_old_request(env):
    schedule_row(env)
    line_row(env, status="Sending", request="MB2B-NEW")
    env.request("MB2B-OLD", schedule_line="L1", payment_schedule="MPS-1", status="Paid")
    env.schedule.sync_line("MB2B-OLD")
    assert line_of(env)["status"] == "Sending"


# ---------------------------------------------------------------- moving dates, Plan Again, Close
def test_accounts_move_planned_lines_to_today_or_later_with_a_comment(env):
    schedule_row(env)
    line_row(env, planned_date=day(1))
    env.frappe.session.user, env.frappe.user_roles = OTHER, {"Accounts User"}
    with pytest.raises(env.frappe.ValidationError, match="has passed"):
        env.schedule.move_date(["L1"], day(-1).isoformat())
    assert env.schedule.move_date('["L1"]', day(5).isoformat()) == {"moved": 1}
    line = line_of(env)
    assert (line["planned_date"], line["changed_by"]) == (day(5), OTHER)  # the mover may not pay it now
    assert any("from 19 Oct 2026 to 23 Oct 2026" in c[2] for c in env.frappe.comments)


def test_a_sent_line_cannot_be_moved_and_payers_cannot_move(env):
    schedule_row(env)
    line_row(env, status="Sent", request="MB2B-1")
    env.frappe.user_roles = {"M-Pesa Payer"}
    with pytest.raises(env.frappe.PermissionError):
        env.schedule.move_date(["L1"], TODAY.isoformat())
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.ValidationError, match="only a planned line can be moved"):
        env.schedule.move_date(["L1"], TODAY.isoformat())


def test_plan_again_is_for_failed_lines_and_an_unknown_one_needs_a_manager_first(env):
    schedule_row(env)
    line_row(env, status="Unknown", request="MB2B-1")
    env.request("MB2B-1", status="Unknown", transaction_id=None)
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.ValidationError, match="A manager checks the statement and cancels MB2B-1"):
        env.schedule.plan_again("L1")
    line_of(env)["status"] = "Paid"
    with pytest.raises(env.frappe.ValidationError, match="Only a failed line is planned again"):
        env.schedule.plan_again("L1")
    line_of(env)["status"] = "Failed"
    env.frappe.db.table(B2B)["MB2B-1"].update(status="Paid", docstatus=2)  # a late Result paid it
    with pytest.raises(env.frappe.ValidationError, match="MB2B-1 is Paid"):
        env.schedule.plan_again("L1")


def test_close_cancels_the_planned_lines_and_a_schedule_that_paid_cannot_be_deleted(env):
    schedule_row(env)
    line_row(env, "L1")
    line_row(env, "L2", status="Sent", request="MB2B-1")
    env.frappe.user_roles = {"Accounts User"}
    assert env.schedule.close_schedule("MPS-1") == {"cancelled": 1}
    assert (line_of(env, "L1")["status"], line_of(env, "L2")["status"]) == ("Cancelled", "Sent")
    assert env.frappe.db.table(SCHEDULE)["MPS-1"]["status"] == "Closed"
    with pytest.raises(env.frappe.ValidationError, match="cannot be deleted: close it instead"):
        env.schedule.check_delete(fake_frappe._dict(name="MPS-1"))
    schedule_row(env, "MPS-2")
    line_row(env, "L3", parent="MPS-2")
    env.schedule.check_delete(fake_frappe._dict(name="MPS-2"))  # nothing sent: may go


# ---------------------------------------------------------------- the interrupted-send sweep
def test_a_send_interrupted_after_its_marker_becomes_unknown_and_is_never_sent_again(env):
    schedule_row(env)
    line_row(env, status="Sending", request="MB2B-1")
    row = env.request("MB2B-1", status="Not Initiated", transaction_id=None, schedule_line="L1",
                      payment_schedule="MPS-1", send_started_on=NOW - datetime.timedelta(minutes=11))
    env.request("MB2B-2", status="Not Initiated", transaction_id=None,
                send_started_on=NOW - datetime.timedelta(minutes=3))  # still sending
    env.schedule.sweep()
    assert row["status"] == "Unknown" and row["error_message"] == env.schedule.INTERRUPTED
    assert env.frappe.db.table(B2B)["MB2B-2"]["status"] == "Not Initiated"
    assert line_of(env)["status"] == "Unknown" and env.alerts and env.posted == []
    env.b2b.send_deferred("MB2B-1")
    assert env.posted == []


def test_a_request_never_started_is_sent_again_within_the_hour_then_planned_again(env):
    schedule_row(env)
    line_row(env, "L1", status="Sending", request="MB2B-1")
    line_row(env, "L2", status="Sending", request="MB2B-2")
    line_row(env, "L3", status="Sending", request="MB2B-3")
    line_of(env, "L2")["batch"] = "B-7"
    for name, line, minutes in (("MB2B-1", "L1", 2), ("MB2B-2", "L2", 20), ("MB2B-3", "L3", 61)):
        env.request(name, status="Not Initiated", transaction_id=None, schedule_line=line, payment_schedule="MPS-1",
                    send_started_on=None, creation=NOW - datetime.timedelta(minutes=minutes))
    env.schedule.sweep()
    (job,) = [j for j in env.frappe.enqueued if j[0].endswith("schedule.send_batch")]
    assert job[1]["requests"] == ["MB2B-2"]  # the 2-minute one may still be in the queue
    # under its batch's job id, and not while that job (confirm's or an earlier sweep's) is alive
    assert (job[1]["job_id"], job[1]["deduplicate"], job[1]["batch"]) == ("mpesa_schedule_send_B-7", True, "B-7")
    assert env.frappe.db.table(B2B)["MB2B-3"]["docstatus"] == 2  # it never reached M-Pesa
    assert (line_of(env, "L3")["status"], line_of(env, "L3")["request"]) == ("Planned", None)
    assert "was not sent within an hour" in line_of(env, "L3")["last_problem"]
    assert env.frappe.db.table(B2B)["MB2B-1"]["docstatus"] == 1 and env.posted == []


def test_a_request_whose_send_began_is_cancelled_by_a_manager_only_and_stays_unknown(env):
    # its worker died after the marker, or the call is still out: M-Pesa may have paid it (R1)
    env.pi()
    schedule_row(env)
    line_row(env)
    env.schedule.confirm_payment(prepared(env, "L1")["token"])
    name = line_of(env)["request"]
    row = env.frappe.db.table(B2B)[name]
    row["send_started_on"] = NOW - datetime.timedelta(minutes=2)
    with pytest.raises(env.frappe.PermissionError):
        env.frappe.get_doc(B2B, name).cancel()  # the payer: M-Pesa Payer may cancel requests
    assert row["docstatus"] == 1
    env.frappe.session.user, env.frappe.user_roles = OTHER, {"M-Pesa Manager"}
    env.frappe.get_doc(B2B, name).cancel()  # after checking the statement
    assert (row["docstatus"], row["status"], row["error_message"]) == (2, "Unknown", env.schedule.INTERRUPTED)


def test_a_line_whose_request_was_cancelled_mid_send_is_planned_again_by_a_manager_only(env):
    # a cancel that went around before_cancel (it keeps such a request Unknown)
    schedule_row(env)
    line_row(env, status="Sending", request="MB2B-1")
    env.request("MB2B-1", docstatus=2, status="Not Initiated", transaction_id=None, schedule_line="L1",
                payment_schedule="MPS-1", send_started_on=NOW - datetime.timedelta(minutes=2))
    env.schedule.sync_line("MB2B-1")
    line = line_of(env)
    assert line["status"] == "Failed" and "after its send began, so M-Pesa may have paid it" in line["last_problem"]
    env.frappe.session.user, env.frappe.user_roles = ACCOUNTS, {"Accounts User"}
    with pytest.raises(env.frappe.ValidationError,
                       match="M-Pesa may have paid row 1: MB2B-1 was cancelled after its send began"):
        env.schedule.plan_again("L1")
    env.frappe.session.user, env.frappe.user_roles = OTHER, {"Accounts Manager"}  # not a manager since D2
    with pytest.raises(env.frappe.ValidationError, match="A manager checks the statement"):
        env.schedule.plan_again("L1")
    env.frappe.session.user, env.frappe.user_roles = OTHER, {"M-Pesa Manager"}
    env.schedule.plan_again("L1")
    assert (line_of(env)["status"], line_of(env)["request"]) == ("Planned", None)


def test_the_shared_administrator_login_does_not_plan_again_a_line_that_may_have_been_paid(env):
    # decision 1: it holds every role, M-Pesa Manager too, but names nobody who checked the statement
    schedule_row(env)
    line_row(env, status="Sending", request="MB2B-1")
    env.request("MB2B-1", docstatus=2, status="Not Initiated", transaction_id=None, schedule_line="L1",
                payment_schedule="MPS-1", send_started_on=NOW - datetime.timedelta(minutes=2))
    env.schedule.sync_line("MB2B-1")
    env.frappe.session.user, env.frappe.user_roles = "Administrator", {"System Manager", "M-Pesa Manager"}
    with pytest.raises(env.frappe.PermissionError, match="Use your own login"):
        env.schedule.plan_again("L1")
    assert (line_of(env)["status"], line_of(env)["request"]) == ("Failed", "MB2B-1")


@pytest.mark.parametrize("call,args", [("move_date", (["L1"], TODAY.isoformat())), ("plan_again", ("L1",)),
                                       ("close_schedule", ("MPS-1",))])
def test_system_manager_does_not_prepare_or_change_schedules(env, call, args):
    # decision 1: PREPARE_ROLES, not FINANCE_ROLES: a moved or re-planned line is payable
    schedule_row(env)
    line_row(env, planned_date=day(3))
    env.frappe.session.user, env.frappe.user_roles = "it@example.com", {"System Manager"}
    with pytest.raises(env.frappe.PermissionError):
        getattr(env.schedule, call)(*args)
    assert (line_of(env)["planned_date"], line_of(env)["status"]) == (day(3), "Planned")


# ---------------------------------------------------------------- approximate balance
def test_available_takes_the_newest_balance_less_what_is_on_its_way(env):
    settings = b2b_fixture.SETTINGS
    env.frappe.db.table("Mpesa Settings")[settings]["account_balance"] = "Working Account|KES|9000.00|8000.00|0.00|0.00"
    env.frappe.db.table("Mpesa Balance Snapshot")["S1"] = {
        "name": "S1", "shortcode": "600999", "sub_account": "Working", "source": "Account Balance",
        "taken_at": NOW - datetime.timedelta(hours=3)}
    env.request("MB2B-P", status="Paid", transaction_id="R1", mpesa_settings=settings,
                trans_completed_time=NOW - datetime.timedelta(hours=1),
                debit_account_balance="Working Account|KES|6000.00|5500.00|0.00|0.00")
    env.request("MB2B-F", status="Initiated", transaction_id=None, amount=1200, mpesa_settings=settings,
                creation=NOW - datetime.timedelta(minutes=10))
    env.request("MB2B-E", status="Initiated", transaction_id=None, amount=900, mpesa_settings=settings,
                creation=NOW - datetime.timedelta(hours=2))  # before the balance: already in it
    b = env.balances.available(settings, "Working")
    assert (b.amount, b.source, b.pending, b.after, b.approximate) == (5500, "B2B Result", 1200, 4300, True)
    assert b.as_of == NOW - datetime.timedelta(hours=1)


def test_available_without_any_balance(env):
    b = env.balances.available(b2b_fixture.SETTINGS, "Working")
    assert (b.amount, b.after, b.pending) == (None, None, 0.0)


def test_parse_available_reads_the_available_column(env):
    assert env.balances.parse_available("Working Account|KES|700.00|650.50|0.00|0.00&Utility Account|KES|5.00") == {
        "Working": 650.5, "Utility": 5.0}


# ---------------------------------------------------------------- odds and ends
def test_only_a_schedules_request_queues_a_line_update(env):
    row = env.request("MB2B-1", status="Initiated", transaction_id=None, conversation_id="AG_1",
                      originator_conversation_id="OC_1")
    result = {"ResultType": 0, "ResultCode": 0, "ResultDesc": "ok", "ConversationID": "AG_1",
              "OriginatorConversationID": "OC_1", "TransactionID": "QRS1TUV2WX",
              "ResultParameters": {"ResultParameter": [{"Key": "TransCompletedTime", "Value": "20261018101500"}]}}
    env.b2b.handle_result(result, "MB2B-1")
    assert row["status"] == "Paid"
    assert not [j for j in env.frappe.enqueued if j[0].endswith("schedule.sync_line")]


def test_a_number_not_saved_on_a_supplier_is_paid_by_a_manager_only(env):
    env.frappe.db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"]["allow_one_off_b2b"] = 1
    schedule_row(env)
    line_row(env, party=None, party_type=None, payee_name="KPLC", payee_account=None, payee_account_doctype=None,
             one_off_type="Paybill", one_off_shortcode="888888", one_off_account="ACC-9", destination="888888 / ACC-9",
             reference_doctype=None, reference_name=None, entry_mode="Make the entry later")
    result = env.schedule.prepare_payment(["L1"])
    assert result["not_included"][0]["reason"] == (
        "A number not saved on a supplier is paid by an M-Pesa Manager only.")
    env.frappe.session.user, env.frappe.user_roles = OTHER, {"M-Pesa Manager"}
    answer = env.schedule.confirm_payment(prepared(env, "L1")["token"])
    request = env.frappe.db.table(B2B)[answer["sent"][0]["request"]]
    assert (request["receiver_shortcode"], request["account_reference"], request.get("supplier")) == (
        "888888", "ACC-9", None)
    assert request["entry_mode"] == "Make the entry later"


def test_a_closed_schedule_takes_no_new_lines(env):
    env.pi()
    schedule_row(env, closed=1)
    line_row(env, status="Cancelled")
    with pytest.raises(env.frappe.ValidationError, match="it cannot be changed"):
        env.schedule.validate_schedule(form(env, [dict(line_of(env), amount=10)]))
    env.frappe.db.table(SCHEDULE)["MPS-1"]["closed"] = 1
    doc = form(env, [dict(line_of(env)), new_line(name="N2")])
    env.frappe.db.table(SCHEDULE)["MPS-1"]["closed"] = 1
    with pytest.raises(env.frappe.ValidationError, match="MPS-1 is closed"):
        env.schedule.validate_schedule(doc)


def test_the_due_report_lists_due_lines_with_their_problem_and_the_balance_cards(env):
    report = importlib.import_module("mpsa_suite.mpsa_suite.report.m_pesa_payments_due.m_pesa_payments_due")
    env.pi(outstanding_amount=10000)
    schedule_row(env)
    line_row(env, "L1", planned_date=day(-2))
    line_row(env, "L2", planned_date=TODAY, prepared_by=PAYER)
    line_row(env, "L3", planned_date=day(3))
    line_row(env, "L4", status="Sent", request="MB2B-1")
    columns, data, _, _, cards = report.execute({"company": "Astral"})
    assert [d["line"] for d in data] == ["L1", "L2"]
    l1, l2 = data
    # what PINV-1 has left beside L1: L2 and L3 are planned on it too
    assert (l1["overdue"], l1["payable"], l1["problem_kind"], l1["left_to_pay"]) == (1, 1, "ok", 4000)
    assert (l2["payable"], l2["problem"]) == (0, "You prepared or last changed this line: another person must pay it.")
    labels = [c["label"] for c in cards]
    assert labels[0].startswith("Working Astral 600999 (no balance yet") and "Selected" in labels
    assert next(c for c in cards if c["label"].startswith("Due Now"))["value"] == 6000
    assert next(c for c in cards if c["label"].startswith("You Can Pay Now"))["value"] == 3000
    _, week, *_ = report.execute({"company": "Astral", "show": "Next 7 days"})
    assert [d["line"] for d in week] == ["L1", "L2", "L3"] and week[2]["problem"] == "Not due until 21 Oct 2026."
    _, busy, *_ = report.execute({"company": "Astral", "show": "In progress"})
    assert [d["line"] for d in busy] == ["L4"]
    assert {c["fieldname"] for c in columns} >= {"planned_date", "payee", "route", "destination", "reference_name",
                                                 "left_to_pay", "amount", "status", "problem", "prepared_by"}
