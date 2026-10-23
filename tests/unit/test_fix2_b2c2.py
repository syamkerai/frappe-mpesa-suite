"""Round 2 "b2c2" fixes to B2C payout booking and retry, on an in-memory frappe.

C10: a B2C Result (and the accounting sweep, which replays it) writes the row's link to its
request again, so a row whose link an old full save wiped is booked; a cancelled request's
late result never takes a row over, though it fills an empty link no submitted request names.
The one-off patch restores such links from the requests (submitted first, else cancelled).
C11: a Salary Slip payout's Journal Entry carries the payout's receipt as Reference No with a
Reference Date, so ERPNext can submit the Bank Entry; a Payment Entry carrying another payout's
receipt never stands in for a sibling payout (a Journal Entry stamped so still may). C13: the
retry guard checks that a payout marked booked still has its entry, counts a payout cancelled
with its disbursement or whose row lost its link, counts each payout once (an amend copies the
row's link), reads with locks, and on a site without ERPNext never queries Payment Entry tables.
"""

import contextlib
import datetime
import importlib
import logging
import re
import sys
import types

import pytest

import fake_frappe

PARENT = "B2C Payment Disbursement"
REF = "B2C Payment Disbursement Reference"
REQ = "B2C Disbursement Request"
PE, PER = "Payment Entry", "Payment Entry Reference"
JE, JEA = "Journal Entry", "Journal Entry Account"
RESP = "mpsa_suite.services.b2c_response_service"
POST = "mpsa_suite.services.b2c_postprocess_service"
JOURNAL = "mpsa_suite.accounting.journal_service"
PATCH = "mpsa_suite.mpsa_suite.patches.relink_b2c_reference_rows"
REQ_MODULE = "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request"
DISB_MODULE = "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement"


@contextlib.contextmanager
def _stub_modules(**modules):
    saved = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@pytest.fixture
def frappe():
    frappe, saved = fake_frappe.install()
    try:
        frappe.db.commit = lambda: None
        frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
        yield frappe
    finally:
        fake_frappe.uninstall(saved)


# ---------------------------------------------------------------- booking (C10 heal, C11)
def _entry_sql(frappe):
    """Answers booked_docstatus' entry queries from the in-memory entry tables, by evaluating
    the query's own ``alias.column = %s`` / ``IN %s`` conditions (case-insensitive, as MariaDB
    compares) and returning the columns it selects."""

    def same(a, b):
        return str(a).upper() == str(b).upper() if isinstance(a, str) and isinstance(b, str) else a == b

    def sql(query, values=None, **kw):
        q = " ".join(query.split())
        if "`tabPayment Entry" in q:
            parent, child, p, c = PE, PER, "pe", "per"
        elif "`tabJournal Entry" in q:
            parent, child, p, c = JE, JEA, "je", "jea"
        else:
            raise AssertionError(f"unexpected SQL: {q}")
        select = q.split("SELECT ", 1)[1].split(" FROM ", 1)[0].replace("DISTINCT ", "")
        columns = [column.strip() for column in select.split(",")]
        where = q.split(" WHERE ", 1)[1]
        params = re.findall(r"(\w+)\.(\w+) (=|IN) %s", where)
        assert len(params) == len(values), q
        found = []
        for row in frappe.db.table(child).values():
            head = frappe.db.table(parent).get(row["parent"])
            source = {p: head, c: row}
            if not head or head["docstatus"] >= 2 or not all(
                same(source[a].get(col), v) if op == "=" else any(same(source[a].get(col), x) for x in v)
                for (a, col, op), v in zip(params, values)
            ):
                continue
            found.append(source)
        if columns == [f"MAX({p}.docstatus)"]:
            return [(max((f[p]["docstatus"] for f in found), default=None),)]
        rows = [tuple(f[a].get(col) for a, col in (column.split(".") for column in columns)) for f in found]
        return list(dict.fromkeys(rows))  # DISTINCT

    return sql


@pytest.fixture
def book(frappe):
    seq = iter(range(1, 100))

    def add_entry(parent, child, head, rows):
        name = f"{parent[:2].upper()}-{next(seq)}"
        frappe.db.table(parent)[name] = dict(head, name=name)
        for i, row in enumerate(rows, 1):
            frappe.db.table(child)[f"{name}-{i}"] = dict(row, name=f"{name}-{i}", parent=name)
        return name

    def create_payment_entry_for_doc(disb, ref, submit=False):
        # what accounting.payment_entry_service writes: the payout's own receipt
        add_entry(
            PE,
            PER,
            {"docstatus": 1 if submit else 0, "reference_no": ref.reference_no},
            [{"reference_doctype": ref.reference_doctype, "reference_name": ref.reference_name,
              "allocated_amount": ref.allocated_amount, "b2c_payment_disbursement": disb.name}],
        )

    def create_journal_entry(disb, ref, submit=False):
        add_entry(
            JE,
            JEA,
            {"docstatus": 1 if submit else 0, "voucher_type": "Bank Entry", "cheque_no": ref.reference_no},
            [{"account": "MPESA", "credit_in_account_currency": ref.allocated_amount},
             {"account": "PAYABLE", "debit_in_account_currency": ref.allocated_amount, "party": ref.party,
              "b2c_payment_disbursement": disb.name}],
        )

    journal = types.ModuleType(JOURNAL)
    journal.create_journal_entry = create_journal_entry
    payment = types.ModuleType("mpsa_suite.accounting.payment_entry_service")
    payment.create_payment_entry_for_doc = create_payment_entry_for_doc
    with _stub_modules(**{journal.__name__: journal, payment.__name__: payment}):
        frappe.db.sql = _entry_sql(frappe)
        frappe.db.table("Mpesa Settings")["MS-1"] = {"name": "MS-1", "submit_b2c_accounting_entries": 1}
        frappe.db.table(PARENT)["D-1"] = {"name": "D-1"}
        yield fake_frappe._dict(
            frappe=frappe, post=importlib.import_module(POST), resp=importlib.import_module(RESP), add_entry=add_entry
        )


def _row(env, i, receipt, link="", status="Paid", doctype="Purchase Invoice", name="PI-1", amount=100,
         party="SUP-1", paid_row=True, docstatus=1):
    """Disbursement row ``R<i>`` and its request ``B2C-<i>``; ``link`` None: wiped on the row."""
    env.frappe.db.table(REF)[f"R{i}"] = {
        "name": f"R{i}", "parent": "D-1", "parenttype": PARENT, "idx": i,
        "reference_doctype": doctype, "reference_name": name, "party_type": "Supplier", "party": party,
        "allocated_amount": amount, "b2c_disbursement_request": f"B2C-{i}" if link == "" else link,
        "payment_status": status if paid_row else "Initiated", "reference_no": receipt if paid_row else None,
    }
    env.frappe.db.table(REQ)[f"B2C-{i}"] = {
        "name": f"B2C-{i}", "docstatus": docstatus, "status": status, "mpesa_settings": "MS-1",
        "accounting_booked": 0, "accounting_error": None, "transaction_id": receipt,
        "transaction_completed_datetime": datetime.datetime(2026, 10, 20, 9, 30),
        "b2c_payment_disbursement": "D-1", "b2c_payment_disbursement_reference": f"R{i}",
    }


def _receipts(env, doctype=PE):
    field = "reference_no" if doctype == PE else "cheque_no"
    return sorted(e[field] or "" for e in env.frappe.db.table(doctype).values())


def test_result_restores_a_wiped_row_link_and_books_the_payout(book):
    # the old full save wiped the row's link; the Result (or the sweep replaying it) arrives
    _row(book, 1, "SKT1AAAAAA", link=None, paid_row=False)

    book.resp.update_b2c_reference_status("B2C-1", enqueue_next=True)

    row = book.frappe.db.table(REF)["R1"]
    assert (row["b2c_disbursement_request"], row["payment_status"], row["reference_no"]) == (
        "B2C-1", "Paid", "SKT1AAAAAA"
    )
    assert _receipts(book) == ["SKT1AAAAAA"]
    request = book.frappe.db.table(REQ)["B2C-1"]
    assert (request["accounting_booked"], request["accounting_error"]) == (1, None)


def test_accounting_sweep_heals_and_books_a_row_without_its_link(book):
    _row(book, 1, "SKT1AAAAAA", link=None)
    book.frappe.utils.add_days = lambda date, days: "2026-09-18"

    book.post.retry_unbooked_payouts()

    assert book.frappe.db.table(REF)["R1"]["b2c_disbursement_request"] == "B2C-1"
    assert book.frappe.db.table(REQ)["B2C-1"]["accounting_booked"] == 1
    assert _receipts(book) == ["SKT1AAAAAA"]


def test_failed_result_restores_a_wiped_row_link(book):
    _row(book, 1, None, link=None, status="Failed", paid_row=False)

    book.resp.update_b2c_reference_status("B2C-1")

    row = book.frappe.db.table(REF)["R1"]
    assert (row["b2c_disbursement_request"], row["payment_status"]) == ("B2C-1", "Failed")


def test_cancelled_requests_late_result_does_not_take_the_row_over(book):
    # B2C-1 was cancelled and amended; the row now follows the amended request B2C-2
    _row(book, 1, None, link="B2C-2", status="Failed", paid_row=False, docstatus=2)

    book.resp.update_b2c_reference_status("B2C-1")

    assert book.frappe.db.table(REF)["R1"]["b2c_disbursement_request"] == "B2C-2"


def test_cancelled_requests_late_result_fills_an_empty_link_nobody_holds(book):
    # Cancel All took the request with it; its row's link was wiped earlier
    _row(book, 1, "SKT1AAAAAA", link=None, paid_row=False, docstatus=2)

    book.resp.update_b2c_reference_status("B2C-1")

    assert book.frappe.db.table(REF)["R1"]["b2c_disbursement_request"] == "B2C-1"


def test_cancelled_requests_late_result_leaves_a_row_a_submitted_request_names(book):
    # B2C-1 was cancelled and amended to B2C-2, which names the same row and is still out
    _row(book, 1, None, link=None, status="Failed", paid_row=False, docstatus=2)
    book.frappe.db.table(REQ)["B2C-2"] = dict(book.frappe.db.table(REQ)["B2C-1"], name="B2C-2", docstatus=1,
                                              status="Initiated")

    book.resp.update_b2c_reference_status("B2C-1")

    assert book.frappe.db.table(REF)["R1"]["b2c_disbursement_request"] is None


def test_payment_entry_carrying_a_siblings_receipt_is_not_this_payouts(book):
    # one claim paid to two numbers, 3,000 each; two live entries tagged D-1 carry R1's
    # receipt (a duplicate), R2 has none of its own
    for i in (1, 2):
        _row(book, i, f"SKT{i}AAAAAA", doctype="Expense Claim", name="HR-EXP-1", amount=3000)
    for _ in range(2):
        book.add_entry(PE, PER, {"docstatus": 1, "reference_no": "SKT1AAAAAA"},
                       [{"reference_doctype": "Expense Claim", "reference_name": "HR-EXP-1",
                         "allocated_amount": 3000, "b2c_payment_disbursement": "D-1"}])

    assert book.post.booked_docstatus("D-1", book.frappe.get_doc(REF, "R2")) is None
    assert book.post.handle_successful_payment("D-1", "R2") is True

    assert _receipts(book) == ["SKT1AAAAAA", "SKT1AAAAAA", "SKT2AAAAAA"]  # R2 has its own entry
    assert book.frappe.db.table(REQ)["B2C-2"]["accounting_booked"] == 1


def test_payment_entry_carrying_a_receipt_of_another_amount_is_not_a_spare(book):
    _row(book, 1, "SKT1AAAAAA", amount=5000)
    _row(book, 2, "SKT2AAAAAA", amount=3000)
    # an entry tagged D-1 for 3,000 that carries R1's receipt (edited by hand)
    book.add_entry(PE, PER, {"docstatus": 1, "reference_no": "SKT1AAAAAA"},
                   [{"reference_doctype": "Purchase Invoice", "reference_name": "PI-1",
                     "allocated_amount": 3000, "b2c_payment_disbursement": "D-1"}])

    assert book.post.booked_docstatus("D-1", book.frappe.get_doc(REF, "R2")) is None


def test_payment_entry_with_a_bank_reference_still_books_its_payout(book):
    # staff replaced the receipt with their own reference: still this flow's entry
    _row(book, 1, "SKT1AAAAAA", amount=3000)
    book.add_entry(PE, PER, {"docstatus": 1, "reference_no": "BANK-REF-7"},
                   [{"reference_doctype": "Purchase Invoice", "reference_name": "PI-1",
                     "allocated_amount": 3000, "b2c_payment_disbursement": "D-1"}])

    assert book.post.booked_docstatus("D-1", book.frappe.get_doc(REF, "R1")) == 1


def test_journal_entry_stamped_with_a_siblings_receipt_still_counts_once(book):
    # a legacy stamp script that matches by amount wrote R1's receipt on both entries
    for i in (1, 2):
        _row(book, i, f"SKT{i}AAAAAA", doctype="Salary Slip", name="SS-1", amount=200000, party="EMP-1")
    for _ in range(2):
        book.add_entry(JE, JEA, {"docstatus": 1, "cheque_no": "SKT1AAAAAA"},
                       [{"party": "EMP-1", "debit_in_account_currency": 200000, "b2c_payment_disbursement": "D-1"}])

    assert book.post.booked_docstatus("D-1", book.frappe.get_doc(REF, "R2")) == 1


def test_booked_docstatus_for_the_retry_guard_reads_everything_with_locks(book):
    # an entry staff submitted with their own reference: found through the spare-entry reads
    _row(book, 1, "SKT1AAAAAA", amount=3000)
    book.add_entry(PE, PER, {"docstatus": 1, "reference_no": "BANK-REF-7"},
                   [{"reference_doctype": "Purchase Invoice", "reference_name": "PI-1",
                     "allocated_amount": 3000, "b2c_payment_disbursement": "D-1"}])
    frappe = book.frappe
    frappe.db.db_type = "mariadb"
    queries, row_reads, entry_sql = [], [], frappe.db.sql

    def sql(query, values=None, **kw):
        queries.append(" ".join(query.split()))
        return entry_sql(query, values, **kw)

    def get_values(doctype, filters, fields, as_dict=False, for_update=False, **kw):
        row_reads.append((doctype, for_update))
        return frappe.db.get_all(doctype, filters=filters, fields=fields)

    frappe.db.sql, frappe.db.get_values = sql, get_values
    ref = frappe.get_doc(REF, "R1")

    assert book.post.booked_docstatus("D-1", ref, for_update=True) == 1
    assert len(queries) == 3 and all(q.endswith("FOR UPDATE") for q in queries)
    assert row_reads == [(REF, True), (REF, True)]

    queries.clear()
    row_reads.clear()
    assert book.post.booked_docstatus("D-1", ref) == 1  # booking itself: plain reads, as before
    assert len(queries) == 3 and not any("FOR UPDATE" in q for q in queries) and row_reads == []


# ---------------------------------------------------------------- C10 repair patch
def test_patch_relinks_rows_to_their_latest_submitted_request(frappe):
    frappe.db.table_exists = lambda doctype: True
    frappe.db.has_column = lambda doctype, column: True
    sent = []
    frappe.db.sql = lambda query, *a, **kw: sent.append(" ".join(query.split())) or [
        # as the query orders them: cancelled requests first, then submitted, each oldest first
        ("R1", "B2C-4"), ("R3", "B2C-5"), ("R3", "B2C-6"),
        ("R1", "B2C-1"), ("R2", "B2C-2"), ("R1", "B2C-3"),
    ]
    calls = []
    frappe.db.set_value = lambda *a, **kw: calls.append((a, kw))

    importlib.import_module(PATCH).execute()

    assert "req.docstatus IN (1, 2)" in sent[0] and "COALESCE(ref.b2c_disbursement_request, '') = ''" in sent[0]
    assert sent[0].endswith("ORDER BY req.docstatus DESC, req.creation, req.name")
    assert calls == [
        ((REF, "R1", "b2c_disbursement_request", "B2C-3"), {"update_modified": False}),  # latest submitted
        ((REF, "R3", "b2c_disbursement_request", "B2C-6"), {"update_modified": False}),  # only cancelled ones
        ((REF, "R2", "b2c_disbursement_request", "B2C-2"), {"update_modified": False}),
    ]


def test_patch_does_nothing_without_the_tables(frappe):
    frappe.db.table_exists = lambda doctype: False
    frappe.db.sql = lambda *a, **kw: pytest.fail("no query on a site without the tables")
    importlib.import_module(PATCH).execute()


# ---------------------------------------------------------------- C11 journal entry
@pytest.fixture
def journal(frappe):
    made = []

    class JournalEntry(fake_frappe._dict):
        def append(self, table, row):
            self.setdefault(table, []).append(row)

        def insert(self, ignore_permissions=False):
            made.append(("insert", dict(self)))

        def submit(self):
            # ERPNext JournalEntry.on_submit -> validate_cheque_info (v15 and v16)
            if self.voucher_type == "Bank Entry" and (not self.cheque_no or not self.cheque_date):
                raise fake_frappe.ValidationError("Reference No & Reference Date is required for Bank Entry")
            if self.cheque_date and not self.cheque_no:
                raise fake_frappe.ValidationError("Reference No is mandatory if you entered Reference Date")
            made.append(("submit", dict(self)))

    frappe.new_doc = lambda doctype: JournalEntry(doctype=doctype)
    frappe.get_doc = lambda doctype, name=None, **kw: fake_frappe._dict(name=name)  # the Salary Slip
    disbursement = fake_frappe._dict(name="D-1", company="Astral", paid_from="MPESA", paid_to="PAYABLE")

    def ref(**values):
        return fake_frappe._dict(
            {"reference_name": "SS-1", "allocated_amount": 45000, "party_type": "Employee", "party": "EMP-1",
             "payroll_entry": "HR-PRUN-1", "reference_no": "SKT1AAAAAA",
             "reference_date": datetime.datetime(2026, 10, 20, 9, 30), **values}
        )

    yield fake_frappe._dict(mod=importlib.import_module(JOURNAL), made=made, disbursement=disbursement, ref=ref)


def test_salary_bank_entry_carries_the_receipt_and_can_be_submitted(journal):
    journal.mod.create_journal_entry(journal.disbursement, journal.ref(reference_no=" SKT1AAAAAA "), submit=True)

    assert [step for step, _ in journal.made] == ["insert", "submit"]
    entry = journal.made[-1][1]
    assert (entry["voucher_type"], entry["cheque_no"], entry["cheque_date"]) == (
        "Bank Entry", "SKT1AAAAAA", datetime.date(2026, 10, 20)
    )


def test_salary_entry_reference_date_falls_back_to_the_posting_date(journal):
    journal.mod.create_journal_entry(journal.disbursement, journal.ref(reference_date=None))

    entry = journal.made[-1][1]
    assert (entry["cheque_no"], entry["cheque_date"]) == ("SKT1AAAAAA", entry["posting_date"])


def test_salary_entry_without_receipt_gets_no_reference_date(journal):
    # booking never gets here without a receipt; if it did, no date without a number
    journal.mod.create_journal_entry(journal.disbursement, journal.ref(reference_no=None))

    entry = journal.made[-1][1]
    assert not entry.get("cheque_no") and not entry.get("cheque_date")


# ---------------------------------------------------------------- C13 retry guard
class ProgrammingError(Exception):
    """MariaDB 1146: the table does not exist."""


@pytest.fixture
def owed(frappe, monkeypatch):
    sends, queries = [], []
    b2c_api = types.ModuleType("mpsa_suite.mpsa_suite.api.b2c")
    b2c_api.make_b2c_payment_request = sends.append
    query_builder = types.ModuleType("frappe.query_builder")
    query_builder.DocType = lambda name: None
    payment = types.ModuleType("mpsa_suite.accounting.payment_entry_service")
    payment.create_payment_entry_for_doc = lambda *a, **kw: None
    with _stub_modules(**{b2c_api.__name__: b2c_api, "frappe.query_builder": query_builder,
                          payment.__name__: payment}):
        frappe.db.db_type = "mariadb"
        frappe.utils.now = lambda: "2026-10-18 12:00:00"
        frappe.utils.add_days = lambda date, days: "2026-09-18"
        frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
        frappe.logger = lambda *a, **kw: logging.getLogger("fake_frappe")
        # paid: what the read through the rows returns; by_request: the read from the requests
        env = fake_frappe._dict(frappe=frappe, sends=sends, queries=queries, paid=[], by_request=[],
                                erpnext_tables=True)
        frappe.get_installed_apps = lambda: ["frappe", "erpnext", "mpsa_suite"]

        def sql(query, values=None, as_dict=False, **kw):
            q = " ".join(query.split())
            queries.append(q)
            if "`tabPayment Entry" in q and not env.erpnext_tables:
                raise ProgrammingError("(1146, \"Table '_x.tabPayment Entry Reference' doesn't exist\")")
            assert q.startswith("SELECT req.name AS request, req.amount, req.transaction_id"), q
            if "FROM `tabB2C Payment Disbursement Reference` ref JOIN" in q:
                rows = env.paid
            elif "FROM `tabB2C Disbursement Request` req LEFT JOIN" in q:
                rows = env.by_request
            else:
                raise AssertionError(q)
            return [fake_frappe._dict({"listed": 0, **r}) for r in rows]

        frappe.db.sql = sql
        mod = importlib.import_module(REQ_MODULE)
        post = importlib.import_module(POST)
        common = importlib.import_module("mpsa_suite.money_in.common")
        disb = importlib.import_module(DISB_MODULE)
        monkeypatch.setattr(common, "named_lock", lambda key, timeout=10: contextlib.nullcontext())
        monkeypatch.setattr(disb, "open_payouts", lambda doctype, name, **kw: [])
        owed_mod = importlib.import_module("mpsa_suite.money_out.owed")
        monkeypatch.setattr(owed_mod, "b2b_open", lambda doctype, name, **kw: [])

        class Request(mod.B2CDisbursementRequest):
            doctype = REQ

            def __init__(self, name):
                self.name = name
                self.flags = fake_frappe._dict()
                self.reload()

            def reload(self):
                self.__dict__.update(frappe.db.table(REQ)[self.name])

            def db_set(self, values, *a, **kw):
                self.__dict__.update(values)
                frappe.db.table(REQ)[self.name].update(values)

        def failed(doctype="ToDo", reference="TD-1"):
            frappe.db.table(REQ)["B2C-1"] = {
                "name": "B2C-1", "docstatus": 1, "status": "Failed", "amount": 50000,
                "reference_doctype": doctype, "reference_name": reference,
                "b2c_payment_disbursement": "D-1", "originator_conversation_id": "OCID-OLD",
            }
            return Request("B2C-1")

        env.update(mod=mod, post=post, failed=failed)
        yield env


def _without_erpnext(env):
    env.frappe.get_installed_apps = lambda: ["frappe", "mpsa_suite"]
    env.erpnext_tables = False


def test_retry_on_a_site_without_erpnext_queries_no_payment_entry_table(owed):
    _without_erpnext(owed)
    request = owed.failed()

    request.retry_failed_payment()

    assert owed.sends == ["B2C-1"]
    assert owed.queries and not any("Payment Entry" in q for q in owed.queries)


def test_retry_on_a_site_without_erpnext_still_refused_by_an_old_paid_payout(owed):
    # nothing can book a payout there, so another disbursement's Paid payout always counts
    _without_erpnext(owed)
    owed.paid = [{"request": "B2C-9", "sent_from": "D-2", "parent": "D-2", "amount": 50000,
                  "transaction_id": "SKT9"}]
    request = owed.failed()

    with pytest.raises(fake_frappe.ValidationError, match="awaiting accounting: D-2"):
        request.retry_failed_payment()
    assert owed.sends == []


def test_paid_payout_counts_unless_its_entry_is_still_live_and_submitted(owed, monkeypatch):
    # marked booked is not trusted: each payout's entry is checked as the booking sweep does
    owed.paid = [
        {"request": "B2C-2", "sent_from": "D-2", "parent": "D-2", "amount": 1, "transaction_id": "SKTBOOKED"},
        {"request": "B2C-3", "sent_from": "D-3", "parent": "D-3", "amount": 2, "transaction_id": "SKTCANCEL"},
        {"request": "B2C-4", "sent_from": "D-4", "parent": "D-4", "amount": 3, "transaction_id": "SKTDRAFT"},
    ]
    status = {"SKTBOOKED": 1, "SKTCANCEL": None, "SKTDRAFT": 0}
    checked = []

    def booked_docstatus(disbursement, row, for_update=False):
        checked.append((disbursement, row.reference_doctype, row.reference_name, row.allocated_amount, for_update))
        return status[row.reference_no]

    monkeypatch.setattr(owed.post, "booked_docstatus", booked_docstatus)

    rows = owed.mod.paid_without_entry("Purchase Invoice", "PI-1")

    assert [(r.request, r.parent, r.allocated_amount) for r in rows] == [("B2C-3", "D-3", 2), ("B2C-4", "D-4", 3)]
    # as the sweep books it: the disbursement that sent it, this document, the amount paid; locking reads
    assert checked[1] == ("D-3", "Purchase Invoice", "PI-1", 2, True)
    through_rows, from_requests = owed.queries
    # every Paid payout is read (not only old unbooked ones); open_payouts' own listing is marked
    assert "AND COALESCE(req.accounting_booked, 0) = 0 AND req.creation < " not in through_rows
    assert "(d.name IS NOT NULL AND (d.docstatus = 1 OR req.docstatus = 1)" in through_rows
    assert "AND (ref.name IS NULL OR COALESCE(ref.b2c_disbursement_request, '') != req.name)" in from_requests
    # locking reads: a Cancel All or a Payment Entry cancel committed after the snapshot is seen
    assert through_rows.endswith("FOR UPDATE") and from_requests.endswith("FOR UPDATE")
    assert not [q for q in owed.queries if "tabPayment Entry" in q]  # the entry is booked_docstatus' to find


def _not_booked(owed, monkeypatch):
    monkeypatch.setattr(owed.post, "booked_docstatus", lambda disbursement, row, for_update=False: None)


def test_payout_linked_from_several_rows_counts_once(owed, monkeypatch):
    # D-0 was cancelled with its requests (Cancel All) and amended to D-1, whose copy of the
    # paid row keeps the link to B2C-7
    _not_booked(owed, monkeypatch)
    owed.paid = [
        {"request": "B2C-7", "sent_from": "D-0", "parent": "D-0", "amount": 50000, "allocated_amount": 50000},
        {"request": "B2C-7", "sent_from": "D-0", "parent": "D-1", "amount": 50000, "allocated_amount": 50000},
    ]

    rows = owed.mod.paid_without_entry("Purchase Invoice", "PI-1")

    assert [(r.request, r.parent, r.parents, r.allocated_amount) for r in rows] == [
        ("B2C-7", "D-0", ["D-0", "D-1"], 50000)
    ]


def test_payout_open_payouts_lists_through_any_row_is_left_to_it(owed, monkeypatch):
    _not_booked(owed, monkeypatch)
    owed.paid = [
        {"request": "B2C-7", "sent_from": "D-0", "parent": "D-0", "amount": 50000, "listed": 0},
        {"request": "B2C-7", "sent_from": "D-0", "parent": "D-1", "amount": 50000, "listed": 1},
    ]
    owed.by_request = [{"request": "B2C-8", "sent_from": "D-2", "parent": "D-2", "amount": 100, "listed": 1}]

    assert owed.mod.paid_without_entry("Purchase Invoice", "PI-1") == []


def test_payout_whose_row_lost_its_link_is_found_from_its_request(owed, monkeypatch):
    # the row of D-2 (cancelled with it) no longer links B2C-9; the request still names the invoice
    _not_booked(owed, monkeypatch)
    owed.by_request = [{"request": "B2C-9", "sent_from": "D-2", "parent": "D-2", "amount": 50000,
                        "allocated_amount": None, "transaction_id": "SKT9"}]

    rows = owed.mod.paid_without_entry("Purchase Invoice", "PI-1")

    assert [(r.request, r.parent, r.allocated_amount, r.reference_no) for r in rows] == [
        ("B2C-9", "D-2", 50000, "SKT9")
    ]


def test_retry_counts_a_paid_payout_its_amended_row_links_as_its_own(owed, monkeypatch):
    # PI-1 owes 100,000 again after Cancel All on D-0 (which cancelled B2C-7's entry too); the
    # amend D-1 kept B2C-7's paid row (not re-sent) and re-sent the other half, which failed
    _not_booked(owed, monkeypatch)
    monkeypatch.setattr(importlib.import_module(DISB_MODULE), "current_outstanding",
                        lambda doctype, name, for_update=False: 100000)
    owed.paid = [
        {"request": "B2C-7", "sent_from": "D-0", "parent": "D-0", "amount": 50000},
        {"request": "B2C-7", "sent_from": "D-0", "parent": "D-1", "amount": 50000},
    ]
    request = owed.failed(doctype="Purchase Invoice", reference="PI-1")

    request.retry_failed_payment()

    assert owed.sends == ["B2C-1"]


def test_retry_refused_when_the_amended_rows_paid_payout_leaves_nothing_owed(owed, monkeypatch):
    _not_booked(owed, monkeypatch)
    monkeypatch.setattr(importlib.import_module(DISB_MODULE), "current_outstanding",
                        lambda doctype, name, for_update=False: 50000)
    owed.paid = [
        {"request": "B2C-7", "sent_from": "D-0", "parent": "D-0", "amount": 50000},
        {"request": "B2C-7", "sent_from": "D-0", "parent": "D-1", "amount": 50000},
    ]
    request = owed.failed(doctype="Purchase Invoice", reference="PI-1")

    with pytest.raises(fake_frappe.ValidationError, match="50000.0 is still being paid"):
        request.retry_failed_payment()
    assert owed.sends == []


def test_retry_refused_by_another_disbursements_payout_found_from_its_request(owed, monkeypatch):
    _not_booked(owed, monkeypatch)
    owed.by_request = [{"request": "B2C-9", "sent_from": "D-2", "parent": "D-2", "amount": 50000}]
    request = owed.failed(doctype="Purchase Invoice", reference="PI-1")

    with pytest.raises(fake_frappe.ValidationError, match="awaiting accounting: D-2$"):
        request.retry_failed_payment()
    assert owed.sends == []
