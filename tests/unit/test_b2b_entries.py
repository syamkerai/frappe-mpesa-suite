"""Booking Paid B2B requests and Make Entry (money_out.b2b_entries), on an in-memory site whose
SQL runs on SQLite.

Covers: the reference rows (plain, capped, payment terms in due order, a document that can no
longer take the money); the hand-built Payment Entry (the payout's GL whatever the invoice's
Mode of Payment, the completion date, the receipt, remarks, dimensions, no withholding); every
Accounting Entry state; the automatic booking's branches, run as Administrator, under the
locks in R4 order, idempotent, an error recorded and alerted once; Make Entry for each kind with
its permission and move rules and the Journal Entry fields per version; the 15-minute sweep
(legacy rows never booked, the 30-day window, one reminder); and the after_migrate backfill.
"""

import datetime

import pytest

import b2b_fixture
import fake_frappe

PI, PO, B2B = "Purchase Invoice", "Purchase Order", b2b_fixture.B2B
GL = b2b_fixture.GL
COMPLETED = datetime.date(2026, 10, 17)


@pytest.fixture
def env(monkeypatch):
    with b2b_fixture.site(monkeypatch) as env:
        env.frappe.session.user = "accounts@example.com"
        env.frappe.user_roles = {"Accounts User"}
        yield env


def doc(env, name="MB2B-1"):
    return env.frappe.get_doc(B2B, name)


def row(env, name="MB2B-1"):
    return env.frappe.db.table(B2B)[name]


# ---------------------------------------------------------------- reference rows
def test_reference_row_takes_the_payout(env):
    env.pi(outstanding_amount=10000)
    assert env.entries.reference_rows(PI, "PINV-1", 4000) == (
        [{"reference_doctype": PI, "reference_name": "PINV-1", "allocated_amount": 4000}], None)
    assert ("row", PI, "PINV-1", True) in env.log  # the document row, locked


def test_reference_row_is_capped_and_the_rest_stays_unallocated(env):
    env.pi(outstanding_amount=3000)
    assert env.entries.reference_rows(PI, "PINV-1", 4000)[0][0]["allocated_amount"] == 3000
    assert env.entries.reference_rows(PI, "PINV-1", 4000, cap=2500)[0][0]["allocated_amount"] == 2500


def test_what_the_document_cannot_take_is_a_possible_double_payment(env):
    # PINV-1 owed 10,000; 4,000 was paid by bank (or a debit note made) while M-Pesa paid 10,000
    env.pi(outstanding_amount=6000)
    rows, problem = env.entries.reference_rows(PI, "PINV-1", 10000)
    assert [r["allocated_amount"] for r in rows] == [6000]
    assert problem == ("PINV-1 could take only KES 6,000 of this payment, so KES 4,000 is an unallocated supplier "
                       "advance. Check that the supplier was not paid twice.")
    assert env.entries.reference_rows(PI, "PINV-1", 6000)[1] is None  # it takes it all


def test_order_takes_its_total_less_advances(env):
    env.po(rounded_total=20000, advance_paid=18000)
    assert env.entries.reference_rows(PO, "PO-1", 4000)[0][0]["allocated_amount"] == 2000


def test_payment_terms_are_paid_in_due_order(env):
    env.pi(outstanding_amount=8333.38, payment_terms_template="30-60")
    env.frappe.db.table("Payment Terms Template")["30-60"] = {
        "name": "30-60", "allocate_payment_based_on_payment_terms": 1}
    schedule = env.frappe.db.table("Payment Schedule")
    schedule["S2"] = {"name": "S2", "parent": "PINV-1", "parenttype": PI, "idx": 2, "payment_term": "Balance",
                      "due_date": "2026-11-30", "payment_amount": 5000, "paid_amount": 0, "outstanding": 5000}
    schedule["S1"] = {"name": "S1", "parent": "PINV-1", "parenttype": PI, "idx": 1, "payment_term": "Deposit",
                      "due_date": "2026-10-31", "payment_amount": 3000, "paid_amount": 2999.95, "outstanding": 0.05}
    schedule["S3"] = {"name": "S3", "parent": "PINV-1", "parenttype": PI, "idx": 3, "payment_term": "Delivery",
                      "due_date": "2026-10-31", "payment_amount": 333.333, "paid_amount": 0, "outstanding": 333.33}
    rows, problem = env.entries.reference_rows(PI, "PINV-1", 4000)
    # Deposit's 0.05 is skipped (0.1 or less, as ERPNext does); Delivery before Balance (same day: idx)
    assert [(r["payment_term"], r["allocated_amount"]) for r in rows] == [("Delivery", 333.33), ("Balance", 3666.67)]
    assert problem is None


@pytest.mark.parametrize("dt, values, reason", [
    (PI, {"outstanding_amount": 0}, "nothing is left to pay on it"),
    (PI, {"on_hold": 1}, "it is on hold"),
    (PI, {"docstatus": 2}, "it is cancelled"),
    (PI, {"is_return": 1}, "it is a debit note"),
    (PO, {"status": "Closed"}, "it is Closed"),
    (PO, {"status": "On Hold"}, "it is On Hold"),
    (PO, {"per_billed": 100}, "it is fully billed"),
    (PO, {"advance_paid": 20000}, "nothing is left to pay on it"),
])
def test_a_document_that_can_no_longer_take_the_money_gets_no_row(env, dt, values, reason):
    name = env.pi(**values)["name"] if dt == PI else env.po(**values)["name"]
    rows, problem = env.entries.reference_rows(dt, name, 4000)
    assert rows == [] and f"({reason})" in problem and "Check that the supplier was not paid twice." in problem


# ---------------------------------------------------------------- the Payment Entry
def test_payment_entry_pays_from_the_payouts_gl_on_the_completion_date(env):
    env.pi(mode_of_payment="Cash", project="Nairobi Depot")  # ERPNext would swap in the Cash account (F5)
    env.request(purchase_invoice="PINV-1", transaction_id="QKA81LK5CY")
    pe, problem = env.entries.build_payment_entry(doc(env), PI, "PINV-1", "ACME", 4000)
    assert (pe.payment_type, pe.party_type, pe.party, pe.company) == ("Pay", "Supplier", "ACME", "Astral")
    assert (pe.paid_from, pe.paid_to, pe.bank_account) == (GL, "Creditors - AIL", b2b_fixture.BANK_ACCOUNT)
    assert (pe.paid_amount, pe.received_amount, pe.source_exchange_rate, pe.target_exchange_rate) == (4000, 4000, 1, 1)
    assert (pe.posting_date, pe.reference_date, pe.reference_no) == (COMPLETED, COMPLETED, "QKA81LK5CY")
    assert pe.mode_of_payment == f"Mpesa-{b2b_fixture.SETTINGS}"  # a label only
    assert pe.cost_center == "Branch - AIL" and pe.project == "Nairobi Depot"
    assert pe.custom_remarks == 1
    assert pe.remarks == "M-Pesa B2B BusinessPayBill to 888880 (MB2B-1), receipt QKA81LK5CY, for PINV-1"
    assert "apply_tds" not in pe._row and "apply_tax_withholding_amount" not in pe._row  # left to ERPNext's 0 (F6)
    assert [r.allocated_amount for r in pe.references] == [4000] and problem is None


def test_payment_entry_without_a_document_is_a_supplier_advance(env):
    env.request()
    pe, problem = env.entries.build_payment_entry(doc(env), None, None, "ACME", 4000)
    assert pe.paid_to == "Creditors - AIL" and not pe.get("references") and problem is None
    assert pe.cost_center == "Main - AIL"  # neither the document nor Mpesa Settings has one: the company's


def test_payment_entry_refuses_an_account_not_in_kes(env):
    env.pi()
    env.frappe.db.table("Account")["Creditors - AIL"]["account_currency"] = "USD"
    env.request(purchase_invoice="PINV-1")
    with pytest.raises(env.frappe.ValidationError, match="Creditors - AIL is in USD. M-Pesa pays KES only."):
        env.entries.build_payment_entry(doc(env), PI, "PINV-1", "ACME", 4000)


# ---------------------------------------------------------------- Accounting Entry states
def classify(env, gl=GL):
    d = doc(env)
    return env.entries.classify(d, env.entries.receipt_entries(d.transaction_id), gl)


def test_classify_not_paid_is_blank(env):
    env.request(status="Initiated")
    assert classify(env) == ("", None, "")


def test_classify_no_entry(env):
    env.request(purchase_invoice="PINV-1")
    assert classify(env) == ("Entry Not Made", None, None)


def test_classify_no_entry_on_a_cancelled_request_needs_a_manager(env):
    env.request(purchase_invoice="PINV-1", docstatus=2)
    state, entry, note = classify(env)
    assert state == "Needs Checking" and entry is None
    assert note == ("M-Pesa paid this after the request was cancelled. Check the statement and that PINV-1 was not "
                    "paid again, then make the entry (managers only).")


def test_classify_two_entries(env):
    env.request(purchase_invoice="PINV-1", transaction_id="R1")
    env.pe("ACC-PAY-1", "R1", [(PI, "PINV-1", 4000)])
    env.je("ACC-JV-1", "R1", [{"account": "Rent", "debit_in_account_currency": 4000},
                              {"account": GL, "credit_in_account_currency": 4000}])
    state, _, note = classify(env)
    assert state == "Needs Checking" and "is on 2 entries (Payment Entry ACC-PAY-1, Journal Entry ACC-JV-1)" in note


@pytest.mark.parametrize("refs, where", [([(PI, "PINV-B", 4000)], "on PINV-B"), ([], "not allocated")])
def test_classify_off_target(env, refs, where):
    env.pi()
    env.request(purchase_invoice="PINV-1", transaction_id="R1")
    env.pe("ACC-PAY-1", "R1", refs)
    assert classify(env)[0::2] == (
        "Needs Checking", f"Receipt R1 is on Payment Entry ACC-PAY-1 ({where}): allocate it to PINV-1 or correct it.")


def test_classify_draft(env):
    env.request(purchase_invoice="PINV-1", transaction_id="R1")
    env.pe("ACC-PAY-1", "R1", [(PI, "PINV-1", 4000)], docstatus=0)
    state, entry, note = classify(env)
    assert (state, entry.name, note) == ("Draft Entry", "ACC-PAY-1", "Submit Payment Entry ACC-PAY-1.")


@pytest.mark.parametrize("values", [{"paid_from": "Cash - AIL"}, {"paid_amount": 4002}])
def test_classify_another_gl_or_amount_will_not_reconcile(env, values):
    env.pi(outstanding_amount=0)
    env.request(purchase_invoice="PINV-1", transaction_id="R1")
    env.pe("ACC-PAY-1", "R1", [(PI, "PINV-1", 4000)], **values)
    state, _, note = classify(env)
    assert state == "Needs Checking" and note.endswith(f"the M-Pesa line is 4,000 on {GL}, so it will not reconcile.")


def test_classify_unallocated_while_the_invoice_still_owes(env):
    env.pi(outstanding_amount=1000)
    env.request(purchase_invoice="PINV-1", transaction_id="R1")
    env.pe("ACC-PAY-1", "R1", [(PI, "PINV-1", 3000)])
    assert classify(env)[0::2] == (
        "Needs Checking", "KES 1,000 is not allocated: allocate it to PINV-1 (Get Advances or Payment Reconciliation).")


def test_classify_made(env):
    env.pi(outstanding_amount=0)
    env.request(purchase_invoice="PINV-1", transaction_id="R1")
    env.pe("ACC-PAY-1", "R1", [(PI, "PINV-1", 4000)])
    assert classify(env)[0::2] == ("Entry Made", "Booked on Payment Entry ACC-PAY-1.")


def test_classify_part_left_off_the_document_needs_checking(env):
    # the invoice owed 3,000 when this 4,000 was booked: the rest may pay it twice
    env.pi(outstanding_amount=0)
    env.request(purchase_invoice="PINV-1", transaction_id="R1")
    env.pe("ACC-PAY-1", "R1", [(PI, "PINV-1", 3000)])
    assert classify(env)[0::2] == ("Needs Checking", (
        "PINV-1 has nothing left to pay, and KES 1,000 of this payment is not on it (Payment Entry ACC-PAY-1). "
        "Check that the supplier was not paid twice. Then allocate the rest (Payment Reconciliation)."))
    # the note booking wrote (it names why) is kept while it names this amount
    written = ("PINV-1 could take only KES 3,000 of this payment, so KES 1,000 is an unallocated supplier advance. "
               "Check that the supplier was not paid twice.")
    row(env).update(entry_status="Needs Checking", entry_note=written)
    assert classify(env)[2] == written
    env.frappe.db.table("Payment Entry")["ACC-PAY-1"]["unallocated_amount"] = 600  # 400 allocated elsewhere since
    assert classify(env)[2].startswith("PINV-1 has nothing left to pay, and KES 600 of this payment is not on it")


def test_classify_an_advance_without_a_document_is_made(env):
    env.request(transaction_id="R1")
    env.pe("ACC-PAY-1", "R1", [])
    assert classify(env)[0::2] == ("Entry Made", "KES 4,000 is an unallocated supplier advance (Payment Entry ACC-PAY-1).")


def test_classify_an_order_advance_moved_to_the_invoice_is_still_made(env):
    env.po(advance_paid=4000)
    env.request(purchase_order="PO-1", transaction_id="R1")
    env.pe("ACC-PAY-1", "R1", [(PI, "PINV-9", 4000, PO, "PO-1")], unallocated_amount=0)
    assert classify(env)[0] == "Entry Made"


def test_classify_without_a_document(env):
    env.request(transaction_id="R1")
    env.pe("ACC-PAY-1", "R1", [])
    assert classify(env)[0] == "Entry Made"  # an advance to the request's supplier
    env.frappe.db.table("Payment Entry")["ACC-PAY-1"]["party"] = "Someone Else"
    assert classify(env)[0] == "Needs Checking"
    env.frappe.db.table("Payment Entry")["ACC-PAY-1"]["docstatus"] = 2
    env.je("ACC-JV-1", "R1", [{"account": "Rent - AIL", "debit_in_account_currency": 4000},
                              {"account": GL, "credit_in_account_currency": 4000}])
    assert classify(env)[0::2] == ("Entry Made", "Booked on Journal Entry ACC-JV-1.")


def test_refresh_clears_a_cancelled_entry_and_says_so(env):
    env.request(purchase_invoice="PINV-1", transaction_id="R1", payment_entry="ACC-PAY-1", entry_status="Entry Made",
                entry_mode="Book automatically")
    env.pe("ACC-PAY-1", "R1", [(PI, "PINV-1", 4000)], docstatus=2)
    assert env.entries.refresh_entry(doc(env)) == "Entry Not Made"
    assert row(env)["payment_entry"] is None
    assert (B2B, "MB2B-1", "Payment Entry ACC-PAY-1 was cancelled. It is not booked again automatically: press "
            "Make Entry.") in env.frappe.comments
    # staff may have undone it because it went to the wrong invoice: it waits for Make Entry now
    assert row(env)["entry_mode"] == "Make the entry later"
    assert row(env)["entry_note"] == "Paid on 2026-10-17. Press Make Entry when you know what this paid for."
    assert env.fed == ["MB2B-1"]  # the line is looked at again


def test_refresh_says_when_a_linked_entry_is_gone(env):
    env.request(transaction_id="R1", journal_entry="ACC-JV-9", entry_status="Entry Made")
    env.entries.refresh_entry(doc(env))
    assert row(env)["journal_entry"] is None
    assert any(c[2].startswith("Journal Entry ACC-JV-9 was deleted.") for c in env.frappe.comments)


# ---------------------------------------------------------------- automatic booking
def test_book_automatically_books_the_invoice_as_administrator_under_the_locks(env):
    env.pi(outstanding_amount=10000)
    env.request(purchase_invoice="PINV-1", transaction_id="R1")
    assert env.entries.book_automatically("MB2B-1") == "ACC-PAY-0001"
    assert env.inserted == [("Payment Entry", "Administrator", True)] and env.users[0] == "Administrator"
    assert env.frappe.session.user == "accounts@example.com"
    r = row(env)
    assert (r["entry_status"], r["payment_entry"], r["entry_note"]) == (
        "Entry Made", "ACC-PAY-0001", "Booked on Payment Entry ACC-PAY-0001.")
    assert r["entry_attempted_on"] == b2b_fixture.NOW
    assert env.frappe.db.table("Payment Entry")["ACC-PAY-0001"]["docstatus"] == 1  # submit switch on
    assert env.log[0] == ("row", B2B, "MB2B-1", False)  # the request is read first, without a lock
    order = [e for e in env.log if e[0] == "lock" or (e[0] == "row" and e[3])]
    # R4: the document's lock, the receipt's lock, the request row, the document row last
    assert order == [("lock", "b2c-pay:Purchase Invoice:PINV-1"), ("lock", "book:R1"), ("row", B2B, "MB2B-1", True),
                     ("row", PI, "PINV-1", True)]
    assert env.entries.book_automatically("MB2B-1") == "ACC-PAY-0001"  # a re-run books nothing
    assert len(env.inserted) == 1


def test_book_automatically_leaves_a_draft_when_entries_are_reviewed(env):
    # the switch is always on since 2026-10-28 (migrate.post_entries_at_once); a site whose switch is still off
    # (before its migrate) keeps the draft path
    env.frappe.db.table("Mpesa Settings")[b2b_fixture.SETTINGS]["submit_b2c_accounting_entries"] = 0
    env.request(transaction_id="R1")
    env.entries.book_automatically("MB2B-1")
    assert row(env)["entry_status"] == "Draft Entry"


@pytest.mark.parametrize("values, note", [
    ({"entry_mode": "Make the entry later"}, "Paid on 2026-10-17. Press Make Entry when you know what this paid for."),
    ({"supplier": None}, "Paid to a number not saved on a supplier: press Make Entry."),
])
def test_book_automatically_books_nothing_when_told_or_unable(env, values, note):
    env.request(transaction_id="R1", **values)
    assert env.entries.book_automatically("MB2B-1") is None
    assert env.inserted == [] and (row(env)["entry_status"], row(env)["entry_note"]) == ("Entry Not Made", note)


def test_book_automatically_waits_for_a_supplier_on_hold(env):
    env.frappe.db.table("Supplier")["ACME"].update(on_hold=1, hold_type="Payments", release_date=None)
    env.request(transaction_id="R1")
    assert env.entries.book_automatically("MB2B-1") is None and env.inserted == []
    assert row(env)["entry_note"].startswith("ACME is on hold for payments in ERPNext. Release the hold;")


def test_book_automatically_never_books_a_cancelled_request_and_alerts_once(env):
    # the Result wrote "Entry Not Made" and an earlier alert went out (the reminder, say)
    env.request(transaction_id="R1", docstatus=2, purchase_invoice="PINV-1", entry_status="Entry Not Made",
                entry_note="Booking the entry…", entry_alerted_on=b2b_fixture.NOW)
    env.entries.book_automatically("MB2B-1")
    env.entries.book_automatically("MB2B-1")
    assert env.inserted == [] and row(env)["entry_status"] == "Needs Checking"
    # always told, whatever was alerted before; once, when it becomes Needs Checking
    assert len(env.alerts) == 1 and "M-Pesa paid this after the request was cancelled" in env.alerts[0][1]


def test_book_automatically_links_an_entry_that_already_carries_the_receipt(env):
    env.request(transaction_id="R1")
    env.pe("ACC-PAY-7", "R1", [])  # made by hand, as a supplier advance
    assert env.entries.book_automatically("MB2B-1") == "ACC-PAY-7"
    assert env.inserted == [] and (row(env)["payment_entry"], row(env)["entry_status"]) == ("ACC-PAY-7", "Entry Made")


def test_book_automatically_records_an_error_once_and_alerts_once(env):
    env.request(transaction_id="R1")
    env.fail_insert = env.frappe.ValidationError("Accounting Period is closed")
    for _ in range(2):  # the sweep tries again
        assert env.entries.book_automatically("MB2B-1") is None
    assert ("rollback", "mpesa_b2b_pe") in env.log
    assert row(env)["entry_status"] == "Entry Not Made"
    assert row(env)["entry_note"] == (
        "Could not book automatically: traceback. It is tried again every 15 minutes for 30 days, or press Make "
        "Entry. Do not make a Payment Entry for it by hand.")
    assert len([e for e in env.frappe.errors if e[1].get("title") == "M-Pesa B2B booking failed"]) == 1
    assert len(env.alerts) == 1


def test_book_automatically_after_the_invoice_was_paid_elsewhere_needs_checking(env):
    env.pi(outstanding_amount=0)  # paid by bank between send and Result
    env.request(purchase_invoice="PINV-1", transaction_id="R1")
    assert env.entries.book_automatically("MB2B-1") == "ACC-PAY-0001"
    pe = env.frappe.db.table("Payment Entry")["ACC-PAY-0001"]
    assert pe["unallocated_amount"] == 4000 and not env.frappe.db.table("Payment Entry Reference")
    assert row(env)["entry_status"] == "Needs Checking"
    assert "PINV-1 could no longer take this payment (nothing is left to pay on it)" in row(env)["entry_note"]
    assert len(env.alerts) == 1 and any("paid twice" in c[2] for c in env.frappe.comments)


def test_a_possible_double_payment_is_alerted_after_an_earlier_booking_failure(env):
    # 1. booking fails (the period is closed): one alert, entry_alerted_on set
    env.pi(outstanding_amount=10000)
    env.request(purchase_invoice="PINV-1", transaction_id="R1")
    env.fail_insert = env.frappe.ValidationError("Accounting Period is closed")
    env.entries.book_automatically("MB2B-1")
    assert len(env.alerts) == 1 and row(env)["entry_alerted_on"]
    # 2. staff pay the invoice by hand meanwhile; 3. the period reopens and the sweep books again
    env.frappe.db.table(PI)["PINV-1"]["outstanding_amount"] = 0
    env.fail_insert = None
    env.entries.book_automatically("MB2B-1")
    assert row(env)["entry_status"] == "Needs Checking"
    assert len(env.alerts) == 2 and "Check that the supplier was not paid twice." in env.alerts[1][1]


def test_book_automatically_when_the_invoice_was_paid_in_part_elsewhere_needs_checking(env):
    # PINV-1 owed 10,000; staff paid 4,000 by bank between send and Result
    env.pi(outstanding_amount=6000)
    env.request(purchase_invoice="PINV-1", transaction_id="R1", amount=10000, entry_status="Entry Not Made",
                entry_note="Booking the entry…", entry_attempted_on=b2b_fixture.NOW, entry_alerted_on=b2b_fixture.NOW)
    assert env.entries.book_automatically("MB2B-1") == "ACC-PAY-0001"
    assert [r["allocated_amount"] for r in env.frappe.db.table("Payment Entry Reference").values()] == [6000]
    note = ("PINV-1 could take only KES 6,000 of this payment, so KES 4,000 is an unallocated supplier advance. "
            "Check that the supplier was not paid twice.")
    assert (row(env)["entry_status"], row(env)["entry_note"]) == ("Needs Checking", note)
    assert (B2B, "MB2B-1", note) in env.frappe.comments
    assert env.alerts == [("MB2B-1", f"MB2B-1: {note}")]  # whatever was alerted before
    assert len([e for e in env.frappe.errors if e[1].get("title") == "M-Pesa B2B entry needs checking"]) == 1
    env.frappe.db.table(PI)["PINV-1"]["outstanding_amount"] = 0  # what ERPNext shows once it is submitted
    env.entries.refresh_entries()  # the sweep keeps the state and the note, and logs nothing more
    assert (row(env)["entry_status"], row(env)["entry_note"]) == ("Needs Checking", note)
    assert len([e for e in env.frappe.errors if e[1].get("title") == "M-Pesa B2B entry needs checking"]) == 1


def test_book_automatically_needs_a_paid_request(env):
    env.request(status="Initiated", transaction_id=None)
    assert env.entries.book_automatically("MB2B-1") is None and env.inserted == []


# ---------------------------------------------------------------- Make Entry
def make(env, kind, **kw):
    return env.entries.make_entry("MB2B-1", kind, **kw)


def test_make_entry_against_an_invoice_allocates_what_is_left(env):
    env.pi(outstanding_amount=10000)
    env.request(transaction_id="R1", entry_mode="Make the entry later", entry_status="Entry Not Made")
    env.request("MB2B-0009", purchase_invoice="PINV-1", amount=7000, status="Initiated", transaction_id=None)
    result = make(env, PI, purchase_invoice="PINV-1")
    assert result == {"doctype": "Payment Entry", "name": "ACC-PAY-0001", "docstatus": 1,
                      "entry_status": "Needs Checking"}  # 3,000 allocated, 1,000 left unallocated
    assert [r["allocated_amount"] for r in env.frappe.db.table("Payment Entry Reference").values()] == [3000]
    assert env.inserted == [("Payment Entry", "accounts@example.com", False)]  # the user's own permissions
    assert row(env)["purchase_invoice"] == "PINV-1" and env.fed == ["MB2B-1"]
    note = ("PINV-1 could take only KES 3,000 of this payment, so KES 1,000 is an unallocated supplier advance. "
            "Check that the supplier was not paid twice.")
    assert row(env)["entry_note"] == note and (B2B, "MB2B-1", note) in env.frappe.comments
    assert env.alerts == [("MB2B-1", f"MB2B-1: {note}")]


def test_make_entry_against_an_order(env):
    env.po()
    env.request(transaction_id="R1", entry_mode="Make the entry later")
    assert make(env, PO, purchase_order="PO-1")["entry_status"] == "Entry Made"
    assert row(env)["purchase_order"] == "PO-1"


def test_make_entry_moves_an_order_payout_onto_the_invoice_made_from_it(env):
    # paid first on PO-1 ("Make the entry later"), then PINV-1 was made from PO-1
    env.po()
    env.pi(outstanding_amount=10000)
    env.frappe.db.table("Purchase Invoice Item")["PII-1"] = {
        "name": "PII-1", "parent": "PINV-1", "parenttype": PI, "purchase_order": "PO-1"}
    env.request(transaction_id="R1", purchase_order="PO-1", amount=10000, entry_mode="Make the entry later")
    env.frappe.user_roles = {"M-Pesa Manager"}  # moving a payout to another document
    assert make(env, PI, purchase_invoice="PINV-1")["entry_status"] == "Entry Made"
    assert [r["allocated_amount"] for r in env.frappe.db.table("Payment Entry Reference").values()] == [10000]
    assert (row(env)["purchase_invoice"], row(env)["purchase_order"]) == ("PINV-1", None)


def test_make_entry_as_a_supplier_advance_fills_the_supplier(env):
    env.request(transaction_id="R1", supplier=None, entry_mode="Make the entry later")
    with pytest.raises(env.frappe.ValidationError, match="Pick the supplier."):
        make(env, "Supplier Advance")
    assert make(env, "Supplier Advance", supplier="ACME")["entry_status"] == "Entry Made"
    assert row(env)["supplier"] == "ACME"


@pytest.mark.parametrize("fields, custom_remark", [(None, True), ({"user_remark", "cheque_no"}, False)])
def test_make_entry_as_a_journal_entry(env, fields, custom_remark):
    env.meta_fields["Journal Entry"] = fields  # v16 has custom_remark; v15 does not
    env.frappe.db.table("Account")["Rent - AIL"] = {"name": "Rent - AIL", "company": "Astral",
                                                   "report_type": "Profit and Loss", "account_currency": "KES"}
    env.request(transaction_id="R1", entry_mode="Make the entry later")
    result = make(env, "Journal Entry", account="Rent - AIL", cost_center="Main - AIL", remark="October rent")
    je = env.frappe.db.table("Journal Entry")[result["name"]]
    text = "M-Pesa B2B BusinessPayBill to 888880 (MB2B-1), receipt R1. October rent"
    assert (je["voucher_type"], je["cheque_no"], je["cheque_date"], je["posting_date"]) == (
        "Bank Entry", "R1", COMPLETED, COMPLETED)
    assert je["user_remark"] == text
    assert (je.get("custom_remark"), je.get("remark")) == ((1, text) if custom_remark else (None, None))
    accounts = sorted((a["account"], a["debit_in_account_currency"], a["credit_in_account_currency"],
                       a.get("bank_account")) for a in env.frappe.db.table("Journal Entry Account").values())
    assert accounts == [(GL, 0, 4000, b2b_fixture.BANK_ACCOUNT), ("Rent - AIL", 4000, 0, None)]
    assert result["entry_status"] == "Entry Made"


@pytest.mark.parametrize("account, values, message", [
    ("Creditors - AIL", {}, "is a Payable account: use Purchase Invoice"),
    ("Expenses - AIL", {"is_group": 1, "report_type": "Profit and Loss"}, "is a group account"),
    (GL, {}, "is the M-Pesa account this payment left"),
    ("Rent - AIL", {"report_type": "Profit and Loss"}, "Pick a Cost Center for Rent - AIL."),
    ("Other Co Rent", {"company": "Other Co"}, "is not in Astral"),
])
def test_journal_entry_account_rules(env, account, values, message):
    accounts = env.frappe.db.table("Account")
    accounts.setdefault(account, {"name": account, "company": "Astral", "account_currency": "KES"}).update(values)
    env.request(transaction_id="R1")
    with pytest.raises(env.frappe.ValidationError, match=message):
        make(env, "Journal Entry", account=account)
    assert env.inserted == []


def test_make_entry_refused_when_the_receipt_is_on_another_entry(env):
    env.request(transaction_id="R1")
    env.pe("ACC-PAY-7", "R1", [(PI, "PINV-9", 4000)])
    with pytest.raises(env.frappe.ValidationError,
                       match="Receipt R1 is already on Payment Entry ACC-PAY-7 \\(on PINV-9\\): open it, or press Check"):
        make(env, "Supplier Advance")
    assert env.inserted == []


def test_make_entry_needs_a_finance_role_and_erpnexts_permission(env):
    env.request(transaction_id="R1")
    env.frappe.user_roles = {"M-Pesa Payer"}
    with pytest.raises(env.frappe.PermissionError):
        make(env, "Supplier Advance")
    env.frappe.user_roles = {"Accounts User"}
    env.denied.add(("Payment Entry", "create"))
    with pytest.raises(env.frappe.PermissionError):
        make(env, "Supplier Advance")
    assert env.inserted == []


def test_moving_a_payout_to_another_document_needs_a_manager(env):
    env.pi("PINV-1")
    env.pi("PINV-2")
    env.request(transaction_id="R1", purchase_invoice="PINV-1")
    with pytest.raises(env.frappe.PermissionError):
        make(env, PI, purchase_invoice="PINV-2")
    env.frappe.user_roles = {"M-Pesa Manager"}
    make(env, PI, purchase_invoice="PINV-2")
    assert row(env)["purchase_invoice"] == "PINV-2"
    assert any("moved this payment from Purchase Invoice PINV-1 to Purchase Invoice PINV-2" in c[2]
               for c in env.frappe.comments)


def test_entry_for_a_cancelled_request_needs_a_manager(env):
    env.request(transaction_id="R1", docstatus=2)
    with pytest.raises(env.frappe.PermissionError):
        make(env, "Supplier Advance")
    env.frappe.user_roles = {"M-Pesa Manager"}
    assert make(env, "Supplier Advance")["entry_status"] == "Entry Made"


def test_make_entry_refuses_an_invoice_that_cannot_be_paid(env):
    env.pi(on_hold=1)
    env.request(transaction_id="R1")
    with pytest.raises(env.frappe.ValidationError, match="PINV-1 is on hold"):
        make(env, PI, purchase_invoice="PINV-1")
    env.pi(outstanding_amount=4000)
    env.request("MB2B-0009", purchase_invoice="PINV-1", amount=4000, status="Initiated", transaction_id=None)
    with pytest.raises(env.frappe.ValidationError, match="Nothing is left to pay on PINV-1"):
        make(env, PI, purchase_invoice="PINV-1")


def test_check_entry_again(env):
    env.request(transaction_id="R1", entry_status="Entry Not Made")
    env.pe("ACC-PAY-7", "R1", [])
    assert env.entries.refresh_entry_now("MB2B-1")["entry_status"] == "Entry Made"
    env.frappe.user_roles = {"M-Pesa Viewer"}
    with pytest.raises(env.frappe.PermissionError):
        env.entries.refresh_entry_now("MB2B-1")


# ---------------------------------------------------------------- the sweep and the upgrade
def test_sweep_books_a_failed_booking_again_but_never_a_legacy_row(env, monkeypatch):
    env.request("MB2B-0001", transaction_id="R1", entry_status="Entry Not Made", entry_attempted_on=b2b_fixture.NOW)
    env.request("MB2B-0002", transaction_id="R2", entry_status=None)  # paid before this release
    env.request("MB2B-0003", transaction_id="R3", entry_status="Entry Not Made", entry_attempted_on=b2b_fixture.NOW,
                creation=datetime.datetime(2026, 9, 1))  # older than 30 days
    env.request("MB2B-0004", transaction_id="R4", entry_status="Entry Made", payment_entry="ACC-PAY-4")
    env.pe("ACC-PAY-4", "R4", [])
    env.entries.refresh_entries()
    booked = {p["reference_no"] for p in env.frappe.db.table("Payment Entry").values()} - {"R4"}
    assert booked == {"R1"}
    assert row(env, "MB2B-0002")["entry_status"] == "Entry Not Made" and not row(env, "MB2B-0002").get(
        "entry_attempted_on")
    assert row(env, "MB2B-0003")["entry_status"] == "Entry Not Made"
    assert ("commit",) in env.log


def test_sweep_books_a_request_whose_booking_job_was_lost(env):
    # the Result wrote the state and the marker; its job failed before it committed (a lock
    # timeout) or never ran (Redis down)
    env.pi(outstanding_amount=10000)
    env.request(purchase_invoice="PINV-1", transaction_id="R1", entry_status="Entry Not Made",
                entry_note="Booking the entry…", entry_attempted_on=b2b_fixture.NOW, entry_mode="Book automatically")
    env.entries.refresh_entries()
    assert env.inserted == [("Payment Entry", "Administrator", True)]
    assert (row(env)["entry_status"], row(env)["payment_entry"]) == ("Entry Made", "ACC-PAY-0001")
    assert env.fed == ["MB2B-1"]


def test_sweep_gives_a_make_later_request_whose_job_was_lost_its_note(env):
    env.request(transaction_id="R1", entry_status="Entry Not Made", entry_note="Booking the entry…",
                entry_attempted_on=b2b_fixture.NOW, entry_mode="Make the entry later")
    env.entries.refresh_entries()
    assert env.inserted == []
    assert row(env)["entry_note"] == "Paid on 2026-10-17. Press Make Entry when you know what this paid for."


def test_sweep_never_books_again_an_entry_staff_cancelled(env):
    # booked automatically on the wrong invoice; staff cancelled it to make it again by hand
    env.pi(outstanding_amount=10000)
    env.request(purchase_invoice="PINV-1", transaction_id="R1", payment_entry="ACC-PAY-1", entry_status="Entry Made",
                entry_attempted_on=b2b_fixture.NOW, entry_mode="Book automatically")
    env.pe("ACC-PAY-1", "R1", [(PI, "PINV-1", 4000)], docstatus=2)
    env.entries.refresh_entries()
    env.entries.refresh_entries()
    assert env.inserted == []
    assert (row(env)["entry_status"], row(env)["payment_entry"], row(env)["entry_mode"]) == (
        "Entry Not Made", None, "Make the entry later")


def test_a_second_payer_is_told_another_payment_is_being_sent(env):
    env.pi()
    env.request(transaction_id="R1", entry_mode="Make the entry later")
    env.busy_keys.add("b2c-pay:Purchase Invoice:PINV-1")  # a B2B submit holds it through its call to M-Pesa
    with pytest.raises(env.frappe.ValidationError,
                       match="^Another payment for Purchase Invoice PINV-1 is being sent or booked. Try again in a "
                             "minute.$"):
        make(env, PI, purchase_invoice="PINV-1")
    assert env.inserted == []


def test_sweep_alerts_a_payment_after_cancel_whose_booking_job_was_lost(env):
    env.request(transaction_id="R1", docstatus=2, purchase_invoice="PINV-1", entry_status="Entry Not Made",
                entry_note="Booking the entry…", entry_attempted_on=b2b_fixture.NOW)
    env.entries.refresh_entries()
    env.entries.refresh_entries()
    assert env.inserted == [] and row(env)["entry_status"] == "Needs Checking"
    assert len(env.alerts) == 1 and "M-Pesa paid this after the request was cancelled" in env.alerts[0][1]


def test_sweep_links_an_entry_made_by_hand(env):
    env.request(transaction_id="R1", entry_status="Entry Not Made", entry_attempted_on=b2b_fixture.NOW,
                entry_mode="Make the entry later")
    env.je("ACC-JV-1", "R1", [{"account": "Rent - AIL", "debit_in_account_currency": 4000},
                              {"account": GL, "credit_in_account_currency": 4000}])  # Bank Reconciliation Tool
    env.entries.refresh_entries()
    assert (row(env)["journal_entry"], row(env)["entry_status"]) == ("ACC-JV-1", "Entry Made")
    assert env.inserted == [] and env.fed == ["MB2B-1"]


def test_sweep_reminds_once(env, monkeypatch):
    monkeypatch.setattr(env.entries.money_in_common, "suite", lambda: fake_frappe._dict(entry_reminder_days=3))
    env.request(transaction_id="R1", entry_mode="Make the entry later", entry_status="Entry Not Made",
                trans_completed_time=datetime.datetime(2026, 10, 12, 9, 0))
    env.entries.refresh_entries()
    env.entries.refresh_entries()
    assert len(env.alerts) == 1 and "was paid on 2026-10-12 and still has no accounting entry" in env.alerts[0][1]


def test_sweep_reminder_off_and_not_due(env, monkeypatch):
    env.request(transaction_id="R1", entry_mode="Make the entry later", entry_status="Entry Not Made",
                trans_completed_time=datetime.datetime(2026, 10, 12, 9, 0))
    monkeypatch.setattr(env.entries.money_in_common, "suite", lambda: fake_frappe._dict(entry_reminder_days=0))
    env.entries.refresh_entries()
    monkeypatch.setattr(env.entries.money_in_common, "suite", lambda: fake_frappe._dict(entry_reminder_days=10))
    env.entries.refresh_entries()
    assert env.alerts == []


def test_backfill_classifies_old_rows_only_once_and_books_nothing(env):
    env.request("MB2B-0001", transaction_id="R1", entry_status=None, payment_entry="ACC-PAY-1")
    env.pe("ACC-PAY-1", "R1", [])
    env.request("MB2B-0002", transaction_id="R2", entry_status=None)
    env.request("MB2B-0003", transaction_id="R3", entry_status=None, docstatus=2)
    env.entries.backfill_entry_status()
    states = {n: row(env, n)["entry_status"] for n in ("MB2B-0001", "MB2B-0002", "MB2B-0003")}
    assert states == {"MB2B-0001": "Entry Made", "MB2B-0002": "Entry Not Made", "MB2B-0003": "Needs Checking"}
    assert env.inserted == [] and env.fed == [] and env.alerts == [] and env.frappe.errors == []
    before = {n: dict(r) for n, r in env.frappe.db.table(B2B).items()}
    env.entries.backfill_entry_status()
    assert {n: dict(r) for n, r in env.frappe.db.table(B2B).items()} == before


# ---------------------------------------------------------------- entries staff cancel or delete
def test_an_entry_a_request_links_can_be_cancelled(env):
    entry = fake_frappe._dict(doctype="Payment Entry", name="ACC-PAY-1",
                              ignore_linked_doctypes=("GL Entry", "Payment Ledger Entry"))
    env.entries.allow_cancel(entry)
    env.entries.allow_cancel(entry)
    assert entry.ignore_linked_doctypes == ("GL Entry", "Payment Ledger Entry", B2B)
    bare = fake_frappe._dict(doctype="Journal Entry", name="ACC-JV-1")
    env.entries.allow_cancel(bare)
    assert bare.ignore_linked_doctypes == (B2B,)


def test_a_deleted_draft_stops_being_the_requests_entry(env):
    env.request(transaction_id="R1", payment_entry="ACC-PAY-1", entry_status="Draft Entry")
    env.entries.unlink_deleted(fake_frappe._dict(doctype="Payment Entry", name="ACC-PAY-1"))
    assert row(env)["payment_entry"] is None and row(env)["entry_mode"] == "Make the entry later"
    assert (B2B, "MB2B-1", "Payment Entry ACC-PAY-1 was deleted. It is not booked again automatically: press "
            "Make Entry.") in env.frappe.comments
