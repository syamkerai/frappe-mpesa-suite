"""Round 4.5 review of the M-Pesa Bulk Payout sheet (money_out.bulk_payout, money_out.partyless): each finding
that could pay someone twice or book wrongly, as a test. On the in-memory site of test_r45_bulk_payout.

1. A payout whose fate is unknown holds its row however its batch and request were cancelled; the sheet's
   check reads the row's status; a hand-made KES 10 test never pays a row paid or on its way; the row links are
   no_copy (a Duplicate names no paying row); such a request, or a Paid one, is never cancelled.
2. A journal cancelled and amended books its payouts still (the amended copy); a payout a voucher carrying its
   receipt books is booked, never swept into the sheet's journal; one cancelled with no amend is booked again.
3. A sheet's failed payout is retried only by its own Retry Failed (a new request): the request's Retry Payment
   and the batch's Retry Failed Transactions are refused, the first attempt's IDs kept. Mark as Not Paid checks
   self-pay, refuses an acknowledged payout and needs a status query M-Pesa answered.
4. A party-less result takes its document's lock before it writes its row; book_waiting and the unbooked sweep
   hold no row lock while they wait for it.
5. The KES 10 test pays at least the Minimum B2C Payout, and only when the rest can be paid too; the save checks
   the shortcode's Transaction Limit and the minimum.
6. The rows' accounting dimensions are checked as ERPNext checks the journal's lines (mandatory for P&L, the
   company's value, the Accounting Dimension Filters; the credit line's Balance Sheet default).
7. The retry's self-pay check knows the name M-Pesa paid the phone under.
"""

import importlib
import json
import sys
import types
from pathlib import Path

import fake_frappe
import pytest
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the fixture the bulk env extends)
from test_r45_bulk_payout import env as bulk_env  # noqa: F401 (the bulk payout site)
from test_r45_bulk_payout import (APPROVER, DISB, JE, JEA, NOW, PAYER, REQ, ROW, ROWS, SETTINGS, SHEET, WAGES, answer,
                                  as_user, build, journals, paid_names, pay, raises, save, send, sheet, stored_rows)

APP = Path(__file__).resolve().parents[2] / "mpsa_suite"
STATUS_QUERY = "Mpesa B2C Status Query"
LOCK = "b2c-pay:M-Pesa Bulk Payout:MBP-1"
_dict = fake_frappe._dict


@pytest.fixture
def env(bulk_env):  # noqa: F811 (the imported fixture, by its name)
    return bulk_env


def hand_made(env, batch, reference_row="MBP-1-R2", phone="254722000111", amount=2000):
    """A batch made by hand (or by Duplicate) that pays a row of MBP-1."""
    other = env.frappe.new_doc(DISB)
    other.update({k: getattr(batch, k) for k in ("naming_series", "company", "posting_date", "payment_type",
                                                 "transaction_to_pay_against", "mode_of_payment", "paid_from",
                                                 "paid_from_account_currency", "paid_to_account_currency",
                                                 "source_exchange_rate", "target_exchange_rate")})
    other.update({"party_type": None, "paid_to": None})
    other.append("references", {"reference_doctype": SHEET, "reference_name": "MBP-1", "reference_row": reference_row,
                                "partyb": phone, "allocated_amount": amount, "currency": "KES", "exchange_rate": 1})
    return other


def cancel_all(env, batch, request):
    """Cancel All as it was before the guard: the batch and its request cancelled, the status kept."""
    db = env.frappe.db
    db.table(REQ)[request]["docstatus"] = 2
    db.table(DISB)[batch.name]["docstatus"] = 2


def request_doc(env, name, **kw):
    """The B2C Disbursement Request controller on a stored request (frappe's Document is a stub here)."""
    module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request")
    values = dict(env.frappe.db.table(REQ)[name], **kw)
    env.frappe.db.table(REQ)[name].update(kw)
    doc = object.__new__(module.B2CDisbursementRequest)
    doc.__dict__.update(values, doctype=REQ, flags=_dict())
    doc.meta = types.SimpleNamespace(has_field=lambda f: True)
    doc.get = lambda key, default=None: doc.__dict__.get(key, default)
    doc.update = doc.__dict__.update
    doc.reload = lambda: None
    doc.sets = []
    doc.db_set = lambda values, *a, **k: doc.sets.append(values) or doc.__dict__.update(values)
    return module, doc


def logged_writes(env, monkeypatch):
    """frappe.db.set_value logged in env.log as ("set", doctype, name)."""
    plain = env.frappe.db.set_value

    def set_value(doctype, name, field, value=None, **kw):
        env.log.append(("set", doctype, name if isinstance(name, str) else None))
        return plain(doctype, name, field, value, **kw)

    monkeypatch.setattr(env.frappe.db, "set_value", set_value)


# ---------------------------------------------------------------- 1. a payout whose fate is unknown
@pytest.mark.parametrize("state", [{"status": "Status Unknown"}, {"status": "Initiated"},
                                   {"status": "Not Initiated", "send_started_on": NOW}])
def test_a_payout_in_doubt_holds_its_row_after_its_batch_and_request_are_cancelled(env, state):
    sheet(env)
    pay(env)
    batch = send(env)
    requests = answer(env, batch, {"254722000111": ("Unknown",)})
    request = requests["254722000111"]
    env.frappe.db.table(REQ)[request].update(state)
    cancel_all(env, batch, request)
    assert env.bulk.sync_rows("MBP-1")[1]["status"] == "In Flight"  # the sheet says its fate is unknown
    with raises(f"which is being paid by {batch.name} row #2 \\({state['status']}\\): a row is never paid twice"):
        hand_made(env, batch).insert()
    # the sheet's own check reads the row's status too (its other payouts, by b2c_state)
    with raises(f"Row #1: row #2 of MBP-1 \\(OTIENO OUMA\\) is being paid by {batch.name}, {request} "
                f"\\({state['status']}\\). A row is never paid twice"):
        env.bulk.HANDLER.check_row(_dict(idx=1, reference_name="MBP-1", reference_row="MBP-1-R2",
                                         partyb="254722000111", allocated_amount=2000),
                                   _dict(name="MPESA-B2C-NEW", company="Astral", flags=_dict()))
    with raises("No row has failed"):
        pay(env, "retry_failed")
    # settled by M-Pesa (a status query: Failed): the row may be paid again
    env.frappe.db.table(REQ)[request].update(status="Failed", result_code="TS:Failed")
    other = hand_made(env, batch)
    other.insert()
    assert other.name


def test_a_never_sent_cancelled_payout_holds_nothing(env):
    sheet(env)
    pay(env)
    batch = send(env)
    requests = answer(env, batch, {"254722000111": ("Unknown",)})
    request = requests["254722000111"]
    env.frappe.db.table(REQ)[request].update(status="Not Initiated", send_started_on=None)
    cancel_all(env, batch, request)
    assert env.bulk.sync_rows("MBP-1")[1]["status"] == "Draft"
    hand_made(env, batch).insert()  # it never left: the row may be paid


def test_a_hand_made_kes_10_test_never_pays_a_row_paid_or_on_its_way(env):
    sheet(env)
    pay(env)
    batch = send(env)
    answer(env, batch, paid_names())  # row 1 paid, row 4 failed
    with raises(f"Row #1 pays M-Pesa Bulk Payout MBP-1 row MBP-1-R1/test, which is paid by {batch.name} row #1 "
                "\\(paying row MBP-1-R1\\) \\(Paid\\)"):
        hand_made(env, batch, "MBP-1-R1/test", "254711000331", 10).insert()
    with raises("Row #1: row #1 of MBP-1 \\(Jane Wanjiku\\) is paid by"):
        env.bulk.HANDLER.check_row(_dict(idx=1, reference_name="MBP-1", reference_row="MBP-1-R1/test",
                                         partyb="254711000331", allocated_amount=10),
                                   _dict(name="MPESA-B2C-NEW", company="Astral", flags=_dict()))
    # the main payout is not held by its own paid test: it pays the rest
    assert env.bulk.HANDLER.related_rows("MBP-1-R1") == [] and env.bulk.HANDLER.related_rows("MBP-1-R1/test") == [
        "MBP-1-R1"]


def test_a_second_test_of_a_tested_row_is_refused(env):
    sheet(env, ROWS[:1], verify_new_numbers=1)
    pay(env)
    batch = send(env)
    answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU")})  # its test paid, Verified
    assert stored_rows(env)[0]["verification"] == "Verified"
    with raises("which is paid by"):
        hand_made(env, batch, "MBP-1-R1/test", "254711000331", 10).insert()
    with raises("is paid by .* \\(its KES 10 test\\)"):
        env.bulk.HANDLER.check_row(_dict(idx=1, reference_name="MBP-1", reference_row="MBP-1-R1/test",
                                         partyb="254711000331", allocated_amount=10),
                                   _dict(name="MPESA-B2C-NEW", company="Astral", flags=_dict()))


def test_the_row_links_are_no_copy_so_a_duplicate_names_no_paying_row(env):
    data = json.loads((APP / "mpsa_suite" / "doctype" / "b2c_payment_disbursement_reference"
                       / "b2c_payment_disbursement_reference.json").read_text(encoding="utf-8"))
    fields = {f["fieldname"]: f for f in data["fields"]}
    for name in ("reference_row", "claim_payee", "b2c_disbursement_request", "mpesa_schedule_line", "payee_account",
                 "payee_account_doctype"):
        assert fields[name].get("no_copy") == 1, name
    assert data["modified"] > "2026-11-13 12:00:00"  # newer than the site's: the migrate syncs it
    # a Duplicate (no paying row) is refused for a sheet's row
    sheet(env)
    pay(env)
    batch = env.batches[-1]
    with raises("it must name the document and its row it pays"):
        hand_made(env, batch, reference_row=None).insert()


@pytest.mark.parametrize("status, send_started, refused", [
    ("Initiated", None, "M-Pesa may have paid it \\(Initiated"),
    ("Status Unknown", None, "M-Pesa may have paid it \\(Status Unknown"),
    ("Not Initiated", NOW, "M-Pesa may have paid it \\(Not Initiated"),
    ("Paid", None, "it is the record of that payment and cannot be cancelled"),
    ("Failed", None, None),
    ("Not Initiated", None, None),
])
def test_a_partyless_request_that_may_have_paid_is_never_cancelled(env, status, send_started, refused):
    sheet(env)
    pay(env)
    requests = answer(env, send(env), {"254722000111": ("Unknown",)}, record=False)
    _module, doc = request_doc(env, requests["254722000111"], status=status, send_started_on=send_started)
    doc.status = "Failed"  # the form's copy: the database's status decides
    with as_user(env, APPROVER, "M-Pesa Manager"):
        if refused:
            with raises(refused):
                doc.before_cancel()
        else:
            doc.before_cancel()
    assert doc.status == status


@pytest.mark.parametrize("status, refused", [("Status Unknown", "It is not deleted until M-Pesa says"),
                                             ("Paid", "cannot be deleted"), ("Failed", None)])
def test_a_partyless_request_that_may_have_paid_is_never_deleted(env, status, refused):
    sheet(env)
    pay(env)
    requests = answer(env, send(env), {"254722000111": ("Unknown",)}, record=False)
    _module, doc = request_doc(env, requests["254722000111"], status=status)
    if refused:
        with raises(refused):
            doc.on_trash()
    else:
        doc.on_trash()


def test_a_request_of_a_party_is_cancelled_as_before(env):
    sheet(env)
    pay(env)
    requests = answer(env, send(env), {"254722000111": ("Unknown",)}, record=False)
    _module, doc = request_doc(env, requests["254722000111"], status="Initiated", reference_doctype="Expense Claim",
                               b2c_payment_disbursement_reference=None)
    doc.before_cancel()  # no schedule line, no party-less row: nothing refuses it here


# ---------------------------------------------------------------- 2. the journal after cancel and amend
def _amend(env, journal):
    """ERPNext's Cancel and Amend of the sheet's journal: the original cancelled, its copy submitted."""
    db = env.frappe.db
    db.table(JE)[journal]["docstatus"] = 2
    amended = f"{journal}-1"
    db.table(JE)[amended] = dict(db.table(JE)[journal], name=amended, docstatus=1, amended_from=journal)
    for line in [r for r in db.table(JEA).values() if r["parent"] == journal]:
        db.table(JEA)[f"{amended}-{line['idx']}"] = dict(line, name=f"{amended}-{line['idx']}", parent=amended)
    return amended


def _debits(env, journal):
    return sum(r["debit_in_account_currency"] for r in env.frappe.db.table(JEA).values() if r["parent"] == journal)


def test_a_journal_cancelled_and_amended_still_books_its_payouts(env):
    sheet(env)
    pay(env)
    requests = answer(env, send(env), paid_names())
    (first,) = journals(env)
    amended = _amend(env, first)
    assert env.bulk.live_journal(first) == (amended, 1) and env.bulk.live_journal(amended) == (amended, 1)
    db = env.frappe.db
    ref = _dict(db.table(ROW)[db.table(REQ)[requests["254711000331"]]["b2c_payment_disbursement_reference"]])
    assert env.bulk.HANDLER.booked(ref) == 1
    request = _dict(db.table(REQ)[requests["254711000331"]], reference_row="MBP-1-R1")
    assert env.bulk.HANDLER.statement_entry(request) == (JE, amended, 1500)
    assert env.bulk.book_sheet("MBP-1") is None  # nothing to book again
    # the failed row retried and paid later: its own small journal, the amended one's 4,300 never again
    pay(env, "retry_failed")
    answer(env, send(env), {"254711000334": ("Paid", "JOHN KAMAU")})
    late = [n for n in journals(env) if n not in (first, amended)]
    assert len(late) == 1 and _debits(env, late[0]) == 1200
    with as_user(env, APPROVER, "Accounts Manager"):
        state = env.bulk.sheet_state("MBP-1")
    assert sorted(j["name"] for j in state["journals"]) == sorted([amended, late[0]])
    assert state["unbooked"]["rows"] == []


def test_a_draft_amended_copy_books_nothing_twice_either(env):
    sheet(env)
    pay(env)
    answer(env, send(env), paid_names())
    (first,) = journals(env)
    amended = _amend(env, first)
    env.frappe.db.table(JE)[amended]["docstatus"] = 0
    assert env.bulk.live_journal(first) == (amended, 0)
    assert env.bulk.book_sheet("MBP-1") is None


def test_a_journal_cancelled_with_no_amend_is_booked_again(env):
    sheet(env)
    pay(env)
    answer(env, send(env), paid_names())
    (first,) = journals(env)
    env.frappe.db.table(JE)[first]["docstatus"] = 2
    assert env.bulk.live_journal(first) == (None, None)
    again = env.bulk.book_sheet("MBP-1")
    assert again and again != first and _debits(env, again) == 4300


def test_a_payout_a_voucher_carrying_its_receipt_books_is_never_swept_into_the_journal(env):
    sheet(env, ROWS[:2])
    pay(env)
    batch = send(env)
    first = answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU"), "254722000111": ("Initiated",)})
    assert journals(env) == {}  # row 2 holds the journal
    db = env.frappe.db
    receipt = db.table(REQ)[first["254711000331"]]["transaction_id"]
    # Bank Reconciliation's Create Voucher on its statement line: a Journal Entry with the receipt as Cheque No
    db.table(JE)["ACC-JV-V1"] = {"name": "ACC-JV-V1", "docstatus": 1, "cheque_no": receipt,
                                 "voucher_type": "Bank Entry", "posting_date": NOW.date(), "creation": NOW}
    answer(env, batch, {"254722000111": ("Paid", "OTIENO OUMA")})
    ((journal, (_je, lines)),) = journals(env).items()
    assert sum(r["debit_in_account_currency"] for r in lines) == 2000  # row 1 is booked by the voucher
    ref = _dict(db.table(ROW)[db.table(REQ)[first["254711000331"]]["b2c_payment_disbursement_reference"]])
    assert env.bulk.HANDLER.booked(ref) == 1
    env.postprocess.retry_unbooked_payouts()
    assert db.table(REQ)[first["254711000331"]]["accounting_booked"] == 1  # booked, by the voucher
    assert env.bulk.book_sheet("MBP-1") is None and len(journals(env)) == 1
    # a Payment Entry carrying it counts the same
    assert env.bulk.receipt_entries([receipt.lower()]) == {receipt: 1}
    db.table("Payment Entry")["PE-1"] = {"name": "PE-1", "docstatus": 0, "reference_no": "RPE1"}
    assert env.bulk.receipt_entries(["RPE1", "RNONE"]) == {"RPE1": 0}


# ---------------------------------------------------------------- 3. retries and Mark as Not Paid
def _marked(env):
    """Row 2's payout got no answer, a status query answered nothing, and a manager marked it as not paid."""
    sheet(env)
    pay(env)
    batch = send(env)
    requests = answer(env, batch, {"254722000111": ("Unknown",)})
    request = requests["254722000111"]
    env.frappe.db.table(REQ)[request]["originator_conversation_id"] = "OCID-FIRST"
    env.frappe.db.table(STATUS_QUERY)["MBSQ-1"] = {"name": "MBSQ-1", "b2c_request": request,
                                                   "status": "Result Received", "outcome": "Unresolved",
                                                   "creation": NOW}
    with as_user(env, APPROVER, "M-Pesa Manager"):
        env.bulk.mark_not_paid("MBP-1", "MBP-1-R2", "the statement of 18 Oct shows no such payment")
    return batch, request


def test_a_marked_payout_is_retried_only_by_the_sheets_retry_failed(env, monkeypatch):
    batch, request = _marked(env)
    module, doc = request_doc(env, request)
    sent = []
    monkeypatch.setattr(module, "make_b2c_payment_request", lambda name: sent.append(name))
    with as_user(env, PAYER, "M-Pesa Payer"):
        with raises(f"{request} pays a row of M-Pesa Bulk Payout MBP-1: retry it with Retry Failed on MBP-1, which "
                    "makes a new payout \\(a new B2C Disbursement Request with its own OriginatorConversationID\\)"):
            doc.retry_failed_payment()
    assert sent == [] and doc.sets == []
    stored = env.frappe.db.table(REQ)[request]
    assert (stored["status"], stored["originator_conversation_id"]) == ("Failed", "OCID-FIRST")  # its first attempt
    # the batch's Retry Failed Transactions says so too, before it retries anything
    for ref in batch.references:
        ref.payment_status = env.frappe.db.table(ROW)[ref.name]["payment_status"]
        ref.b2c_disbursement_request = env.frappe.db.table(ROW)[ref.name]["b2c_disbursement_request"]
    with as_user(env, PAYER, "M-Pesa Payer"), raises("retry it with Retry Failed on MBP-1"):
        batch.retry_failed_payments()
    assert env.partyless.retry_problem(_dict(reference_doctype="Expense Claim")) is None  # any other row: as before
    # the sheet's Retry Failed: a new batch, a new request; the marked one keeps its IDs
    made = pay(env, "retry_failed")
    assert [r.reference_row for r in env.batches[-1].references] == ["MBP-1-R2"] and made.rows == 1
    again = answer(env, send(env), {"254722000111": ("Paid", "OTIENO OUMA")})
    assert again["254722000111"] != request
    assert env.frappe.db.table(REQ)[request]["originator_conversation_id"] == "OCID-FIRST"


def test_mark_as_not_paid_refuses_the_person_it_pays(env):
    sheet(env)
    pay(env)
    requests = answer(env, send(env), {"254722000111": ("Unknown",)})
    db = env.frappe.db
    boss = "boss@example.com"
    db.table("User")[boss] = {"name": boss, "full_name": "Boss Person", "mobile_no": "0722000111", "phone": None,
                              "enabled": 1, "user_type": "System User"}
    db.table(STATUS_QUERY)["MBSQ-1"] = {"name": "MBSQ-1", "b2c_request": requests["254722000111"],
                                        "status": "Result Received", "creation": NOW}
    with as_user(env, boss, "M-Pesa Manager"), raises("You may not settle a payment to yourself: Row #2 pays your "
                                                      "phone", env.frappe.PermissionError):
        env.bulk.mark_not_paid("MBP-1", "MBP-1-R2", "statement shows nothing")
    assert db.table(REQ)[requests["254722000111"]]["status"] == "Status Unknown"


def test_mark_as_not_paid_refuses_an_acknowledged_payout(env):
    sheet(env)
    pay(env)
    requests = answer(env, send(env), {"254722000111": ("Initiated",)})
    env.frappe.db.table(STATUS_QUERY)["MBSQ-1"] = {"name": "MBSQ-1", "b2c_request": requests["254722000111"],
                                                   "status": "Result Received", "creation": NOW}
    with as_user(env, APPROVER, "M-Pesa Manager"), raises("M-Pesa acknowledged .* \\(Initiated\\): it has the payout"):
        env.bulk.mark_not_paid("MBP-1", "MBP-1-R2", "statement shows nothing")


@pytest.mark.parametrize("query, refused", [
    (None, True),
    ({"status": "Sent"}, True),  # asked, not answered yet
    ({"status": "Queued"}, True),
    ({"status": "Result Received", "outcome": "Paid"}, True),
    ({"status": "Result Received", "outcome": "Unresolved"}, False),  # e.g. "not found": settles nothing
    ({"status": "Result Received"}, False),
])
def test_mark_as_not_paid_needs_a_status_query_m_pesa_answered(env, query, refused):
    sheet(env)
    pay(env)
    requests = answer(env, send(env), {"254722000111": ("Unknown",)})
    request = requests["254722000111"]
    if query:
        env.frappe.db.table(STATUS_QUERY)["MBSQ-1"] = dict({"name": "MBSQ-1", "b2c_request": request,
                                                            "creation": NOW}, **query)
    with as_user(env, APPROVER, "M-Pesa Manager"):
        if refused:
            with raises(f"M-Pesa has not answered a Transaction Status query about {request} yet"):
                env.bulk.mark_not_paid("MBP-1", "MBP-1-R2", "statement shows nothing")
            assert env.frappe.db.table(REQ)[request]["status"] == "Status Unknown"
        else:
            assert env.bulk.mark_not_paid("MBP-1", "MBP-1-R2", "statement shows nothing")["status"] == "Failed"


# ---------------------------------------------------------------- 4. the lock order
def test_a_partyless_result_takes_the_sheets_lock_before_it_writes_its_row(env, monkeypatch):
    sheet(env)
    pay(env)
    batch = send(env)
    requests = answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU")}, record=False)
    logged_writes(env, monkeypatch)
    env.locks.clear()
    env.log.clear()
    env.response.update_b2c_reference_status(requests["254711000331"], enqueue_next=False)
    ref = env.frappe.db.table(REQ)[requests["254711000331"]]["b2c_payment_disbursement_reference"]
    order = [e for e in env.log if e[0] in ("lock", "set", "commit")]
    assert order.index(("lock", LOCK)) < order.index(("set", ROW, ref)) < order.index(("commit",))
    assert env.frappe.db.table(ROW)[ref]["payment_status"] == "Paid"
    # any other row: no document lock (partyless.result_lock is a no-op)
    with env.partyless.result_lock("NO-SUCH-ROW"):
        pass
    with env.partyless.result_lock(None):
        pass


def test_a_result_whose_sheet_stays_locked_writes_nothing_and_the_sweep_runs_it_again(env):
    sheet(env)
    pay(env)
    batch = send(env)
    requests = answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU")}, record=False)
    ref = env.frappe.db.table(REQ)[requests["254711000331"]]["b2c_payment_disbursement_reference"]
    env.busy_keys.add(LOCK)
    with raises("Another payment for M-Pesa Bulk Payout MBP-1 is being sent or booked"):
        env.response.update_b2c_reference_status(requests["254711000331"], enqueue_next=True)
    assert env.frappe.db.table(ROW)[ref]["payment_status"] != "Paid"  # left for the 15-minute sweep
    env.busy_keys.clear()
    env.response.update_b2c_reference_status(requests["254711000331"], enqueue_next=True)
    assert env.frappe.db.table(ROW)[ref]["payment_status"] == "Paid"


def test_book_waiting_and_the_unbooked_sweep_take_the_lock_before_any_row(env, monkeypatch):
    sheet(env)
    pay(env)
    batch = send(env)
    requests = answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU")})
    answer(env, batch, {p: ("Failed", "1", "insufficient") for p in ("254722000111", "254711000333",
                                                                      "254711000334")}, record=False)
    logged_writes(env, monkeypatch)
    env.locks.clear()
    env.log.clear()
    env.bulk.book_waiting("MBP-1")
    first = next(i for i, e in enumerate(env.log) if e[0] in ("lock", "set") or (e[0] == "sql" and e[2]))
    assert env.log[first] == ("lock", LOCK)
    assert len(journals(env)) == 1
    # the sweep commits its stamp before the job, so it holds no row lock when it waits for the sheet's
    env.frappe.db.table(REQ)[requests["254711000331"]]["accounting_booked"] = 0
    env.locks.clear()
    env.log.clear()
    env.postprocess.retry_unbooked_payouts()
    stamp = env.log.index(("set", REQ, requests["254711000331"]))
    assert env.log[stamp + 1] == ("commit",) and env.log.index(("lock", LOCK)) > stamp + 1


# ---------------------------------------------------------------- 5. the KES 10 test and the Minimum B2C Payout
@pytest.fixture
def minimum(env, monkeypatch):
    svc = importlib.import_module("mpsa_suite.services.b2c_request_service")
    monkeypatch.setattr(svc, "min_payout_amount", lambda: 50.0)
    return svc


def test_the_test_pays_the_minimum_payout_and_only_when_the_rest_can_be_paid(env, minimum):
    rows = [*ROWS, dict(payee_name="Small Pay", phone="0711000338", amount=80)]
    sheet(env, rows, verify_new_numbers=1)
    made = pay(env)
    assert [(r.reference_row, r.allocated_amount) for r in env.batches[-1].references] == [
        ("MBP-1-R1/test", 50), ("MBP-1-R2", 2000), ("MBP-1-R3/test", 50), ("MBP-1-R4/test", 50), ("MBP-1-R5", 80)]
    assert made.tests == 3 and any("3 new numbers get KES 50 first" in c[2] for c in env.frappe.comments)
    batch = send(env)  # the send's checks pass: every payout is at least the minimum
    answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU")})
    row = stored_rows(env)[0]
    assert (row["verification"], row["paid_amount"]) == ("Verified", 50)
    assert row["progress"].startswith("KES 50 test paid under the name given")
    pay(env)
    assert [(r.reference_row, r.allocated_amount) for r in env.batches[-1].references] == [("MBP-1-R1", 1450)]
    assert env.bulk.trial_amount() == 50
    assert env.bulk.HANDLER.describe(_dict(reference_name="MBP-1", reference_row="MBP-1-R3/test",
                                           allocated_amount=50))["document"].endswith("(KES 50 test of a new number)")


def test_the_save_refuses_a_row_below_the_minimum_or_above_the_shortcodes_limit(env, minimum):
    with raises("Row #1: M-Pesa B2C pays at least KES 50.00 \\(Minimum B2C Payout on M-Pesa Suite Settings\\); the "
                "amount is 40.00"):
        save(env, build(env, [dict(ROWS[0], amount=40)]))
    env.frappe.db.table("Mpesa Settings")[SETTINGS]["transaction_limit"] = 1000
    with raises(f"Row #1: Amount is above this shortcode's transaction limit of 1,000.00 \\(Mpesa Settings "
                f"{SETTINGS}\\)"):
        save(env, build(env, [ROWS[0]]))
    save(env, build(env, [dict(ROWS[0], amount=1000)]))
    # no shortcode on the sheet: the company's only one is what Pay by M-Pesa would pick
    with raises("transaction limit of 1,000.00"):
        save(env, build(env, [ROWS[0]], mpesa_settings=None))


# ---------------------------------------------------------------- 6. accounting dimensions
@pytest.fixture
def department(env, monkeypatch):
    dims = sys.modules[env.bulk.DIMENSIONS_MODULE]
    checks = [_dict(fieldname="department", label="Department", company="Astral", disabled=0, mandatory_for_pl=1,
                    mandatory_for_bs=0, default_dimension=None),
              _dict(fieldname="department", label="Department", company="Other", disabled=0, mandatory_for_pl=1,
                    mandatory_for_bs=1, default_dimension=None)]
    monkeypatch.setattr(dims, "get_accounting_dimensions", lambda as_list=True: ["project", "department"])
    monkeypatch.setattr(dims, "get_checks_for_pl_and_bs_accounts", lambda: checks, raising=False)
    db = env.frappe.db
    db.table("Accounting Dimension")["Department"] = {"name": "Department", "fieldname": "department",
                                                     "label": "Department", "document_type": "Department"}
    for name, company in (("Ops - AIL", "Astral"), ("Admin - AIL", "Astral"), ("Ops - XYZ", "Other")):
        db.table("Department")[name] = {"name": name, "company": company}
    db.table("Account")[WAGES]["report_type"] = "Profit and Loss"
    db.table("Account")["Overtime - AIL"]["report_type"] = "Profit and Loss"
    return checks


def test_a_dimension_mandatory_for_profit_and_loss_is_required_or_takes_the_companys_default(env, department):
    with raises("Row #1: Accounting Dimension Department is required for 'Profit and Loss' account Casual Wages - AIL: "
                "pick its Department, or set a default Department for Astral on the Accounting Dimension"):
        save(env, build(env, [ROWS[0]]))
    doc = save(env, build(env, [dict(ROWS[0], department="Ops - AIL")]))
    assert doc.rows[0].department == "Ops - AIL"
    department[0]["default_dimension"] = "Admin - AIL"
    doc = save(env, build(env, [ROWS[0], dict(ROWS[2], department="Ops - AIL")]))
    assert [r.department for r in doc.rows] == ["Admin - AIL", "Ops - AIL"]  # the default only where none is given


def test_a_dimension_value_belongs_to_the_company(env, department):
    with raises("Row #1: Department: Ops - XYZ does not belong to the Company: Astral"):
        save(env, build(env, [dict(ROWS[0], department="Ops - XYZ")]))
    with raises("Row #1: Department Nowhere not found"):
        save(env, build(env, [dict(ROWS[0], department="Nowhere")]))


def test_the_accounting_dimension_filters_of_the_expense_account_are_kept(env, department, monkeypatch):
    module = types.ModuleType(env.bulk.DIMENSION_FILTERS_MODULE)
    rules = {("department", WAGES): {"allowed_dimensions": ["Ops - AIL"], "is_mandatory": 0,
                                     "allow_or_restrict": "Allow"},
             ("project", WAGES): {"allowed_dimensions": ["PROJ-B"], "is_mandatory": 1, "allow_or_restrict": "Restrict"}}
    module.get_dimension_filter_map = lambda: rules
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with raises("Row #1: Admin - AIL is not allowed for Department against account Casual Wages - AIL"):
        save(env, build(env, [dict(ROWS[0], department="Admin - AIL")]))
    with raises("Row #1: PROJ-B is not allowed for Project against account Casual Wages - AIL"):
        save(env, build(env, [dict(ROWS[0], department="Ops - AIL", project="PROJ-B")]))
    with raises("Row #1: Project is mandatory for account Casual Wages - AIL"):
        save(env, build(env, [dict(ROWS[0], department="Ops - AIL")], project=None))
    save(env, build(env, [dict(ROWS[0], department="Ops - AIL")]))


def test_a_dimension_mandatory_for_the_balance_sheet_needs_a_default_the_credit_line_takes(env, department):
    department[0].update(mandatory_for_bs=1, default_dimension=None)
    with raises("Accounting Dimension Department is required for 'Balance Sheet' accounts at Astral"):
        save(env, build(env, [dict(ROWS[0], department="Ops - AIL")]))
    department[0]["default_dimension"] = "Admin - AIL"
    sheet(env, [dict(ROWS[0], department="Ops - AIL")])
    pay(env)
    answer(env, send(env), {"254711000331": ("Paid", "JANE WANJIKU")})
    ((_name, (_je, lines)),) = journals(env).items()
    assert [(r.get("department"), bool(r.get("debit_in_account_currency"))) for r in lines] == [
        ("Ops - AIL", True), ("Admin - AIL", False)]


# ---------------------------------------------------------------- 7. the retry's self-pay check and M-Pesa's name
def test_the_retrys_self_pay_check_knows_the_name_m_pesa_paid_the_phone_under(env):
    rows = [dict(ROWS[1], payee_name="Peter Otieno")]  # 0722000111, known to M-Pesa as OTIENO OUMA
    sheet(env, rows)
    pay(env)
    db = env.frappe.db
    otieno = "otieno.user@example.com"
    db.table("User")[otieno] = {"name": otieno, "full_name": "Otieno Ouma", "mobile_no": None, "phone": None,
                                "enabled": 1, "user_type": "System User"}
    requests = answer(env, send(env), {"254722000111": ("Failed", "1", "insufficient")})
    request = env.frappe.get_doc(REQ, requests["254722000111"])
    ref = _dict(db.table(ROW)[db.table(REQ)[requests["254722000111"]]["b2c_payment_disbursement_reference"]])
    found = env.partyless.describe(ref)
    assert (found["receiver"], found["mpesa_name"]) == ("Peter Otieno", "OTIENO OUMA")
    with as_user(env, otieno, "M-Pesa Payer"), raises("You may not resend a payment to yourself: .* pays 'OTIENO "
                                                      "OUMA', your name", env.frappe.PermissionError):
        env.partyless.check_retry(request)
    # the default for a handler with no self_pay_rows of its own reads it too
    batch = env.batches[-1]
    plain = env.partyless.self_pay_rows

    class Plain(env.partyless.Handler):
        def describe(self, row):
            return env.bulk.HANDLER.describe(row)

        def check_row(self, row, parent):
            return None

    env.partyless.register(SHEET, Plain())
    try:
        assert [r["mpesa_name"] for r in plain(batch)] == ["OTIENO OUMA"]
    finally:
        env.partyless.register(SHEET, env.bulk.HANDLER)
