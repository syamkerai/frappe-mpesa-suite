"""B2C payout requests: booking, retry and Result follow-up, on an in-memory frappe.

Covers: every Paid payout row is booked by its own M-Pesa receipt, so a second row for the
same document gets its own entry, an entry made before the suite counts, and a payout with
no receipt is not booked; an entry staff submitted without the receipt (another Reference No,
another voucher type) still books its payout once, and split rows each keep their own entry
and amount (C11); a retry is refused once the document was paid another way or another
disbursement is paying it (also one paid long ago that nothing booked), while this
disbursement's other rows only take their share, and it reads the request's status and what
the document owes as committed, not from a stale snapshot (C13); the reference-status job
after a Daraja error or a Result is queued after the commit, so it sees Failed (C28).
"""

import contextlib
import copy
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
POST = "mpsa_suite.services.b2c_postprocess_service"
REQ_MODULE = "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request"
DISB_MODULE = "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement"
CONNECTOR = "mpsa_suite.connectors.mpesa.b2c_connector"


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


# ---------------------------------------------------------------- C11: booking key
def _entry_sql(frappe):
    """Answers the 'is this payout booked' queries from the in-memory entry tables.

    Evaluates the query's own ``alias.column = %s`` / ``IN %s`` conditions (case-insensitive,
    as MariaDB compares) and returns the columns it selects, so the old and new keys are both
    judged by what they actually ask.
    """

    def same(a, b):
        return str(a).upper() == str(b).upper() if isinstance(a, str) and isinstance(b, str) else a == b

    def sql(query, values=None, **kw):
        q = " ".join(query.split())
        frappe.queries.append(q)
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
            if not head or not all(
                same(source[a].get(col), v) if op == "=" else any(same(source[a].get(col), x) for x in v)
                for (a, col, op), v in zip(params, values)
            ):
                continue
            if f"{p}.docstatus < 2" in where and head["docstatus"] >= 2:
                continue
            found.append(source)
        if columns == [f"MAX({p}.docstatus)"]:
            return [(max((f[p]["docstatus"] for f in found), default=None),)]
        rows = [tuple(f[a].get(col) for a, col in (column.split(".") for column in columns)) for f in found]
        return list(dict.fromkeys(rows))  # DISTINCT

    return sql


@pytest.fixture
def book():
    frappe, saved = fake_frappe.install()
    seq = iter(range(1, 100))

    def add_entry(parent, child, head, rows):
        name = f"{parent[:2].upper()}-{next(seq)}"
        frappe.db.table(parent)[name] = dict(head, name=name)
        for i, row in enumerate(rows, 1):
            frappe.db.table(child)[f"{name}-{i}"] = dict(row, name=f"{name}-{i}", parent=name)
        return name

    def create_payment_entry_for_doc(disb, ref, submit=False):
        # what accounting.payment_entry_service writes
        add_entry(
            PE,
            PER,
            {"docstatus": 1 if submit else 0, "payment_type": "Pay", "reference_no": ref.reference_no},
            [{"reference_doctype": ref.reference_doctype, "reference_name": ref.reference_name,
              "allocated_amount": ref.allocated_amount, "b2c_payment_disbursement": disb.name}],
        )

    def create_journal_entry(disb, ref, submit=False):
        # accounting.journal_service: no Cheque No today (stamp_cheque False); True is the
        # receipt (the change it still needs), a string what a legacy stamp script chose
        stamp = frappe.stamp_cheque
        cheque = ref.reference_no if stamp is True else (stamp or None)
        if submit and not cheque:
            raise fake_frappe.ValidationError("Reference No & Reference Date is required for Bank Entry")
        add_entry(
            JE,
            JEA,
            {"docstatus": 1 if submit else 0, "voucher_type": "Bank Entry", "cheque_no": cheque},
            [{"account": "MPESA", "credit_in_account_currency": ref.allocated_amount},
             {"account": "PAYABLE", "debit_in_account_currency": ref.allocated_amount, "party": ref.party,
              "b2c_payment_disbursement": disb.name}],
        )

    journal = types.ModuleType("mpsa_suite.accounting.journal_service")
    journal.create_journal_entry = create_journal_entry
    payment = types.ModuleType("mpsa_suite.accounting.payment_entry_service")
    payment.create_payment_entry_for_doc = create_payment_entry_for_doc
    try:
        with _stub_modules(**{journal.__name__: journal, payment.__name__: payment}):
            frappe.queries = []
            frappe.stamp_cheque = False  # as accounting.journal_service is today
            frappe.db.sql = _entry_sql(frappe)
            frappe.db.commit = lambda: None
            frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
            frappe.db.table("Mpesa Settings")["MS-1"] = {"name": "MS-1", "submit_b2c_accounting_entries": 1}
            frappe.db.table(PARENT)["D-1"] = {"name": "D-1"}
            yield fake_frappe._dict(frappe=frappe, mod=importlib.import_module(POST), add_entry=add_entry)
    finally:
        fake_frappe.uninstall(saved)


def _paid_row(env, i, receipt, doctype="Purchase Invoice", name="PI-1", amount=100, party="SUP-1"):
    env.frappe.db.table(REF)[f"R{i}"] = {
        "name": f"R{i}", "parent": "D-1", "parenttype": PARENT, "idx": i,
        "reference_doctype": doctype, "reference_name": name, "party_type": "Supplier", "party": party,
        "allocated_amount": amount, "b2c_disbursement_request": f"B2C-{i}",
        "payment_status": "Paid", "reference_no": receipt,
    }
    env.frappe.db.table(REQ)[f"B2C-{i}"] = {
        "name": f"B2C-{i}", "docstatus": 1, "status": "Paid", "mpesa_settings": "MS-1",
        "accounting_booked": 0, "accounting_error": None, "transaction_id": receipt,
    }


def _salary_row(env, i, receipt, amount):
    _paid_row(env, i, receipt, doctype="Salary Slip", name="SS-1", amount=amount, party="EMP-1")


def _drafts(env):
    env.frappe.db.table("Mpesa Settings")["MS-1"]["submit_b2c_accounting_entries"] = 0


def _receipts(env, doctype=PE):
    field = "reference_no" if doctype == PE else "cheque_no"
    return sorted(e[field] or "" for e in env.frappe.db.table(doctype).values())


def _journal_amounts(env):
    return sorted(r["debit_in_account_currency"] for r in env.frappe.db.table(JEA).values()
                  if r.get("debit_in_account_currency"))


def _booked(env, *requests):
    return [env.frappe.db.table(REQ)[r]["accounting_booked"] for r in requests]


def _staff_submit(env, doctype, field, value):
    """Staff submit every draft, typing ``value`` as its Reference No (None: changed the voucher type)."""
    for entry in env.frappe.db.table(doctype).values():
        if entry["docstatus"] == 0:
            entry.update({"docstatus": 1, field: value})


def test_second_row_for_same_document_gets_its_own_entry(book):
    # PI-1 over the B2C limit, split across two rows of one disbursement
    _paid_row(book, 1, "SKT1AAAAAA", amount=250000)
    _paid_row(book, 2, "SKT2BBBBBB", amount=150000)

    assert book.mod.handle_successful_payment("D-1", "R1") is True
    assert book.mod.handle_successful_payment("D-1", "R2") is True

    assert _receipts(book) == ["SKT1AAAAAA", "SKT2BBBBBB"]
    assert _booked(book, "B2C-1", "B2C-2") == [1, 1]


def test_rows_of_the_same_amount_for_one_claim_each_get_their_own_entry(book):
    # one Expense Claim paid to many numbers, the same amount each
    for i in (1, 2, 3):
        _paid_row(book, i, f"SKT{i}AAAAAA", doctype="Expense Claim", name="HR-EXP-1", amount=3000)
    for _ in range(2):
        assert [book.mod.handle_successful_payment("D-1", f"R{i}") for i in (1, 2, 3)] == [True] * 3
    assert _receipts(book) == ["SKT1AAAAAA", "SKT2AAAAAA", "SKT3AAAAAA"]


def test_resent_result_books_nothing_more(book):
    _paid_row(book, 1, "SKT1AAAAAA")
    _paid_row(book, 2, "SKT2BBBBBB")
    for _ in range(2):
        book.mod.handle_successful_payment("D-1", "R1")
        book.mod.handle_successful_payment("D-1", "R2")
    assert _receipts(book) == ["SKT1AAAAAA", "SKT2BBBBBB"]


def test_entry_made_before_the_suite_for_the_receipt_counts(book):
    # booked by the old backstop / by hand: same receipt and document, no disbursement link
    book.add_entry(PE, PER, {"docstatus": 1, "reference_no": "SKT1AAAAAA"},
                   [{"reference_doctype": "Purchase Invoice", "reference_name": "PI-1", "allocated_amount": 100}])
    _paid_row(book, 1, "SKT1AAAAAA")

    assert book.mod.handle_successful_payment("D-1", "R1") is True
    assert len(book.frappe.db.table(PE)) == 1
    assert _booked(book, "B2C-1") == [1]


def test_entry_for_the_receipt_against_another_document_is_left_to_staff(book):
    book.add_entry(PE, PER, {"docstatus": 1, "reference_no": "SKT1AAAAAA"},
                   [{"reference_doctype": "Purchase Invoice", "reference_name": "PI-9", "allocated_amount": 100}])
    _paid_row(book, 1, "SKT1AAAAAA")

    assert book.mod.handle_successful_payment("D-1", "R1") is False

    # Payment Entry refuses a second entry with the same receipt: none is attempted. The
    # double-payout guard still counts the payout (paid_without_entry re-checks the entry).
    booked_for = [r["reference_name"] for r in book.frappe.db.table(PER).values()]
    assert booked_for == ["PI-9"]
    request = book.frappe.db.table(REQ)["B2C-1"]
    assert request["accounting_booked"] == 0  # the sweep looks again, quietly, each pass
    assert "already on Payment Entry" in request["accounting_error"]
    assert "it is on Purchase Invoice PI-9" in request["accounting_error"]
    assert "Purchase Invoice PI-1 stay blocked" in request["accounting_error"]


def test_unallocated_entry_for_the_receipt_books_nothing_more(book):
    book.add_entry(PE, PER, {"docstatus": 1, "reference_no": "SKT1AAAAAA"}, [])  # an advance, not allocated
    _paid_row(book, 1, "SKT1AAAAAA")

    assert book.mod.handle_successful_payment("D-1", "R1") is False
    assert book.mod.handle_successful_payment("D-1", "R1") is False  # the next pass: still nothing
    assert len(book.frappe.db.table(PE)) == 1
    request = book.frappe.db.table(REQ)["B2C-1"]
    assert request["accounting_booked"] == 0
    assert "not allocated: allocate it to Purchase Invoice PI-1" in request["accounting_error"]


def test_a_claims_payout_it_could_take_none_of_is_booked_unallocated_and_said_so(book, monkeypatch):
    # final review: payment_entry_service books a claim's payout the claim can no longer take as an unallocated
    # Payment Entry to the employee (and alerts): the first pass already says where it is, not "a draft"
    def unallocated(disb, ref, submit=False):
        book.add_entry(PE, PER, {"docstatus": 1, "payment_type": "Pay", "reference_no": ref.reference_no}, [])

    monkeypatch.setattr(book.mod, "create_payment_entry_for_doc", unallocated)
    _paid_row(book, 1, "SKT1AAAAAA", doctype="Expense Claim", name="HR-EXP-1", amount=3000, party="EMP-1")
    assert book.mod.handle_successful_payment("D-1", "R1") is False
    assert book.mod.handle_successful_payment("D-1", "R1") is False  # the sweep: nothing booked again
    assert len(book.frappe.db.table(PE)) == 1
    request = book.frappe.db.table(REQ)["B2C-1"]
    assert request["accounting_booked"] == 0  # the double-payout guards still count it
    assert "not allocated: allocate it to Expense Claim HR-EXP-1" in request["accounting_error"]


def test_journal_entry_carrying_the_receipt_books_nothing_more(book):
    # Bank Reconciliation's Create Voucher copies the receipt into a Journal Entry's Cheque No
    book.add_entry(JE, JEA, {"docstatus": 1, "cheque_no": "SKT1AAAAAA"}, [{"reference_type": None, "reference_name": None}])
    _paid_row(book, 1, "SKT1AAAAAA")

    assert book.mod.handle_successful_payment("D-1", "R1") is False
    assert book.frappe.db.table(PE) == {}
    assert "already on Journal Entry" in book.frappe.db.table(REQ)["B2C-1"]["accounting_error"]


def test_payout_is_booked_once_the_stray_entry_no_longer_carries_its_receipt(book):
    stray = book.add_entry(PE, PER, {"docstatus": 1, "reference_no": "SKT1AAAAAA"},
                           [{"reference_doctype": "Purchase Invoice", "reference_name": "PI-9", "allocated_amount": 100}])
    _paid_row(book, 1, "SKT1AAAAAA")
    assert book.mod.handle_successful_payment("D-1", "R1") is False

    book.frappe.db.table(PE)[stray]["docstatus"] = 2  # it was another payout's: staff cancel it

    assert book.mod.handle_successful_payment("D-1", "R1") is True
    assert book.frappe.db.table(REQ)["B2C-1"]["accounting_booked"] == 1


def test_draft_entry_for_the_receipt_elsewhere_keeps_the_payout_unbooked(book):
    book.add_entry(PE, PER, {"docstatus": 0, "reference_no": "SKT1AAAAAA"},
                   [{"reference_doctype": "Purchase Invoice", "reference_name": "PI-9", "allocated_amount": 100}])
    _paid_row(book, 1, "SKT1AAAAAA")

    assert book.mod.handle_successful_payment("D-1", "R1") is False
    request = book.frappe.db.table(REQ)["B2C-1"]
    assert request["accounting_booked"] == 0
    assert "a draft: submit it" in request["accounting_error"]


def test_row_without_its_receipt_takes_the_paid_requests(book):
    _paid_row(book, 1, None)
    book.frappe.db.table(REQ)["B2C-1"].update(status="Paid", transaction_id="SKT1AAAAAA")

    assert book.mod.handle_successful_payment("D-1", "R1") is True
    assert book.frappe.db.table(REF)["R1"]["reference_no"] == "SKT1AAAAAA"  # healed first
    assert _receipts(book) == ["SKT1AAAAAA"]  # the entry carries it


def test_cancelled_entry_does_not_count(book):
    book.add_entry(PE, PER, {"docstatus": 2, "reference_no": "SKT1AAAAAA"},
                   [{"reference_doctype": "Purchase Invoice", "reference_name": "PI-1", "allocated_amount": 100,
                     "b2c_payment_disbursement": "D-1"}])
    _paid_row(book, 1, "SKT1AAAAAA")
    assert book.mod.handle_successful_payment("D-1", "R1") is True
    assert len(book.frappe.db.table(PE)) == 2


def test_paid_payout_without_receipt_is_not_booked(book):
    _paid_row(book, 2, "SKT2BBBBBB")
    book.mod.handle_successful_payment("D-1", "R2")  # the other row for PI-1 is booked
    _paid_row(book, 1, None)

    assert book.mod.handle_successful_payment("D-1", "R1") is False

    assert _receipts(book) == ["SKT2BBBBBB"]
    request = book.frappe.db.table(REQ)["B2C-1"]
    assert request["accounting_booked"] == 0  # keeps blocking a new payout for PI-1
    assert "no M-Pesa receipt" in request["accounting_error"]
    assert book.mod.booked_docstatus("D-1", book.frappe.get_doc(REF, "R1")) is None


def test_draft_entry_is_not_booked(book):
    _drafts(book)
    _paid_row(book, 1, "SKT1AAAAAA")
    _paid_row(book, 2, "SKT2BBBBBB")
    assert book.mod.handle_successful_payment("D-1", "R1") is False
    assert book.mod.handle_successful_payment("D-1", "R2") is False
    assert book.mod.handle_successful_payment("D-1", "R1") is False  # the sweep again
    assert _receipts(book) == ["SKT1AAAAAA", "SKT2BBBBBB"]
    assert book.frappe.db.table(REQ)["B2C-1"]["accounting_error"] == "Accounting entry is a draft; submit it."


def test_payment_entry_submitted_with_another_reference_no_is_booked_once(book):
    # staff replaced the receipt with their bank reference before submitting the draft
    _drafts(book)
    _paid_row(book, 1, "SKT1AAAAAA")
    assert book.mod.handle_successful_payment("D-1", "R1") is False
    _staff_submit(book, PE, "reference_no", "BANK-REF-7")

    assert book.mod.handle_successful_payment("D-1", "R1") is True  # the sweep
    assert _receipts(book) == ["BANK-REF-7"]
    assert _booked(book, "B2C-1") == [1]


@pytest.mark.parametrize("typed", ["OCT-PAYROLL", None], ids=["other-reference", "journal-entry-type"])
def test_single_salary_row_draft_submitted_by_staff_is_booked_once(book, typed):
    # journal_service makes the draft without a Cheque No; staff submit it with their own
    # Reference No, or change it to a Journal Entry so none is needed
    _drafts(book)
    _salary_row(book, 1, "SKT1AAAAAA", 45000)
    assert book.mod.handle_successful_payment("D-1", "R1") is False
    _staff_submit(book, JE, "cheque_no", typed)

    for _ in range(2):  # the sweep, twice
        assert book.mod.handle_successful_payment("D-1", "R1") is True
    assert len(book.frappe.db.table(JE)) == 1
    assert _booked(book, "B2C-1") == [1]


def test_salary_draft_without_cheque_no_is_not_booked_again(book):
    _drafts(book)
    _salary_row(book, 1, "SKT1AAAAAA", 45000)

    for _ in range(3):  # the result, then the sweep
        assert book.mod.handle_successful_payment("D-1", "R1") is False

    assert len(book.frappe.db.table(JE)) == 1
    assert _booked(book, "B2C-1") == [0]


def test_split_salary_slip_rows_each_get_a_journal_entry(book):
    # net pay over the B2C limit: two rows, one employee; journal_service sets the receipt
    book.frappe.stamp_cheque = True
    _salary_row(book, 1, "SKT1AAAAAA", 250000)
    _salary_row(book, 2, "SKT2BBBBBB", 50000)

    assert book.mod.handle_successful_payment("D-1", "R1") is True
    assert book.mod.handle_successful_payment("D-1", "R2") is True
    book.mod.handle_successful_payment("D-1", "R2")  # re-sent

    assert _receipts(book, JE) == ["SKT1AAAAAA", "SKT2BBBBBB"]


def test_split_salary_rows_without_cheque_no_each_get_their_own_draft(book):
    _drafts(book)
    _salary_row(book, 1, "SKT1AAAAAA", 250000)
    _salary_row(book, 2, "SKT2BBBBBB", 50000)
    for _ in range(2):
        assert book.mod.handle_successful_payment("D-1", "R1") is False
        assert book.mod.handle_successful_payment("D-1", "R2") is False
    assert _journal_amounts(book) == [50000, 250000]

    _staff_submit(book, JE, "cheque_no", "OCT-PAYROLL")
    assert [book.mod.handle_successful_payment("D-1", r) for r in ("R1", "R2")] == [True, True]
    assert _journal_amounts(book) == [50000, 250000]


def test_one_split_row_retried_before_the_other_is_booked_keeps_each_amount(book):
    # R2 is Paid but its booking failed; the sweep tries R1 again first
    _drafts(book)
    _salary_row(book, 1, "SKT1AAAAAA", 250000)
    _salary_row(book, 2, "SKT2BBBBBB", 50000)
    book.mod.handle_successful_payment("D-1", "R1")
    book.mod.handle_successful_payment("D-1", "R1")
    assert _journal_amounts(book) == [250000]  # R1's draft is R1's; R2 still needs its own

    book.mod.handle_successful_payment("D-1", "R2")
    assert _journal_amounts(book) == [50000, 250000]


def test_equal_split_rows_of_one_payout_still_waiting_share_no_entry(book):
    _drafts(book)
    _salary_row(book, 1, "SKT1AAAAAA", 200000)
    _salary_row(book, 2, "SKT2BBBBBB", 200000)
    book.mod.handle_successful_payment("D-1", "R1")
    # one draft for two payouts of the same amount: neither is booked by it alone
    for row in ("R1", "R2"):
        assert book.mod.booked_docstatus("D-1", book.frappe.get_doc(REF, row)) is None
    book.mod.handle_successful_payment("D-1", "R2")
    book.mod.handle_successful_payment("D-1", "R1")
    assert _journal_amounts(book) == [200000, 200000]


def test_equal_split_rows_stamped_with_one_receipt_book_no_more(book):
    # a legacy stamp script that matches by amount picks the first row's receipt for both
    book.frappe.stamp_cheque = "SKT1AAAAAA"
    _salary_row(book, 1, "SKT1AAAAAA", 200000)
    _salary_row(book, 2, "SKT2BBBBBB", 200000)

    assert book.mod.handle_successful_payment("D-1", "R1") is True
    for _ in range(3):  # the result, then the sweep
        assert book.mod.handle_successful_payment("D-1", "R2") is True
    assert _journal_amounts(book) == [200000, 200000]


def test_salary_journal_entry_with_receipt_for_another_employee_does_not_count(book):
    book.add_entry(JE, JEA, {"docstatus": 1, "cheque_no": "SKT1AAAAAA"}, [{"party": "EMP-9"}])
    _salary_row(book, 1, "SKT1AAAAAA", 45000)
    assert book.mod.booked_docstatus("D-1", book.frappe.get_doc(REF, "R1")) is None


def test_entry_of_another_amount_or_disbursement_is_not_this_payouts(book):
    book.add_entry(JE, JEA, {"docstatus": 1, "cheque_no": None},
                   [{"party": "EMP-1", "debit_in_account_currency": 44000, "b2c_payment_disbursement": "D-1"}])
    book.add_entry(JE, JEA, {"docstatus": 1, "cheque_no": None},
                   [{"party": "EMP-1", "debit_in_account_currency": 45000, "b2c_payment_disbursement": "D-0"}])
    _salary_row(book, 1, "SKT1AAAAAA", 45000)
    assert book.mod.booked_docstatus("D-1", book.frappe.get_doc(REF, "R1")) is None


# ---------------------------------------------------------------- C13: retry guard
def _salary_paid_sql(frappe):
    """salary_slip_paid's two reads of the Paid payouts: the plain one sees the snapshot."""

    def sql(query, values=None, **kw):
        q = " ".join(query.split())
        if q.startswith("SELECT name, amount FROM `tabB2C Disbursement Request`"):
            tables = frappe.snapshot if frappe.snapshot is not None else frappe.db.tables
        elif "JOIN `tabB2C Disbursement Request` req" in q and q.endswith("FOR UPDATE"):
            tables = frappe.db.tables
        else:
            raise AssertionError(f"unexpected SQL: {q}")
        return tuple(
            (r["name"], r["amount"]) for r in tables.get(REQ, {}).values()
            if r.get("docstatus") in (1, 2) and r.get("status") == "Paid"
            and r.get("reference_doctype") == "Salary Slip" and r.get("reference_name") == values[0]
        )

    return sql


@pytest.fixture
def retry(monkeypatch):
    frappe, saved = fake_frappe.install()
    sends = []
    b2c_api = types.ModuleType("mpsa_suite.mpsa_suite.api.b2c")
    b2c_api.make_b2c_payment_request = lambda name: sends.append(
        (name, dict(frappe.db.table(REQ)[name]), list(frappe.locks_held))
    )
    query_builder = types.ModuleType("frappe.query_builder")
    query_builder.DocType = lambda name: None
    try:
        with _stub_modules(**{b2c_api.__name__: b2c_api, "frappe.query_builder": query_builder}):
            frappe.db.db_type = "mariadb"
            frappe.utils.now = lambda: "2026-10-18 12:00:00"
            frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
            frappe.logger = lambda *a, **kw: logging.getLogger("fake_frappe")
            frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
            frappe.locks_held, frappe.row_locks_before, frappe.calls = [], [], []
            # this transaction's snapshot (None: none taken): plain reads see it, locking reads
            # see what is committed now
            frappe.snapshot = None
            plain_get_value = frappe.db.get_value

            def get_value(doctype, filters, fieldname="name", as_dict=False, for_update=False, **kw):
                if frappe.snapshot is None or for_update:
                    return plain_get_value(doctype, filters, fieldname, as_dict=as_dict, for_update=for_update, **kw)
                live, frappe.db.tables = frappe.db.tables, frappe.snapshot
                try:
                    return plain_get_value(doctype, filters, fieldname, as_dict=as_dict, **kw)
                finally:
                    frappe.db.tables = live

            frappe.db.get_value = get_value
            frappe.db.sql = _salary_paid_sql(frappe)
            mod = importlib.import_module(REQ_MODULE)
            common = importlib.import_module("mpsa_suite.money_in.common")
            disb = importlib.import_module(DISB_MODULE)

            @contextlib.contextmanager
            def named_lock(key, timeout=10, busy=None):
                if key not in frappe.locks_held:  # held until commit; re-entering is a no-op
                    frappe.locks_held.append(key)
                    frappe.row_locks_before.append(list(frappe.db.locks))
                    frappe.busy.append(busy)
                yield

            frappe.busy = []
            env = fake_frappe._dict(frappe=frappe, sends=sends, others=[], stale=[], b2b=[], advances=[], owed=50000)
            owed_mod = importlib.import_module("mpsa_suite.money_out.owed")

            def b2b_open(doctype, name, exclude=None, for_update=False):
                frappe.calls.append(("b2b_open", doctype, name, for_update, list(frappe.locks_held)))
                return [fake_frappe._dict(o) for o in env.b2b]

            def order_advances(pi, for_update=False, exclude_request=None):
                frappe.calls.append(("order_advances", pi, for_update, list(frappe.locks_held)))
                return [fake_frappe._dict(o) for o in env.advances]

            def open_payouts(doctype, name, exclude_parent=None, exclude_requests=None):
                frappe.calls.append(("open_payouts", doctype, name, exclude_parent, exclude_requests,
                                     list(frappe.locks_held)))
                return [fake_frappe._dict(o) for o in env.others]

            def current_outstanding(doctype, name, for_update=False):
                frappe.calls.append(("current_outstanding", doctype, name, for_update, list(frappe.locks_held)))
                return env.owed

            def paid_without_entry(doctype, name):
                frappe.calls.append(("paid_without_entry", doctype, name, list(frappe.locks_held)))
                return [fake_frappe._dict(o) for o in env.stale]

            real_current_outstanding = disb.current_outstanding
            monkeypatch.setattr(common, "named_lock", named_lock)
            monkeypatch.setattr(disb, "open_payouts", open_payouts)
            monkeypatch.setattr(disb, "current_outstanding", current_outstanding)
            monkeypatch.setattr(mod, "paid_without_entry", paid_without_entry)
            monkeypatch.setattr(owed_mod, "b2b_open", b2b_open)
            monkeypatch.setattr(owed_mod, "order_advances", order_advances)
            env.planned, env.bank = [], None

            def planned_lines(dt, dn, exclude_line=None, exclude_schedule=None, for_update=False):
                frappe.calls.append(("planned_lines", dt, dn, for_update, list(frappe.locks_held)))
                return [fake_frappe._dict(o) for o in env.planned]

            monkeypatch.setattr(owed_mod, "planned_lines", planned_lines)
            monkeypatch.setattr(owed_mod, "slip_bank_problem", lambda slip: env.bank)
            env.taken = []

            def employee_doc_taken_off(dt, dn, b2b=(), exclude_payees=()):
                frappe.calls.append(("employee_doc_taken_off", dt, dn, [o.parent for o in b2b], list(exclude_payees),
                                     list(frappe.locks_held)))
                return [fake_frappe._dict(o) for o in env.taken]

            monkeypatch.setattr(owed_mod, "employee_doc_taken_off", employee_doc_taken_off)
            env.use_real_outstanding = lambda: monkeypatch.setattr(
                disb, "current_outstanding", real_current_outstanding
            )
            env.real_current_outstanding = real_current_outstanding

            class Request(mod.B2CDisbursementRequest):
                doctype = REQ

                def __init__(self, name):
                    self.name = name
                    self.flags = fake_frappe._dict()
                    self.reload()

                def reload(self):
                    if self.flags.for_update:  # a locking read: committed now, and the row lock
                        frappe.db.locks.append((REQ, self.name))
                        tables = frappe.db.tables
                    else:
                        tables = frappe.snapshot if frappe.snapshot is not None else frappe.db.tables
                    self.__dict__.update(copy.deepcopy(tables[REQ][self.name]))

                def db_set(self, values, *a, **kw):
                    self.__dict__.update(values)
                    frappe.db.table(REQ)[self.name].update(values)

            def failed(name="B2C-1", amount=50000, doctype="Purchase Invoice", reference="PI-1", parent="D-1"):
                frappe.db.table(REQ)[name] = {
                    "name": name, "docstatus": 1, "status": "Failed", "amount": amount,
                    "reference_doctype": doctype, "reference_name": reference,
                    "b2c_payment_disbursement": parent, "originator_conversation_id": "OCID-OLD",
                    "result_code": "1", "result_desc": "The balance is insufficient",
                }
                return Request(name)

            def take_snapshot():
                frappe.snapshot = copy.deepcopy(frappe.db.tables)

            env.failed = failed
            env.take_snapshot = take_snapshot
            yield env
    finally:
        fake_frappe.uninstall(saved)


def _refused(env, request, message):
    with pytest.raises(fake_frappe.ValidationError, match=message):
        request.retry_failed_payment()
    row = env.frappe.db.table(REQ)[request.name]
    assert env.sends == []
    assert (row["status"], row["originator_conversation_id"], row["result_code"]) == ("Failed", "OCID-OLD", "1")


def test_retry_refused_once_the_document_was_paid_another_way(retry):
    request = retry.failed()
    retry.owed = 0.0  # paid by a second disbursement (now booked) or a bank Payment Entry
    _refused(retry, request, "PI-1 owes 0.0; retrying would pay 50000.0")


def test_retry_refused_when_the_document_now_owes_less(retry):
    request = retry.failed()
    retry.owed = 20000
    _refused(retry, request, "owes 20000.0")


def test_retry_refused_while_another_disbursement_pays_the_document(retry):
    request = retry.failed()
    retry.others = [{"parent": "D-2", "allocated_amount": 50000, "request_status": "Initiated"}]
    _refused(retry, request, "already has a payout in progress or awaiting accounting: D-2")


def test_retry_refused_while_another_disbursement_awaits_accounting(retry):
    request = retry.failed()
    retry.others = [{"parent": "D-2", "allocated_amount": 50000, "request_status": "Paid"}]
    _refused(retry, request, "D-2")


def test_retry_refused_while_a_b2b_request_pays_the_document(retry):
    # an Initiated M-Pesa B2B request for the same invoice (B2C's guard sees B2B, R3)
    request = retry.failed()
    retry.b2b = [{"parent": "MB2B-26-10-000001", "allocated_amount": 50000, "request_status": "Initiated"}]
    _refused(retry, request, "awaiting accounting: MB2B-26-10-000001")


def test_retry_refused_by_an_old_paid_payout_nothing_booked(retry):
    # D-2's payout is older than the accounting retry window, so open_payouts no longer lists
    # it, but no entry books it and the invoice still shows the full amount owed
    request = retry.failed()
    retry.stale = [{"parent": "D-2", "allocated_amount": 50000, "request_status": "Paid"}]
    _refused(retry, request, "awaiting accounting: D-2")


def test_retry_counts_an_old_paid_row_of_this_disbursement_nothing_booked(retry):
    request = retry.failed(amount=150000)
    retry.stale = [{"parent": "D-1", "allocated_amount": 250000, "request_status": "Paid"}]
    retry.owed = 300000  # 400,000, less 100,000 paid by bank since
    _refused(retry, request, "250000.0 is still being paid")


def test_retry_sends_when_the_document_still_owes_it(retry):
    request = retry.failed()

    request.retry_failed_payment()

    assert len(retry.sends) == 1
    name, row, locks = retry.sends[0]
    assert (name, row["status"], row["result_code"]) == ("B2C-1", "Not Initiated", None)
    assert row["originator_conversation_id"] != "OCID-OLD"
    # same per-document lock and reads as the submit check, all under the lock
    key = "b2c-pay:Purchase Invoice:PI-1"
    assert locks == [key]
    assert retry.frappe.calls == [
        ("open_payouts", "Purchase Invoice", "PI-1", None, None, [key]),
        ("paid_without_entry", "Purchase Invoice", "PI-1", [key]),
        ("b2b_open", "Purchase Invoice", "PI-1", True, [key]),  # B2B counts too, under the same lock
        ("order_advances", "PI-1", True, [key]),  # and the invoice's order advances, before its row (R4)
        ("planned_lines", "Purchase Invoice", "PI-1", True, [key]),  # and lines a payment schedule plans (R3)
        ("current_outstanding", "Purchase Invoice", "PI-1", True, [key]),
    ]
    # the document lock before the request's row lock, in the order a disbursement submit takes them
    assert retry.frappe.row_locks_before == [[]]
    assert retry.frappe.db.locks == [(REQ, "B2C-1")]
    assert retry.frappe.busy == ["Another payment for Purchase Invoice PI-1 is being sent or booked. "
                                 "Try again in a minute."]


def test_retry_refused_by_an_advance_still_on_the_invoices_order(retry):
    # a B2B advance on PO-1; the invoice made from it (without Get Advances) still owes 50,000
    request = retry.failed()
    retry.advances = [{"kind": "advance", "name": "PO-1", "amount": 50000,
                       "label": "advance still on PO-1 50,000 (allocate it with Get Advances first)"}]
    _refused(retry, request, "PI-1 owes 50000.0, less advance still on PO-1 50,000 .*; retrying would pay 50000.0")


def test_retry_refused_once_the_order_has_invoices(retry):
    # the failed payout was an advance on PO-1; PI-1 was made from PO-1 since (B2B may have paid it)
    request = retry.failed(doctype="Purchase Order", reference="PO-1")
    retry.frappe.db.table("Purchase Order")["PO-1"] = {"name": "PO-1", "per_billed": 100}
    _refused(retry, request, "PO-1 already has invoices: pay the Purchase Invoice instead.")


def test_retry_of_another_document_does_not_ask_about_order_advances(retry, monkeypatch):
    import importlib

    # round 4: the claim, the approved phone and the payer are checked again (unit-tested in
    # test_employee_documents); here only that the retry asks
    documents = importlib.import_module("mpsa_suite.money_out.employee_documents")
    asked = []
    monkeypatch.setattr(documents, "check_b2c_retry", lambda doc: asked.append(doc.name))
    request = retry.failed(doctype="Expense Claim", reference="EC-1")
    request.retry_failed_payment()
    assert len(retry.sends) == 1 and asked == ["B2C-1"]
    assert not [c for c in retry.frappe.calls if c[0] == "order_advances"]
    # round 4: a B2B request may pay an Expense Claim too (money_out.employee_documents)
    assert [c[:3] for c in retry.frappe.calls if c[0] == "b2b_open"] == [("b2b_open", "Expense Claim", "EC-1")]


def test_a_retry_of_a_claim_is_refused_by_a_draft_entry_on_it_and_its_payees_share(retry, monkeypatch):
    # final review: the retry guard takes off what owed.left_to_pay also takes off (draft entries, a claim's
    # unpaid payee lines), as the send does; an invoice's retry asks nothing more
    import importlib

    documents = importlib.import_module("mpsa_suite.money_out.employee_documents")
    monkeypatch.setattr(documents, "check_b2c_retry", lambda doc: None)
    retry.owed = 5000
    retry.taken = [{"kind": "draft", "name": "ACC-PAY-1", "amount": 5000, "label": "draft Payment Entry ACC-PAY-1 5,000"}]
    request = retry.failed(amount=5000, doctype="Employee Advance", reference="EA-1")
    with pytest.raises(fake_frappe.ValidationError, match="EA-1 owes 5000.0, less draft Payment Entry ACC-PAY-1 5,000"):
        request.retry_failed_payment()
    assert retry.sends == []
    (asked,) = [c for c in retry.frappe.calls if c[0] == "employee_doc_taken_off"]
    assert asked[:5] == ("employee_doc_taken_off", "Employee Advance", "EA-1", [], [])
    assert "b2c-pay:Employee Advance:EA-1" in asked[5]  # under the document's lock
    retry.taken = []
    request.retry_failed_payment()
    assert len(retry.sends) == 1
    retry.frappe.calls.clear()
    retry.failed(name="B2C-PI", amount=1000).retry_failed_payment()  # an invoice
    assert not [c for c in retry.frappe.calls if c[0] == "employee_doc_taken_off"]


def test_retry_reads_the_status_a_concurrent_retry_committed(retry):
    # this request's form (or Retry Failed Transactions) read it while it was Failed; a second
    # retry has since reset it and committed midway through its send
    request = retry.failed()
    retry.take_snapshot()
    retry.frappe.db.table(REQ)["B2C-1"].update(status="Not Initiated", originator_conversation_id="OCID-NEW")

    with pytest.raises(fake_frappe.ValidationError, match=r"can be retried \(Not Initiated\)"):
        request.retry_failed_payment()

    assert retry.sends == [] and retry.frappe.calls == []
    assert retry.frappe.db.table(REQ)["B2C-1"]["originator_conversation_id"] == "OCID-NEW"


def test_retry_reads_what_the_invoice_owes_after_a_booking_committed_since(retry):
    # D-2 paid PI-1 and its Payment Entry was booked after this transaction's snapshot:
    # open_payouts (a locking read) no longer lists it, and the snapshot still shows it owed
    retry.use_real_outstanding()
    retry.frappe.db.table("Purchase Invoice")["PI-1"] = {"name": "PI-1", "docstatus": 1, "outstanding_amount": 50000}
    request = retry.failed()
    retry.take_snapshot()
    retry.frappe.db.table("Purchase Invoice")["PI-1"]["outstanding_amount"] = 0

    assert retry.real_current_outstanding("Purchase Invoice", "PI-1") == 50000  # the snapshot alone
    _refused(retry, request, "PI-1 owes 0.0")


def test_retry_counts_this_disbursements_other_rows_for_the_document(retry):
    # PI-1 owes 400,000, split over two rows of D-1; the other row is still in flight
    request = retry.failed(amount=150000)
    retry.others = [{"parent": "D-1", "allocated_amount": 250000, "request_status": "Initiated"}]
    retry.owed = 400000
    request.retry_failed_payment()
    assert len(retry.sends) == 1


def test_retry_refused_when_this_disbursements_other_rows_take_what_is_owed(retry):
    request = retry.failed(amount=150000)
    retry.others = [{"parent": "D-1", "allocated_amount": 250000, "request_status": "Paid"}]
    retry.owed = 300000  # paid since by bank for part of it
    _refused(retry, request, "250000.0 is still being paid")


def test_retry_salary_slip_does_not_count_a_paid_row_twice(retry):
    # a Salary Slip's owed already has its Paid payouts taken off
    request = retry.failed(amount=50000, doctype="Salary Slip", reference="SS-1")
    retry.others = [{"parent": "D-1", "allocated_amount": 250000, "request_status": "Paid"}]
    retry.owed = 50000
    request.retry_failed_payment()
    assert len(retry.sends) == 1
    assert "paid_without_entry" not in [call[0] for call in retry.frappe.calls]


def test_retry_salary_slip_counts_a_row_in_flight(retry):
    request = retry.failed(amount=50000, doctype="Salary Slip", reference="SS-1")
    retry.others = [{"parent": "D-1", "allocated_amount": 250000, "request_status": None}]
    retry.owed = 300000 - 1
    _refused(retry, request, "SS-1 owes")


def _slip(retry, net_pay):
    retry.frappe.db.table("Salary Slip")["SS-1"] = {
        "name": "SS-1", "docstatus": 1, "rounded_total": net_pay, "net_pay": net_pay,
        "employee": "EMP-1", "payroll_entry": None,
    }


def test_retry_salary_slip_paid_by_another_disbursement_since_the_snapshot(retry):
    # the real owed read: D-2's payout was Paid and booked after this transaction's snapshot,
    # so open_payouts drops it and only salary_slip_paid's locking read still sees it
    retry.use_real_outstanding()
    _slip(retry, 50000)
    retry.frappe.db.table(REQ)["B2C-9"] = {"name": "B2C-9", "docstatus": 1, "status": "Initiated", "amount": 50000,
                                           "reference_doctype": "Salary Slip", "reference_name": "SS-1"}
    request = retry.failed(doctype="Salary Slip", reference="SS-1")
    retry.take_snapshot()
    retry.frappe.db.table(REQ)["B2C-9"].update(status="Paid", accounting_booked=1)

    _refused(retry, request, "SS-1 owes 0.0")


def test_retry_salary_slip_shares_the_rest_with_a_paid_row_of_this_disbursement(retry):
    # net pay 300,000 split over D-1: B2C-2 (250,000) Paid, not yet booked; B2C-1 failed
    retry.use_real_outstanding()
    _slip(retry, 300000)
    retry.frappe.db.table(REQ)["B2C-2"] = {"name": "B2C-2", "docstatus": 1, "status": "Paid", "amount": 250000,
                                           "reference_doctype": "Salary Slip", "reference_name": "SS-1"}
    retry.others = [{"parent": "D-1", "allocated_amount": 250000, "request_status": "Paid"}]
    request = retry.failed(doctype="Salary Slip", reference="SS-1")

    request.retry_failed_payment()

    assert len(retry.sends) == 1


def test_retry_only_for_failed_requests_still_checked_first(retry):
    request = retry.failed()
    retry.frappe.db.table(REQ)["B2C-1"]["status"] = "Paid"
    with pytest.raises(fake_frappe.ValidationError, match="Only submitted, failed"):
        request.retry_failed_payment()
    assert retry.frappe.calls == [] and retry.sends == []


# ---------------------------------------------------------------- C28: queued after commit
@pytest.fixture
def connector(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.db.commit = lambda: None
        frappe.after_commit = []
        frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
        mod = importlib.import_module(CONNECTOR)
        monkeypatch.setattr(mod, "update_integration_request", lambda *a, **kw: None)
        monkeypatch.setattr(mod, "create_request_log", lambda **kw: fake_frappe._dict(name="IR-1"))
        monkeypatch.setattr(fake_frappe.FakeDoc, "save", lambda self, *a, **kw: None, raising=False)

        def run(method, kw):
            module, fn = method.rsplit(".", 1)
            getattr(importlib.import_module(module), fn)(**kw)

        def enqueue(method, enqueue_after_commit=False, queue=None, timeout=None, **kw):
            if enqueue_after_commit:
                frappe.after_commit.append((method, kw))
                return
            # a free worker starts it now, in its own transaction: it sees only committed data
            live = frappe.db.tables
            frappe.db.tables = copy.deepcopy(frappe.committed)
            try:
                run(method, kw)
                seen = frappe.db.tables
            finally:
                frappe.db.tables = live
            for doctype, rows in seen.items():  # its own commit
                for name, row in rows.items():
                    if row != frappe.committed.get(doctype, {}).get(name):
                        live.setdefault(doctype, {}).setdefault(name, {}).update(row)

        def commit():
            frappe.committed = copy.deepcopy(frappe.db.tables)
            while frappe.after_commit:
                run(*frappe.after_commit.pop(0))

        frappe.enqueue = enqueue
        frappe.db.table(REF)["R1"] = {"name": "R1", "payment_status": "Initiated"}
        frappe.db.table(REQ)["B2C-1"] = {
            "name": "B2C-1", "docstatus": 1, "status": "Not Initiated", "originator_conversation_id": "OCID-1",
            "b2c_payment_disbursement": "D-1", "b2c_payment_disbursement_reference": "R1",
        }
        frappe.committed = copy.deepcopy(frappe.db.tables)
        conn = object.__new__(mod.MpesaB2CConnector)
        conn.provider, conn._base_url = "Mpesa", "https://sandbox.safaricom.co.ke"
        yield fake_frappe._dict(frappe=frappe, conn=conn, commit=commit)
    finally:
        fake_frappe.uninstall(saved)


def test_daraja_error_on_send_marks_the_row_failed(connector):
    response = types.SimpleNamespace(status_code=400)
    connector.conn._handle_error(
        response=response,
        data={"errorCode": "400.002.02", "errorMessage": "Bad Request - Invalid PartyB"},
        document_name="B2C-1",
        integration_request=fake_frappe._dict(name="IR-1"),
    )
    connector.commit()  # the send job (or the retry request) commits

    assert connector.frappe.db.table(REQ)["B2C-1"]["status"] == "Failed"
    assert connector.frappe.db.table(REF)["R1"]["payment_status"] == "Failed"


def test_failed_result_marks_the_row_failed(connector):
    connector.frappe.db.table(REQ)["B2C-1"]["status"] = "Initiated"
    connector.frappe.committed = copy.deepcopy(connector.frappe.db.tables)

    connector.conn.handle_callback(
        {"ResultType": 0, "ResultCode": 2001, "ResultDesc": "The initiator information is invalid.",
         "OriginatorConversationID": "OCID-1", "ConversationID": "AG_1", "TransactionID": "SKT0000000"}
    )
    connector.commit()  # the callback request commits

    assert connector.frappe.db.table(REQ)["B2C-1"]["status"] == "Failed"
    assert connector.frappe.db.table(REF)["R1"]["payment_status"] == "Failed"
