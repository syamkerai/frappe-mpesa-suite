"""B2C Payment Disbursement payouts, on an in-memory frappe.

Covers: the status check never saves the (possibly stale) form copy, so request links and
results written by the payout jobs survive, and a save after submit keeps them too, while
on_change and the Version still run; a Salary Slip's owed amount goes down by what B2C (also
a cancelled request, also one paid after the transaction's snapshot) or payroll banking
already paid, so a second payout is refused; rows outside Daraja's B2C limits are refused
before submit, and the send job marks such a row Failed instead of leaving it Initiated forever.
"""

import contextlib
import importlib
import logging
import sys
import types

import pytest

import fake_frappe

PARENT = "B2C Payment Disbursement"
REF = "B2C Payment Disbursement Reference"
REQ = "B2C Disbursement Request"
MODULE = "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement"
REF_MODULE = (
    "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement_reference."
    "b2c_payment_disbursement_reference"
)


class Q:
    """Chainable stand-in for frappe.qb / DocType: every call returns itself, run() pops a result."""

    def __init__(self, results=None):
        self.results = results if results is not None else []

    def __getattr__(self, key):
        return self

    def __call__(self, *a, **kw):
        return self

    def __eq__(self, other):
        return self

    def __gt__(self, other):
        return self

    def __ge__(self, other):
        return self

    def __le__(self, other):
        return self

    __hash__ = object.__hash__

    def run(self, as_dict=False):
        return self.results.pop(0)


def _fake_sql(frappe):
    """The few queries these code paths run, answered from the in-memory tables."""
    db, flt = frappe.db, frappe.utils.flt

    def sql(query, values=None, as_dict=False, **kw):
        q = " ".join(query.split())
        frappe.queries.append(q)
        submitted = (1, 2) if "docstatus IN (1, 2)" in q else (1,)
        if "JOIN `tabB2C Disbursement Request` req ON req.name = ref.b2c_disbursement_request" in q:
            (name,) = values  # the locking read: latest committed rows
            requests = db.table(REQ)
            return tuple(
                (req["name"], req.get("amount"))
                for ref in db.table(REF).values()
                if ref.get("reference_name") == name and ref.get("reference_doctype") == "Salary Slip"
                and (req := requests.get(ref.get("b2c_disbursement_request")))
                and req.get("docstatus") in submitted and req.get("status") == "Paid"
            )
        if q.startswith("SELECT name, amount FROM `tabB2C Disbursement Request`"):
            (name,) = values  # a plain read: this transaction's snapshot when the test set one
            requests = frappe.snapshot.get(REQ) if frappe.snapshot else db.table(REQ)
            return tuple(
                (r["name"], r.get("amount")) for r in requests.values()
                if r.get("docstatus") in submitted and r.get("status") == "Paid"
                and r.get("reference_doctype") == "Salary Slip" and r.get("reference_name") == name
            )
        if "FROM `tabM-Pesa Payment Schedule Line`" in q:
            return []  # nothing planned on a payment schedule
        if q.startswith("SELECT DISTINCT je.name FROM `tabJournal Entry` je"):
            # owed.payroll_bank_entries: live bank / cash entries of the payroll not made by M-Pesa
            (payroll_entry,) = values
            found = set()
            for row in db.table("Journal Entry Account").values():
                je = db.table("Journal Entry").get(row["parent"]) or {}
                tagged = any(r.get("b2c_payment_disbursement") or r.get("mpesa_b2b_payment_request")
                             for r in db.table("Journal Entry Account").values() if r["parent"] == row["parent"])
                if (je.get("docstatus", 0) < 2 and je.get("voucher_type") in ("Bank Entry", "Cash Entry")
                        and row.get("reference_type") == "Payroll Entry"
                        and row.get("reference_name") == payroll_entry and not tagged):
                    found.add(row["parent"])
            return [(n,) for n in sorted(found)]
        if "FROM `tabJournal Entry Account` jea" in q:
            payroll_entry, employee = values
            skip_b2c = "b2c_payment_disbursement" in q
            totals = {}  # slip_bank_payments: each entry with what it debits to the employee
            for row in db.table("Journal Entry Account").values():
                je = db.table("Journal Entry").get(row["parent"]) or {}
                if (je.get("docstatus") == 1 and je.get("voucher_type") in ("Bank Entry", "Cash Entry")
                        and row.get("reference_type") == "Payroll Entry"
                        and row.get("reference_name") == payroll_entry
                        and row.get("party_type") == "Employee" and row.get("party") == employee
                        and flt(row.get("debit_in_account_currency")) > 0
                        and not (skip_b2c and row.get("b2c_payment_disbursement"))):
                    totals[row["parent"]] = totals.get(row["parent"], 0.0) + flt(row.get("debit_in_account_currency"))
            return [fake_frappe._dict(name=n, voucher_type="Bank Entry", cheque_no=None, posting_date=None, amount=a)
                    for n, a in sorted(totals.items())]
        if "FROM `tabB2C Payment Disbursement Reference`" in q:
            parent, parenttype = values
            return [fake_frappe._dict(r) for r in db.table(REF).values()
                    if r.get("parent") == parent and r.get("parenttype") == parenttype]
        raise AssertionError(f"unexpected SQL: {q}")

    return sql


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    query_builder = types.ModuleType("frappe.query_builder")
    query_builder.DocType = lambda name: Q()
    saved_qb = sys.modules.get("frappe.query_builder")
    sys.modules["frappe.query_builder"] = query_builder
    try:
        frappe.utils.now = lambda: "2026-10-18 12:00:00"
        frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
        frappe.logger = lambda *a, **kw: logging.getLogger("fake_frappe")
        frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
        frappe.db.db_type = "mariadb"
        frappe.queries, frappe.saved, frappe.notified, frappe.events = [], [], [], []
        frappe.snapshot = None
        frappe.db.sql = _fake_sql(frappe)
        frappe.qb = Q()
        mod = importlib.import_module(MODULE)
        common = importlib.import_module("mpsa_suite.money_in.common")
        monkeypatch.setattr(common, "named_lock", lambda key, timeout=10, **kw: contextlib.nullcontext())
        Doc = _doc_class(frappe, mod)
        get_doc = frappe.get_doc
        frappe.get_doc = lambda d, *a, **kw: (
            Doc(**{k: v for k, v in d.items() if k != "doctype"})
            if isinstance(d, dict) and d.get("doctype") == PARENT else get_doc(d, *a, **kw)
        )
        yield fake_frappe._dict(
            frappe=frappe,
            mod=mod,
            ref_mod=importlib.import_module(REF_MODULE),
            payouts=importlib.import_module("mpsa_suite.utils.payouts"),
            Doc=Doc,
        )
    finally:
        if saved_qb is None:
            sys.modules.pop("frappe.query_builder", None)
        else:
            sys.modules["frappe.query_builder"] = saved_qb
        fake_frappe.uninstall(saved)


def _doc_class(frappe, mod):
    class Doc(mod.B2CPaymentDisbursement):
        """The controller on a plain object, with what Frappe's Document does around it:
        save() writes every row back, db_set writes only the fields it is given."""

        doctype = PARENT
        _doc_before_save = None

        def __init__(self, **fields):
            self.__dict__.update(fields)

        def get(self, key, default=None):
            return self.__dict__.get(key, default)

        def update(self, values):
            self.__dict__.update(values)

        def as_dict(self):
            fields = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
            return dict(fields, doctype=self.doctype, references=[dict(r) for r in self.references])

        def reload(self):
            self.__dict__.update(frappe.db.table(PARENT)[self.name])
            rows = [r for r in frappe.db.table(REF).values() if r.get("parent") == self.name]
            self.references = [fake_frappe._dict(r) for r in sorted(rows, key=lambda r: r["idx"])]

        def db_set(self, values, notify=False):
            # Frappe's db_set loads the document for update (every row locked) unless it
            # already has a before-image
            assert self._doc_before_save is not None, "db_set would lock every row"
            self.update(values)
            self.modified = frappe.utils.now()
            self.run_method("before_change")
            frappe.db.set_value(self.doctype, self.name, dict(values, modified=self.modified))
            self.run_method("on_change")
            if notify:
                self.notify_update()

        def run_method(self, method):
            frappe.events.append((method, self._doc_before_save.status, self.status))

        def save_version(self):
            before = self._doc_before_save
            frappe.events.append(("version", before.retry_count, self.retry_count,
                                  [r["name"] for r in before.references] == [r["name"] for r in self.references]))

        def notify_update(self):
            frappe.notified.append((self.name, self.modified))

        def save(self, *a, **kw):
            frappe.saved.append(self.name)
            for row in self.references:
                frappe.db.table(REF)[row.name].update(dict(row))

    return Doc


# ---------------------------------------------------------------- C10: status check on a stale copy
def _sent_batch(env, statuses=("Paid", "Initiated", "Initiated")):
    """A submitted batch as the database has it after the payout jobs ran, and the form's copy from submit."""
    f = env.frappe
    f.db.table(PARENT)["D1"] = {"name": "D1", "docstatus": 1, "status": "Initiated", "retry_count": 2}
    stale_rows = []
    for i, status in enumerate(statuses, 1):
        f.db.table(REF)[f"R{i}"] = {
            "name": f"R{i}", "parent": "D1", "parenttype": PARENT, "parentfield": "references", "idx": i,
            "b2c_disbursement_request": f"B2C-{i}", "payment_status": status,
            "reference_no": "SKL1234567" if status == "Paid" else None,
            "reference_date": "2026-10-18" if status == "Paid" else None,
        }
        stale_rows.append(fake_frappe._dict(
            name=f"R{i}", parent="D1", parenttype=PARENT, parentfield="references", idx=i,
            b2c_disbursement_request=None, payment_status="Initiated", reference_no=None, reference_date=None,
        ))
    return env.Doc(name="D1", status="Initiated", retry_count=0, modified="2026-10-18 10:00:00",
                   references=stale_rows)


def test_status_check_on_stale_form_copy_keeps_request_links(env):
    stale = _sent_batch(env)
    message = stale.update_disbursement_status()

    rows = env.frappe.db.table(REF)
    assert [rows[f"R{i}"]["b2c_disbursement_request"] for i in (1, 2, 3)] == ["B2C-1", "B2C-2", "B2C-3"]
    assert rows["R1"]["payment_status"] == "Paid" and rows["R1"]["reference_no"] == "SKL1234567"
    assert env.frappe.saved == []
    parent = env.frappe.db.table(PARENT)["D1"]
    assert parent["status"] == "Partially Paid"  # counted from the database, not the stale rows
    assert parent["retry_count"] == 3  # 2 in the database, 0 in the stale copy
    assert parent["last_status_check"] == "2026-10-18 12:00:00"
    assert "from 'Initiated' to 'Partially Paid'" in message and "1 Paid, 0 Failed, 3 Total" in message
    assert "Retry #3" in message
    # open forms are told the new timestamp, so they reload instead of keeping their old rows
    assert env.frappe.notified == [("D1", "2026-10-18 12:00:00")]
    # only the parent row was locked; the rows the payout jobs write to were not
    assert env.frappe.db.locks == [(PARENT, "D1")]


def test_status_check_runs_change_events_without_a_save(env):
    stale = _sent_batch(env)
    stale.update_disbursement_status()

    # what a save ran after writing (Value Change notifications, webhooks, Version), with the
    # database's old values as the before-image
    assert env.frappe.events == [
        ("before_change", "Initiated", "Partially Paid"),
        ("on_change", "Initiated", "Partially Paid"),
        ("version", 2, 3, True),  # same rows on both sides: only the parent fields differ
    ]
    # the copy the form gets back carries the database's rows, not the ones it sent
    assert [r.b2c_disbursement_request for r in stale.references] == ["B2C-1", "B2C-2", "B2C-3"]
    assert env.frappe.saved == []


def test_status_check_counts_results_written_after_the_copy(env):
    stale = _sent_batch(env, statuses=("Paid", "Paid", "Paid"))
    stale.update_disbursement_status()
    assert env.frappe.db.table(PARENT)["D1"]["status"] == "Paid"

    env.frappe.db.table(PARENT)["D1"]["retry_count"] = 3
    message = stale.update_disbursement_status()
    assert "No status change (still 'Paid')" in message and "Retry #4" in message


def test_status_check_failed_and_unsent(env):
    stale = _sent_batch(env, statuses=("Failed", "Failed"))
    stale.update_disbursement_status()
    assert env.frappe.db.table(PARENT)["D1"]["status"] == "Failed"


def test_save_after_submit_keeps_what_the_jobs_wrote(env):
    stale = _sent_batch(env)
    stale.before_update_after_submit()

    by_name = {row.name: row for row in stale.references}
    assert by_name["R1"].b2c_disbursement_request == "B2C-1" and by_name["R1"].payment_status == "Paid"
    assert by_name["R1"].reference_no == "SKL1234567" and by_name["R1"].reference_date == "2026-10-18"
    assert by_name["R3"].b2c_disbursement_request == "B2C-3"
    assert any("FOR UPDATE" in q for q in env.frappe.queries)  # latest committed values, not a snapshot

    stale.save()  # Frappe then writes the rows back: now with the database's values
    assert env.frappe.db.table(REF)["R2"]["b2c_disbursement_request"] == "B2C-2"


def test_save_after_submit_leaves_new_rows_alone(env):
    stale = _sent_batch(env)
    stale.references.append(fake_frappe._dict(name="NEW", payment_status="Not Initiated",
                                              b2c_disbursement_request=None))
    stale.before_update_after_submit()
    assert stale.references[-1].payment_status == "Not Initiated"


# ---------------------------------------------------------------- C12: Salary Slip owed after payment
def test_owed_salary_slip_subtracts_what_was_paid(env):
    owed = env.payouts.owed
    slip = {"rounded_total": 45123, "net_pay": 45123.40}
    assert owed("Salary Slip", slip) == 45123
    assert owed("Salary Slip", slip, paid=45123) == 0
    assert owed("Salary Slip", slip, paid=20000) == 25123
    assert owed("Salary Slip", slip, paid=50000) == 0
    assert owed("Salary Slip", {"net_pay": 45000}, paid=None) == 45000
    # the other doctypes' fields already go down when they are paid
    assert owed("Purchase Invoice", {"outstanding_amount": 1200}, paid=500) == 1200


def _slip(env, **kw):
    row = {"name": "SS-1", "docstatus": 1, "rounded_total": 45123, "net_pay": 45123.40,
           "employee": "EMP-1", "payroll_entry": "PE-1"}
    row.update(kw)
    env.frappe.db.table("Salary Slip")[row["name"]] = row


def _b2c_paid(env, name, amount, slip="SS-1", **kw):
    row = {"name": name, "docstatus": 1, "status": "Paid", "amount": amount, "accounting_booked": 1,
           "reference_doctype": "Salary Slip", "reference_name": slip}
    row.update(kw)
    env.frappe.db.table(REQ)[name] = row


def _bank_entry(env, name, debit, employee="EMP-1", b2c=None, docstatus=1, voucher_type="Bank Entry"):
    env.frappe.db.table("Journal Entry")[name] = {"name": name, "docstatus": docstatus, "voucher_type": voucher_type}
    env.frappe.db.table("Journal Entry Account")[name + "-1"] = {
        "name": name + "-1", "parent": name, "reference_type": "Payroll Entry", "reference_name": "PE-1",
        "party_type": "Employee", "party": employee, "debit_in_account_currency": debit,
        "b2c_payment_disbursement": b2c,
    }


def _second_disbursement(env, allocated, slip="SS-1"):
    row = fake_frappe._dict(name="D2-R1", idx=1, reference_doctype="Salary Slip", reference_name=slip,
                            allocated_amount=allocated, b2c_disbursement_request=None)
    return env.Doc(name="D2", references=[row])


def test_salary_slip_paid_and_booked_refuses_a_second_payout(env, monkeypatch):
    # D1 paid the slip and its Journal Entry is booked, so open_payouts no longer lists it
    monkeypatch.setattr(env.mod, "open_payouts", lambda *a, **kw: [])
    _slip(env)
    _b2c_paid(env, "B2C-1", 45123)
    _bank_entry(env, "JV-B2C", 45123, b2c="D1")  # the B2C flow's own entry is not counted twice

    assert env.mod.current_outstanding("Salary Slip", "SS-1") == 0
    with pytest.raises(env.frappe.ValidationError, match="owes 0.0; this disbursement allocates 45123.0"):
        _second_disbursement(env, 45123).validate_no_double_payout()


def test_salary_slip_unbooked_paid_payout_past_retry_window_still_counts(env, monkeypatch):
    monkeypatch.setattr(env.mod, "open_payouts", lambda *a, **kw: [])  # older than RETRY_WINDOW_DAYS
    _slip(env)
    _b2c_paid(env, "B2C-1", 45123, accounting_booked=0)
    with pytest.raises(env.frappe.ValidationError, match="Allocated More Than Outstanding|owes 0.0"):
        _second_disbursement(env, 45123).validate_no_double_payout()


def test_salary_slip_partial_payout_allows_only_the_rest(env, monkeypatch):
    monkeypatch.setattr(env.mod, "open_payouts", lambda *a, **kw: [])
    _slip(env)
    _b2c_paid(env, "B2C-1", 20000)
    _bank_entry(env, "JV-B2C", 20000, b2c="D1")
    _b2c_paid(env, "B2C-2", 7000, status="Failed")
    _b2c_paid(env, "B2C-3", 5000, docstatus=0, status="Not Initiated")
    _b2c_paid(env, "B2C-4", 5000, slip="SS-OTHER")

    assert env.mod.current_outstanding("Salary Slip", "SS-1") == 25123
    _second_disbursement(env, 25123).validate_no_double_payout()
    with pytest.raises(env.frappe.ValidationError, match="owes 25123.0"):
        _second_disbursement(env, 25124).validate_no_double_payout()


def test_salary_slip_payout_still_counts_after_its_request_is_cancelled(env, monkeypatch):
    # D1 paid the slip, then staff cancelled D1 with Cancel All: its request, its Journal Entry
    # and D1 itself are cancelled, and open_payouts drops the row. The money is still gone.
    monkeypatch.setattr(env.mod, "open_payouts", lambda *a, **kw: [])
    _slip(env)
    _b2c_paid(env, "B2C-1", 45123, docstatus=2)
    _bank_entry(env, "JV-B2C", 45123, b2c="D1", docstatus=2)

    assert env.mod.current_outstanding("Salary Slip", "SS-1") == 0
    with pytest.raises(env.frappe.ValidationError, match="owes 0.0"):
        _second_disbursement(env, 45123).validate_no_double_payout()


def test_salary_slip_payout_paid_after_the_snapshot_is_seen_by_the_locked_check(env, monkeypatch):
    # D2's transaction read the tables while D1's payout was still Initiated. Before D2 got the
    # lock, the Result marked it Paid and the Journal Entry was booked, so open_payouts (a
    # locking read) drops it while a plain read still sees it Initiated.
    monkeypatch.setattr(env.mod, "open_payouts", lambda *a, **kw: [])
    _slip(env)
    _b2c_paid(env, "B2C-1", 45123, status="Initiated", accounting_booked=0)
    env.frappe.snapshot = {REQ: {k: dict(v) for k, v in env.frappe.db.table(REQ).items()}}
    env.frappe.db.table(REQ)["B2C-1"].update(status="Paid", accounting_booked=1)
    env.frappe.db.table(REF)["D1-R1"] = {"name": "D1-R1", "parent": "D1", "reference_doctype": "Salary Slip",
                                         "reference_name": "SS-1", "b2c_disbursement_request": "B2C-1"}

    assert env.mod.current_outstanding("Salary Slip", "SS-1") == 45123  # the snapshot alone
    env.frappe.queries.clear()
    with pytest.raises(env.frappe.ValidationError, match="owes 0.0"):
        _second_disbursement(env, 45123).validate_no_double_payout()
    assert any("JOIN `tabB2C Disbursement Request` req" in q and q.endswith("FOR UPDATE")
               for q in env.frappe.queries)


def test_salary_slip_payout_found_twice_counts_once(env):
    # an amended copy of D1's row links the same request; the plain read finds it too
    _slip(env)
    _b2c_paid(env, "B2C-1", 20000)
    for row in ("D1-R1", "D1-1-R1"):
        env.frappe.db.table(REF)[row] = {"name": row, "reference_doctype": "Salary Slip",
                                         "reference_name": "SS-1", "b2c_disbursement_request": "B2C-1"}
    assert env.mod.current_outstanding("Salary Slip", "SS-1", for_update=True) == 25123


def test_salary_slip_paid_by_payroll_bank_entry_refuses_b2c(env, monkeypatch):
    monkeypatch.setattr(env.mod, "open_payouts", lambda *a, **kw: [])
    _slip(env)
    _bank_entry(env, "JV-BANK", 45123.40)
    _bank_entry(env, "JV-DRAFT", 45123.40, docstatus=0)
    _bank_entry(env, "JV-ACCRUAL", 45123.40, voucher_type="Journal Entry")
    _bank_entry(env, "JV-OTHER", 45123.40, employee="EMP-2")
    assert env.mod.salary_slip_paid("SS-1", "EMP-1", "PE-1") == 45123.40
    # the payroll has bank entries not made by M-Pesa (a draft too): refused before what is owed
    with pytest.raises(env.frappe.ValidationError,
                       match=r"PE-1 has a bank or cash entry not made by M-Pesa \(JV-BANK, JV-DRAFT, JV-OTHER\)"):
        _second_disbursement(env, 45123).validate_no_double_payout()


@pytest.mark.parametrize("docstatus, party", [(0, "EMP-1"), (1, None)])
def test_a_payroll_bank_entry_hrms_left_as_a_draft_or_without_parties_refuses_b2c(env, monkeypatch, docstatus, party):
    # HRMS's Make Bank Entry saves a draft, and with employee-wise accounting off (the default) its
    # payable row names no employee: salary_slip_paid cannot see it, the payroll check does
    monkeypatch.setattr(env.mod, "open_payouts", lambda *a, **kw: [])
    _slip(env)
    _bank_entry(env, "JV-HRMS", 45123.40, docstatus=docstatus)
    if party is None:
        env.frappe.db.table("Journal Entry Account")["JV-HRMS-1"].update(party_type=None, party=None)
    assert env.mod.salary_slip_paid("SS-1", "EMP-1", "PE-1") == 0
    with pytest.raises(env.frappe.ValidationError, match=r"PE-1 has a bank or cash entry not made by M-Pesa"):
        _second_disbursement(env, 45123).validate_no_double_payout()
    env.frappe.db.table("Journal Entry")["JV-HRMS"]["docstatus"] = 2  # cancelled: paid by M-Pesa again
    _second_disbursement(env, 45123).validate_no_double_payout()


def test_salary_slip_without_payroll_entry_counts_b2c_only(env):
    _slip(env, payroll_entry=None)
    _b2c_paid(env, "B2C-1", 10000)
    _bank_entry(env, "JV-BANK", 45123.40)
    assert env.mod.current_outstanding("Salary Slip", "SS-1") == 35123


def _reads_slips(env, monkeypatch, denied=()):
    """frappe.has_permission on Salary Slips: the user reads every slip but ``denied``."""
    asked = []

    def has_permission(doctype, ptype="read", doc=None, throw=False, **kw):
        asked.append((doctype, ptype, doc))
        allowed = doc not in denied
        if throw and not allowed:
            raise env.frappe.PermissionError(f"No {ptype} on {doctype} {doc}")
        return allowed

    monkeypatch.setattr(env.frappe, "has_permission", has_permission, raising=False)
    return asked


def test_get_references_skips_slips_b2c_already_paid(env, monkeypatch):
    # SS-1 was paid by B2C: its Journal Entry debits 45,123, which never equals net_pay 45,123.40
    _reads_slips(env, monkeypatch)
    _slip(env)
    _slip(env, name="SS-2", employee="EMP-2", rounded_total=30000, net_pay=30000)
    _b2c_paid(env, "B2C-1", 45123)
    slips = [fake_frappe._dict(name="SS-1", employee="EMP-1", net_pay=45123.40),
             fake_frappe._dict(name="SS-2", employee="EMP-2", net_pay=30000)]
    env.frappe.qb.results[:] = [slips, [fake_frappe._dict(name="JV-B2C")],
                                [fake_frappe._dict(party="EMP-1", debit_in_account_currency=45123)]]

    found = env.Doc(name="D2")._fetch_unpaid_salary_slips("Salary Slip", {"payroll_entry": "PE-1"}, {})
    assert [s.name for s in found] == ["SS-2"]


def test_get_references_lists_only_the_slips_the_user_may_read(env, monkeypatch):
    # final review (decision D4): each slip is read by its own rights (HRMS, User Permissions)
    asked = _reads_slips(env, monkeypatch, denied={"SS-1"})
    _slip(env)
    _slip(env, name="SS-2", employee="EMP-2", rounded_total=30000, net_pay=30000)
    slips = [fake_frappe._dict(name="SS-1", employee="EMP-1", net_pay=45123.40),
             fake_frappe._dict(name="SS-2", employee="EMP-2", net_pay=30000)]
    env.frappe.qb.results[:] = [slips, []]
    found = env.Doc(name="D2")._fetch_unpaid_salary_slips("Salary Slip", {"payroll_entry": "PE-1"}, {})
    assert [s.name for s in found] == ["SS-2"]
    assert ("Salary Slip", "read", "SS-1") in asked


def test_salary_references_are_for_who_may_see_salaries_and_read_the_payroll(env, monkeypatch):
    # final review (decision D4): an Accounts Manager (prepare rights on the disbursement, no HR role) is
    # refused Get References for Salary Slips (each employee's net pay and phone) before anything is read
    payroll = importlib.import_module("mpsa_suite.money_out.payroll")
    seen = {"salaries": False}

    def require_salary_reader():
        if not seen["salaries"]:
            raise env.frappe.PermissionError(payroll.SALARIES_HIDDEN)

    monkeypatch.setattr(payroll, "require_salary_reader", require_salary_reader)
    asked = _reads_slips(env, monkeypatch, denied={"PE-9"})
    fetched = []
    monkeypatch.setattr(env.Doc, "_check_can_prepare", lambda self: None)
    monkeypatch.setattr(env.Doc, "_fetch_entries", lambda self, *a: fetched.append(a[0]) or [])
    doc = env.Doc(name="D2")
    args = {"transaction_to_pay_against": "Salary Slip", "payroll_entry": "PE-1", "company": "Astral"}
    with pytest.raises(env.frappe.PermissionError, match="salaries are shown only"):
        doc.get_outstanding_reference_documents(args)
    seen["salaries"] = True
    with pytest.raises(env.frappe.PermissionError, match="No read on Payroll Entry PE-9"):
        doc.get_outstanding_reference_documents(dict(args, payroll_entry="PE-9"))
    assert fetched == []
    assert doc.get_outstanding_reference_documents(args) == [] and fetched == ["Salary Slip"]
    assert ("Payroll Entry", "read", "PE-1") in asked
    seen["salaries"] = False  # an invoice's references ask nothing of salaries
    doc.get_outstanding_reference_documents({"transaction_to_pay_against": "Purchase Invoice", "company": "Astral"})
    assert fetched[-1] == "Purchase Invoice"


def test_a_salary_row_is_saved_only_by_who_may_see_salaries_and_read_its_slip(env, monkeypatch):
    payroll = importlib.import_module("mpsa_suite.money_out.payroll")
    seen = {"salaries": False}

    def require_salary_reader():
        if not seen["salaries"]:
            raise env.frappe.PermissionError(payroll.SALARIES_HIDDEN)

    monkeypatch.setattr(payroll, "require_salary_reader", require_salary_reader)
    _reads_slips(env, monkeypatch, denied={"SS-9"})
    row = fake_frappe._dict(reference_doctype="Salary Slip", reference_name="SS-1")
    doc = env.Doc(name="D2", docstatus=0, transaction_to_pay_against="Salary Slip", references=[row],
                  flags=fake_frappe._dict())
    with pytest.raises(env.frappe.PermissionError, match="salaries are shown only"):
        doc.check_salary_reader()
    seen["salaries"] = True
    doc.check_salary_reader()
    row.reference_name = "SS-9"
    with pytest.raises(env.frappe.PermissionError, match="No read on Salary Slip SS-9"):
        doc.check_salary_reader()
    seen["salaries"] = False
    doc.docstatus = 1  # the send: the payer's roles see salaries, and HRMS gives them no slip
    doc.check_salary_reader()
    doc.docstatus = 0
    doc.flags.mpesa_from_schedule = True  # Pay Selected checked its preparer
    doc.check_salary_reader()
    claims = env.Doc(name="D3", docstatus=0, transaction_to_pay_against="Expense Claim", flags=fake_frappe._dict(),
                     references=[fake_frappe._dict(reference_doctype="Expense Claim", reference_name="EC-1")])
    claims.check_salary_reader()  # not salary data


def test_get_references_without_bank_entries_still_skips_paid_slips(env, monkeypatch):
    _reads_slips(env, monkeypatch)
    _slip(env)
    _b2c_paid(env, "B2C-1", 45123, accounting_booked=0)  # paid, entry not made yet
    env.frappe.qb.results[:] = [[fake_frappe._dict(name="SS-1", employee="EMP-1", net_pay=45123.40)], []]
    assert env.Doc(name="D2")._fetch_unpaid_salary_slips("Salary Slip", {"payroll_entry": "PE-1"}, {}) == []
    assert any("fully paid" in m for m in env.frappe.messages)


# ---------------------------------------------------------------- C14: Daraja B2C limits on the rows
@pytest.mark.parametrize("amount, problem", [
    (12345.50, "whole shillings"),
    (250001, "10 to 250,000"),
    (9, "10 to 250,000"),
    (12345, None),
    (10, None),
    (250000, None),
])
def test_b2c_amount_problem(env, amount, problem):
    found = env.ref_mod.b2c_amount_problem(amount)
    assert (found is None) if problem is None else (problem in found)


def _row(env, **kw):
    row = env.ref_mod.B2CPaymentDisbursementReference()
    row.__dict__.update({"idx": 3, "party": "EMP-1", "partyb": "0712345678", "allocated_amount": 12345,
                         "outstanding_amount": 300000})
    row.__dict__.update(kw)
    return row


@pytest.mark.parametrize("amount, msg", [(12345.50, "Row #3: M-Pesa B2C pays whole shillings"),
                                         (250001, "Row #3: M-Pesa B2C pays Kshs. 10 to 250,000")])
def test_row_outside_b2c_limits_is_refused_before_submit(env, amount, msg):
    with pytest.raises(env.frappe.ValidationError, match=msg):
        _row(env, allocated_amount=amount).validate()


def test_row_inside_b2c_limits_passes(env):
    row = _row(env)
    row.validate()
    assert row.partyb == "254712345678"


def _job_ref(env, allocated):
    env.frappe.db.table(REF)["R1"] = {"name": "R1", "payment_status": "Initiated", "b2c_disbursement_request": None}
    return fake_frappe._dict(doctype=REF, name="R1", idx=1, allocated_amount=allocated,
                             reference_doctype="Purchase Invoice", reference_name="PINV-1",
                             party_type="Supplier", party="SUP-1", partyb="254712345678")


def test_send_job_marks_row_outside_limits_failed_and_sends_nothing(env, monkeypatch):
    created = []
    monkeypatch.setattr(env.frappe, "get_doc", lambda *a, **kw: created.append(a))
    ref = _job_ref(env, 12345.50)

    env.mod.create_b2c_request(ref, types.SimpleNamespace(name="Astral 600999"), types.SimpleNamespace(name="D1"))

    assert created == []  # no request inserted, nothing sent
    assert env.frappe.db.table(REF)["R1"]["payment_status"] == "Failed"  # no longer a live payout
    ((_args, kw),) = env.frappe.errors
    assert "D1 row 1" in kw["title"] and "whole shillings" in kw["message"] and "PINV-1" in kw["message"]


def test_send_job_creates_request_for_valid_amount(env, monkeypatch):
    created = []

    def get_doc(data, *a, **kw):
        created.append(data)
        raise RuntimeError("stop before sending")

    monkeypatch.setattr(env.frappe, "get_doc", get_doc)
    with pytest.raises(env.frappe.ValidationError, match="stop before sending"):
        env.mod.create_b2c_request(_job_ref(env, 12345), types.SimpleNamespace(name="Astral 600999"),
                                   types.SimpleNamespace(name="D1"))
    assert created[0]["amount"] == 12345 and created[0]["b2c_payment_disbursement_reference"] == "R1"
    assert env.frappe.db.table(REF)["R1"]["payment_status"] == "Initiated"
