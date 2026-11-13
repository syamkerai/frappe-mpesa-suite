"""Round 4.5: party-less B2C payouts (money_out.partyless), on the in-memory site of test_payroll_schedule
(SQL on SQLite) with the real B2C Payment Disbursement, row and request controllers.

A fake handler registers a "Test Sheet" whose rows pay people no party names. Covers: the registry
(in-process and from hooks.py, the last path winning, a class made, a path that cannot load left out);
a party-less disbursement saved and sent (no Party Type or Account Paid To, the row's own checks, the
handler's check, no party, a paying row named once and never paid by two payouts, the send under the
document's lock, self-pay refused); the Send via M-Pesa summary; the result copied to the paying row
before booking; booking direct and deferred (the waiting note, the sweep booking it later, a later
error still logged and alerted); booked_docstatus and the closed-period hint; the statement line's
part of an entry that books several payouts (what is left of it, cleared or draft entries); the retry
(the row checked, not the document, and self-pay); a request made by hand refused; maker-checker
watching the paying row; prints and proofs by phone; the name lookups reading such payouts; and every
unregistered DocType exactly as before. The settings this round adds and their child DocType are
checked at the end.
"""

import ast
import importlib
import json
import sys
import types
from pathlib import Path

import b2b_fixture
import fake_frappe
import pytest
from test_payroll_schedule import DISB, PAYER, REQ, ROW, SETTINGS
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the fixture this one extends)

APP = Path(__file__).resolve().parents[2] / "mpsa_suite"
SHEET, SHEET_ROW = "Test Sheet", "Test Sheet Row"
GL, NOW = b2b_fixture.GL, b2b_fixture.NOW
JE, JEA = b2b_fixture.JE, b2b_fixture.JEA
BT, BTP = "Bank Transaction", "Bank Transaction Payments"
EC = "Expense Claim"
_dict = fake_frappe._dict


class SheetHandler:
    """A party-less DocType's handler as a feature writes one (partyless.Handler's defaults)."""

    def __init__(self, env, base):
        self.env, self.calls, self.mode, self.refuse = env, [], "direct", None
        self.base = base

    def _row(self, name):
        return self.env.frappe.db.table(SHEET_ROW)[name]

    def check_row(self, row, parent):
        self.calls.append(("check_row", row.get("reference_row"), parent.get("name") if parent else None))
        if self.refuse:
            self.env.frappe.throw(self.refuse)

    def on_result(self, row, request):
        self.calls.append(("on_result", row.reference_row, row.payment_status, row.reference_no))
        if self.mode == "result fails":
            raise ValueError("the sheet is locked")
        self._row(row.reference_row).update(status=row.payment_status, receipt=row.reference_no,
                                            receiver_name=request.receiver_public_name)

    def book(self, row):
        self.calls.append(("book", row.reference_row))
        if self.mode == "deferred":
            return self.env.partyless.deferred("the sheet's journal books it once no row waits")
        if self.mode == "error":
            raise self.env.frappe.ValidationError("Cost Center Site A - AIL is disabled")
        name = f"ACC-JV-{row.reference_row}"
        self.env.je(name, row.reference_no, [{"account": GL, "credit_in_account_currency": row.allocated_amount},
                                             {"account": "Casual Wages - AIL",
                                              "debit_in_account_currency": row.allocated_amount}],
                    clearance_date=None)
        self._row(row.reference_row)["journal_entry"] = name
        return None

    def booked(self, row):
        journal = self._row(row.reference_row).get("journal_entry")
        return self.env.frappe.db.get_value(JE, journal, "docstatus") if journal else None

    def statement_entry(self, request):
        journal = self._row(request.reference_row).get("journal_entry")
        return (JE, journal, request.amount) if journal else None

    def describe(self, row):
        sheet_row = self._row(row.get("reference_row"))
        return {"receiver": sheet_row["payee_name"], "party": sheet_row.get("project"),
                "document": f"{SHEET} {row.get('reference_name')}, row #{sheet_row['idx']}"}

    def check_retry(self, row, request):
        self.calls.append(("check_retry", row.reference_row, request.name))
        self.base.check_retry(self, row, request)


@pytest.fixture
def env(payroll_env, monkeypatch):  # noqa: F811
    env = payroll_env
    frappe = env.frappe
    columns = b2b_fixture.COLUMNS
    monkeypatch.setitem(columns, ROW, columns[ROW] + ("reference_row", "payee_account_doctype", "payee_account",
                                                      "claim_payee"))
    monkeypatch.setitem(columns, DISB, columns[DISB] + ("party_type", "transaction_to_pay_against", "owner"))
    monkeypatch.setitem(columns, REQ, columns[REQ] + ("phone_number", "receiver_public_name", "accounting_booked",
                                                      "accounting_error", "party_type", "party"))
    monkeypatch.setitem(columns, JE, columns[JE] + ("clearance_date",))
    monkeypatch.setitem(columns, BTP, ("name", "parent", "payment_document", "payment_entry", "allocated_amount"))
    monkeypatch.setitem(columns, BT, ("name", "docstatus"))
    env.partyless = importlib.import_module("mpsa_suite.money_out.partyless")
    env.handler = SheetHandler(env, env.partyless.Handler)
    env.partyless.register(SHEET, env.handler)
    maker_checker = importlib.import_module("mpsa_suite.money_out.maker_checker")
    monkeypatch.setattr(maker_checker, "check", lambda doc: None)  # tested on its own below
    env.send_confirm = importlib.import_module("mpsa_suite.money_out.send_confirm")
    # the Payment and Journal Entries of party payouts (ERPNext's own modules): never reached for these rows
    for name in ("mpsa_suite.accounting.payment_entry_service", "mpsa_suite.accounting.journal_service"):
        stub = types.ModuleType(name)
        stub.create_payment_entry_for_doc = stub.create_journal_entry = lambda *a, **kw: None
        monkeypatch.setitem(sys.modules, name, stub)
    env.postprocess = importlib.import_module("mpsa_suite.services.b2c_postprocess_service")
    env.response = importlib.import_module("mpsa_suite.services.b2c_response_service")
    env.ed = importlib.import_module("mpsa_suite.money_out.employee_documents")
    db = frappe.db
    db.table(SHEET)["SHEET-1"] = {"name": "SHEET-1", "docstatus": 1, "company": "Astral"}
    for i, (payee, phone) in enumerate((("Jane Wanjiku", "254711000333"), ("Peter Otieno", "254711000444"),
                                        ("Ann Njeri", "254711000555")), 1):
        db.table(SHEET_ROW)[f"SR-{i}"] = {"name": f"SR-{i}", "idx": i, "parent": "SHEET-1", "payee_name": payee,
                                          "phone": phone, "amount": 1500, "project": "Site A", "status": "Draft"}
    db.table("User")[PAYER] = {"name": PAYER, "full_name": "Payer Person", "mobile_no": None, "phone": None}
    yield env


def new_disbursement(env, rows=("SR-1", "SR-2"), **kw):
    """A party-less B2C Payment Disbursement of the sheet's rows, as the feature makes it."""
    doc = env.frappe.new_doc(DISB)
    doc.update({"naming_series": "MPESA-B2C-.YYYY.-", "company": "Astral", "posting_date": "2026-10-18",
                "payment_type": "Mpesa Disbursement", "party_type": None, "transaction_to_pay_against": SHEET,
                "mode_of_payment": f"Mpesa-{SETTINGS}", "paid_from": GL, "paid_from_account_currency": "KES",
                "paid_to": None, "paid_to_account_currency": "KES", "source_exchange_rate": 1,
                "target_exchange_rate": 1, **kw})
    doc.meta = types.SimpleNamespace(get_label=lambda field: field.replace("_", " ").title())
    sheet = env.frappe.db.table(SHEET_ROW)
    for name in rows:
        row = doc.append("references", {"reference_doctype": SHEET, "reference_name": "SHEET-1", "reference_row": name,
                                        "partyb": sheet[name]["phone"], "allocated_amount": sheet[name]["amount"],
                                        "currency": "KES", "exchange_rate": 1, "party_type": None, "party": None})
        row.parent_doc = doc
    return doc


def submit(env, doc):
    with env.send_confirm.deliberate("test", doc):
        doc.submit()


def raises(env, match, exc=None):
    return pytest.raises(exc or env.frappe.ValidationError, match=match)


def paid_payout(env, n=1, status="Paid", disbursement="MPESA-B2C-7", **kw):
    """A sent row of the sheet (SR-<n>) and its B2C Disbursement Request."""
    db = env.frappe.db
    db.table(DISB).setdefault(disbursement, {"name": disbursement, "docstatus": 1, "company": "Astral",
                                             "transaction_to_pay_against": SHEET, "party_type": None})
    row = f"{disbursement}-R{n}"
    request = f"B2C-{disbursement[-1]}{n}"
    db.table(ROW)[row] = {"name": row, "parent": disbursement, "parenttype": DISB, "idx": n,
                          "reference_doctype": SHEET, "reference_name": "SHEET-1", "reference_row": f"SR-{n}",
                          "party_type": None, "party": None, "partyb": db.table(SHEET_ROW)[f"SR-{n}"]["phone"],
                          "allocated_amount": 1500, "payment_status": "Initiated",
                          "b2c_disbursement_request": request, "reference_no": None, "claim_payee": None}
    db.table(REQ)[request] = {"name": request, "docstatus": 1, "status": status, "amount": 1500,
                              "transaction_id": f"RCP{n}" if status == "Paid" else None,
                              "b2c_payment_disbursement": disbursement, "b2c_payment_disbursement_reference": row,
                              "mpesa_settings": SETTINGS, "reference_doctype": SHEET, "reference_name": "SHEET-1",
                              "party_type": None, "party": None, "phone_number": db.table(ROW)[row]["partyb"],
                              "receiver_public_name": f"{db.table(ROW)[row]['partyb']} - JANE WANJIKU",
                              "transaction_completed_datetime": NOW, "creation": NOW, "accounting_booked": 0,
                              "accounting_error": None, **kw}
    return row, request


# ---------------------------------------------------------------- the registry
def test_handlers_come_from_hooks_py_the_last_path_winning(env, monkeypatch):
    module = types.ModuleType("r45_handlers")
    module.HANDLER = object()

    class Made:
        pass

    module.Made = Made
    monkeypatch.setitem(sys.modules, "r45_handlers", module)
    env.partyless.unregister(SHEET)
    env.frappe.get_hooks = lambda hook=None, default=None, app_name=None: {
        "Test Sheet": ["r45_handlers.Missing", "r45_handlers.HANDLER"], "Other Sheet": "r45_handlers.Made",
        "Broken Sheet": ["r45_handlers.Missing"]} if hook == "mpsa_partyless_payouts" else []
    found = env.partyless.handlers()
    assert found[SHEET] is module.HANDLER and isinstance(found["Other Sheet"], Made)
    assert "Broken Sheet" not in found and env.partyless.handler_for("Broken Sheet") is None
    assert env.frappe.errors[-1][1]["title"] == "M-Pesa party-less payout handler not loaded: r45_handlers.Missing"
    assert env.partyless.handlers()["Other Sheet"] is found["Other Sheet"]  # made once
    titles = [kw["title"] for _a, kw in env.frappe.errors]
    assert titles.count("M-Pesa party-less payout handler not loaded: r45_handlers.Missing") == 1  # logged once
    env.partyless.register("Other Sheet", env.handler)  # in-process wins
    assert env.partyless.handler_for("Other Sheet") is env.handler
    assert env.partyless.doctypes() == ["Other Sheet", SHEET]
    assert env.partyless.handler_for(None) is None and env.partyless.handler_for(EC) is None
    env.frappe.get_hooks = lambda hook=None, default=None, app_name=None: []  # a hook nobody set
    assert env.partyless.handlers() == {"Other Sheet": env.handler}


def test_the_hook_is_in_hooks_py_and_empty_until_a_feature_fills_it():
    tree = ast.parse((APP / "hooks.py").read_text(encoding="utf-8"))
    hooks = {n.targets[0].id: n.value for n in tree.body
             if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}
    # each feature adds its line (the bulk payout sheet first: tests/unit/test_r45_bulk_payout.py)
    registered = ast.literal_eval(hooks["mpsa_partyless_payouts"])
    assert all(isinstance(path, str) and path.startswith("mpsa_suite.money_out.") for path in registered.values())


# ---------------------------------------------------------------- the disbursement and its rows
def test_a_party_less_disbursement_is_saved_without_party_type_or_account_paid_to(env):
    doc = new_disbursement(env).insert()
    assert doc.name == "MPESA-B2C-1"
    assert [c for c in env.handler.calls if c[0] == "check_row"] == [("check_row", "SR-1", "MPESA-B2C-1"),
                                                                       ("check_row", "SR-2", "MPESA-B2C-1")]
    stored = env.frappe.db.table(ROW)["MPESA-B2C-1-R1"]
    assert (stored["reference_row"], stored["partyb"], stored["party"], stored.get("payee_account")) == (
        "SR-1", "254711000333", None, None)
    for field in ("company", "paid_from"):
        with raises(env, f"Field {field.replace('_', ' ').title()} is mandatory"):
            new_disbursement(env, **{field: None}).insert()
    with raises(env, "A payout of Test Sheet names no Party Type"):
        new_disbursement(env, party_type="Employee").insert()


@pytest.mark.parametrize("change, message", [
    ({"party_type": "Employee", "party": "EMP-1"}, "Row #1 pays Test Sheet SHEET-1 row SR-1: it names no Party"),
    ({"reference_row": None}, "Row #1 pays Test Sheet: it must name the document and its row it pays"),
    ({"claim_payee": "LN-1"}, "it is not an Expense Claim payee or a payment schedule line"),
    ({"allocated_amount": 5}, "Allocated Amount cannot be less than Kshs. 10"),
    ({"partyb": "0733"}, "is not a number M-Pesa B2C can pay"),
])
def test_a_party_less_row_is_checked(env, change, message):
    doc = new_disbursement(env, rows=("SR-1",))
    doc.references[0].__dict__.update(change)
    with raises(env, message):
        doc.insert()


def test_the_handlers_own_check_refuses_and_an_approved_account_is_never_named(env):
    env.handler.refuse = "SR-1 is Cancelled on SHEET-1"
    with raises(env, "SR-1 is Cancelled on SHEET-1"):
        new_disbursement(env).insert()
    env.handler.refuse = None
    doc = new_disbursement(env, rows=("SR-1",))
    doc.references[0].__dict__.update(payee_account_doctype="Employee M-Pesa Account", payee_account="MEA-2")
    doc.insert()
    assert (doc.references[0].payee_account_doctype, doc.references[0].payee_account) == (None, None)


def test_a_paying_row_is_paid_once(env):
    with raises(env, "Rows #1, #2 pay the same Test Sheet SHEET-1 row SR-1: a row is paid once"):
        new_disbursement(env, rows=("SR-1", "SR-1")).insert()
    # another disbursement pays it already
    paid_payout(env, 1, status="Initiated", disbursement="MPESA-B2C-7")
    with raises(env, r"Row #1 pays Test Sheet SHEET-1 row SR-1, which is being paid by MPESA-B2C-7 row #1 "
                     r"\(Initiated\): a row is never paid twice"):
        new_disbursement(env, rows=("SR-1",)).insert()
    env.frappe.db.table(REQ)["B2C-71"]["status"] = "Failed"  # M-Pesa said it failed: paid again
    new_disbursement(env, rows=("SR-1",)).insert()
    env.frappe.db.table(REQ)["B2C-71"].update(status="Paid", docstatus=2)  # Paid, though cancelled since
    env.frappe.db.table(DISB)["MPESA-B2C-7"]["docstatus"] = 2
    with raises(env, r"which is paid by MPESA-B2C-7 row #1 \(Paid\)"):
        new_disbursement(env, rows=("SR-1",)).insert()


def test_the_send_checks_each_row_under_the_documents_lock_not_what_it_owes(env):
    doc = new_disbursement(env).insert()
    other = new_disbursement(env, rows=("SR-2",)).insert()  # a draft too: whichever is sent first takes SR-2
    paid_payout(env, 3, status="Initiated", disbursement="MPESA-B2C-7")  # SR-3 on its way: not this payout's
    env.locks.clear()
    env.log.clear()
    submit(env, doc)
    assert env.locks == ["b2c-pay:Test Sheet:SHEET-1"]
    assert any(e[0] == "sql" and e[1][0] == ROW and e[2] for e in env.log), "a locking read of the rows"
    assert not [e for e in env.log if e[0] == "b2c_open_payouts"]  # never the document-wide guard
    jobs = [kw for method, kw in env.frappe.enqueued if method.endswith("create_b2c_request")]
    assert [j["ref"].reference_row for j in jobs] == ["SR-1", "SR-2"]
    # the other draft of SR-2, sent after: refused under the lock
    with raises(env, r"which is being paid by MPESA-B2C-1 row #2"):
        submit(env, other)


def test_nobody_sends_a_party_less_payout_to_themselves(env):
    env.frappe.db.table("User")[PAYER].update(mobile_no="0711 000 444")
    doc = new_disbursement(env).insert()
    summary = env.send_confirm._summary(doc)
    assert summary["problems"] == ["You may not send a payment to yourself: Row #2 pays your phone 254711000444. "
                                   "Another person must send it."]
    with raises(env, "Row #2 pays your phone", env.frappe.PermissionError):
        submit(env, doc)
    env.frappe.db.table("User")[PAYER].update(mobile_no=None, full_name="Ann Wambui Njeri")
    doc = new_disbursement(env, rows=("SR-3",)).insert()  # Ann Njeri: the sender's name
    with raises(env, "Row #1 pays 'Ann Njeri', your name", env.frappe.PermissionError):
        submit(env, doc)


def test_the_send_summary_shows_who_the_document_says(env):
    doc = new_disbursement(env).insert()
    summary = env.send_confirm._summary(doc)
    assert summary["problems"] == []
    assert [(r["receiver"], r["destination"], r["amount"], r["document"], r["party"]) for r in summary["rows"]] == [
        ("Jane Wanjiku", "M-Pesa phone 254711000333", 1500, "Test Sheet SHEET-1, row #1", "Site A"),
        ("Peter Otieno", "M-Pesa phone 254711000444", 1500, "Test Sheet SHEET-1, row #2", "Site A")]
    assert summary["total"] == 3000
    env.handler.describe = lambda row: 1 / 0  # a handler that fails: still shown, logged
    rows = env.send_confirm._summary(doc)["rows"]
    assert [(r["receiver"], r["document"]) for r in rows][0] == ("Not named", "Test Sheet SHEET-1")
    assert env.frappe.errors[-1][1]["title"] == "M-Pesa party-less payout not described"


# ---------------------------------------------------------------- the result and booking
def test_the_result_reaches_the_paying_row_and_the_payout_is_booked(env):
    _row, request = paid_payout(env, 1)
    env.response.update_b2c_reference_status(request, enqueue_next=True)
    calls = [c for c in env.handler.calls if c[0] in ("on_result", "book")]
    assert calls == [("on_result", "SR-1", "Paid", "RCP1"), ("book", "SR-1")]  # the row first, then booked
    sheet_row = env.frappe.db.table(SHEET_ROW)["SR-1"]
    assert (sheet_row["status"], sheet_row["receipt"], sheet_row["receiver_name"]) == (
        "Paid", "RCP1", "254711000333 - JANE WANJIKU")
    stored = env.frappe.db.table(REQ)[request]
    assert (stored["accounting_booked"], stored["accounting_error"]) == (1, None)
    assert env.fed == [request]  # its statement line
    # again (the 15-minute sweep): never booked twice
    env.response.update_b2c_reference_status(request, enqueue_next=True)
    assert [c for c in env.handler.calls if c[0] == "book"] == [("book", "SR-1")]
    assert env.postprocess.booked_docstatus("MPESA-B2C-7", _dict(env.frappe.db.table(ROW)["MPESA-B2C-7-R1"])) == 1


def test_a_failed_result_reaches_the_paying_row_and_books_nothing(env):
    _row, request = paid_payout(env, 2, status="Failed")
    env.response.update_b2c_reference_status(request, enqueue_next=True)
    assert [c for c in env.handler.calls if c[0] in ("on_result", "book")] == [("on_result", "SR-2", "Failed", None)]
    assert env.frappe.db.table(SHEET_ROW)["SR-2"]["status"] == "Failed"


def test_an_error_in_the_handlers_result_is_rolled_back_and_logged_and_booking_goes_on(env):
    _row, request = paid_payout(env, 1)
    env.handler.mode = "result fails"
    env.response.update_b2c_reference_status(request, enqueue_next=False)
    assert ("rollback", "mpesa_partyless_result") in env.log
    assert env.frappe.errors[-1][1]["title"] == "M-Pesa payout result not copied to Test Sheet"
    assert env.frappe.db.table(ROW)["MPESA-B2C-7-R1"]["payment_status"] == "Paid"  # the row itself is recorded
    env.handler.mode = "direct"
    env.handler.on_result = lambda row, req: (_ for _ in ()).throw(env.frappe.QueryDeadlockError())
    with pytest.raises(env.frappe.QueryDeadlockError):  # the transaction is gone: the job runs again
        env.response.update_b2c_reference_status(request, enqueue_next=False)


def test_a_deferred_booking_waits_without_an_error_and_the_sweep_books_it(env):
    _row, request = paid_payout(env, 1)
    env.handler.mode = "deferred"
    env.response.update_b2c_reference_status(request, enqueue_next=True)
    stored = env.frappe.db.table(REQ)[request]
    assert (stored["accounting_booked"], stored["accounting_error"]) == (
        0, "Waiting: the sheet's journal books it once no row waits")
    assert env.alerts == [] and env.frappe.errors == []
    env.handler.mode = "direct"
    env.postprocess.retry_unbooked_payouts()
    assert (stored["accounting_booked"], stored["accounting_error"]) == (1, None)
    assert [c for c in env.handler.calls if c[0] == "book"] == [("book", "SR-1"), ("book", "SR-1")]


def test_a_waiting_payout_whose_booking_then_fails_is_logged_and_alerted(env):
    _row, request = paid_payout(env, 1)
    env.handler.mode = "deferred"
    env.response.update_b2c_reference_status(request, enqueue_next=True)
    env.handler.mode = "error"
    env.postprocess.retry_unbooked_payouts()
    stored = env.frappe.db.table(REQ)[request]
    assert stored["accounting_booked"] == 0
    assert stored["accounting_error"] == "traceback"  # the traceback's last line (the fake site's traceback)
    assert [kw["title"] for _a, kw in env.frappe.errors] == ["B2C accounting failed for B2C-71"]
    assert len(env.alerts) == 1 and "B2C-71 was paid but could not be booked" in env.alerts[0][1]


def test_a_deferred_marker_without_a_note_names_the_document(env):
    _row, request = paid_payout(env, 1)
    env.handler.book = lambda row: env.partyless.DEFERRED
    env.response.update_b2c_reference_status(request, enqueue_next=True)
    assert env.frappe.db.table(REQ)[request]["accounting_error"] == "Waiting: booked later by Test Sheet SHEET-1"


def test_booked_and_the_closed_period_hint_ask_the_handler(env):
    row, _request = paid_payout(env, 1)
    ref = _dict(env.frappe.db.table(ROW)[row], reference_no="RCP1")
    assert env.postprocess.booked_docstatus("MPESA-B2C-7", ref) is None
    env.je("ACC-JV-X", "OTHER", [{"account": GL, "credit_in_account_currency": 1500}], docstatus=0)
    env.frappe.db.table(SHEET_ROW)["SR-1"]["journal_entry"] = "ACC-JV-X"
    assert env.postprocess.booked_docstatus("MPESA-B2C-7", ref) == 0
    no_receipt = _dict(ref, reference_no=None, b2c_disbursement_request=None)
    assert env.postprocess.booked_docstatus("MPESA-B2C-7", no_receipt) is None
    hint = env.postprocess.closed_period_hint(ref, "RCP1")
    assert hint.endswith("book it from Test Sheet SHEET-1 once the period is open, or with a date in an open "
                         "period.")
    env.handler.closed_period_hint = lambda row, receipt: f"Book Now on {row.reference_name} after opening it."
    assert env.postprocess.closed_period_hint(ref, "RCP1") == "Book Now on SHEET-1 after opening it."


def test_an_unregistered_doctype_books_as_before(env):
    paid_payout(env, 1)
    env.frappe.db.table(REQ)["B2C-71"]["reference_doctype"] = "Delivery Note"
    ref = env.frappe.get_doc(ROW, "MPESA-B2C-7-R1")
    ref.reference_doctype = "Delivery Note"
    with raises(env, "No accounting entry is defined for B2C payouts against Delivery Note"):
        env.postprocess._book("MPESA-B2C-7", ref)
    hint = env.postprocess.closed_period_hint(_dict(reference_doctype=EC, reference_name="HR-EXP-1"), "RCP1")
    assert "make a Payment Entry with Reference No RCP1, allocated to Expense Claim HR-EXP-1" in hint


# ---------------------------------------------------------------- the statement line's part of the entry
@pytest.fixture
def bulk(env):
    """Three paid rows booked in one journal (no receipt on it), as a bulk sheet books them."""
    for n in (1, 2, 3):
        paid_payout(env, n)
        env.frappe.db.table(SHEET_ROW)[f"SR-{n}"]["journal_entry"] = "ACC-JV-BULK"
    env.je("ACC-JV-BULK", "SHEET-1", [{"account": GL, "credit_in_account_currency": 4500},
                                      {"account": "Casual Wages - AIL", "debit_in_account_currency": 4500}],
           clearance_date=None)
    env.statement = importlib.import_module("mpsa_suite.money_out.statement")
    return env


def test_each_payout_takes_its_own_part_of_the_journal(bulk):
    statement = bulk.statement
    assert statement.entries_for("RCP2", GL) == [(JE, "ACC-JV-BULK", 1500)]
    db = bulk.frappe.db
    db.table(BT).update({"BT-A": {"name": "BT-A", "docstatus": 1}, "BT-B": {"name": "BT-B", "docstatus": 2}})
    db.table(BTP).update({
        "P1": {"name": "P1", "parent": "BT-A", "payment_document": JE, "payment_entry": "ACC-JV-BULK",
               "allocated_amount": 3500},
        "P2": {"name": "P2", "parent": "BT-B", "payment_document": JE, "payment_entry": "ACC-JV-BULK",
               "allocated_amount": 1000}})  # a cancelled line's allocation is no allocation
    assert statement.entries_for("RCP2", GL) == [(JE, "ACC-JV-BULK", 1000)]  # what is left: the line waits for staff
    db.table("Bank Transaction Payments")["P1"]["allocated_amount"] = 4500
    assert statement.entries_for("RCP2", GL) == []  # all of it taken
    db.table(BTP).clear()
    assert statement.entries_for("RCP2", "Other GL - AIL") == []  # it does not pay out of that account
    db.table(JE)["ACC-JV-BULK"]["clearance_date"] = "2026-10-18"
    assert statement.entries_for("RCP2", GL) == []  # cleared
    db.table(JE)["ACC-JV-BULK"].update(clearance_date=None, docstatus=0)
    assert statement.entries_for("RCP2", GL) == []  # a draft
    db.table(JE)["ACC-JV-BULK"]["docstatus"] = 1
    db.table(SHEET_ROW)["SR-2"]["journal_entry"] = None
    assert statement.entries_for("RCP2", GL) == []  # not booked yet


def test_an_entry_carrying_the_receipt_is_listed_once(bulk):
    # a top-up's own journal carries its receipt: found by the usual rule and by the handler, listed once
    bulk.frappe.db.table(SHEET_ROW)["SR-1"]["journal_entry"] = "ACC-JV-TOPUP"
    bulk.je("ACC-JV-TOPUP", "RCP1", [{"account": GL, "credit_in_account_currency": 1500}], clearance_date=None)
    assert bulk.statement.entries_for("RCP1", GL) == [(JE, "ACC-JV-TOPUP", 1500)]


def test_the_withdrawal_line_is_reconciled_with_its_part(bulk, monkeypatch):
    made = []
    tool = types.ModuleType("erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool")
    tool.reconcile_vouchers = lambda bt, vouchers: made.append((bt, json.loads(vouchers)))
    monkeypatch.setitem(sys.modules, "erpnext.accounts.doctype.bank_reconciliation_tool",
                        types.ModuleType("erpnext.accounts.doctype.bank_reconciliation_tool"))
    monkeypatch.setitem(sys.modules, tool.__name__, tool)
    bulk.frappe.db.table(BT)["BT-1"] = {"name": "BT-1", "docstatus": 1, "reference_number": "RCP3",
                                        "transaction_id": "RCP3", "unallocated_amount": 1500}
    assert bulk.statement.reconcile_withdrawal("BT-1", GL) == "ACC-JV-BULK"
    assert made == [("BT-1", [{"payment_doctype": JE, "payment_name": "ACC-JV-BULK", "amount": 1500}])]


def test_without_a_handler_the_statement_reads_nothing_more(bulk):
    bulk.partyless.unregister(SHEET)
    assert bulk.partyless.statement_entries("RCP1", GL) == []
    assert bulk.statement.entries_for("RCP1", GL) == []
    bulk.partyless.register(SHEET, bulk.handler)
    bulk.frappe.db.table(REQ)["B2C-72"]["reference_doctype"] = EC  # another DocType's payout
    assert bulk.partyless.statement_entries("RCP2", GL) == []
    bulk.handler.statement_entry = lambda request: 1 / 0
    assert bulk.partyless.statement_entries("RCP1", GL) == []
    assert bulk.frappe.errors[-1][1]["title"] == "M-Pesa party-less payout entry not found: B2C-71"


# ---------------------------------------------------------------- the retry
def request_doc(env, name):
    module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request")
    doc = object.__new__(module.B2CDisbursementRequest)
    doc.__dict__.update(env.frappe.db.table(REQ)[name], doctype=REQ, flags=_dict())
    return doc


def test_a_retry_checks_its_paying_row_not_the_document(env):
    _row, request = paid_payout(env, 1, status="Failed")
    paid_payout(env, 2, status="Initiated")  # another row of the sheet on its way: not this payout's business
    doc = request_doc(env, request)
    env.locks.clear()
    doc.validate_still_owed()
    assert env.locks == ["b2c-pay:Test Sheet:SHEET-1"]
    assert ("check_retry", "SR-1", request) in env.handler.calls and ("check_row", "SR-1", "MPESA-B2C-7") in (
        env.handler.calls)
    env.ed.check_b2c_retry(doc)  # its row names no party: no mismatch
    # the row was paid again by another batch since
    paid_payout(env, 1, status="Paid", disbursement="MPESA-B2C-8")
    with raises(env, r"which is paid by MPESA-B2C-8 row #1 \(Paid\)"):
        doc.validate_still_owed()


def test_a_retry_refuses_the_person_it_pays_and_the_handlers_refusal(env):
    _row, request = paid_payout(env, 1, status="Failed")
    doc = request_doc(env, request)
    env.frappe.db.table("User")[PAYER]["phone"] = "0711000333"
    with raises(env, "You may not resend a payment to yourself: B2C-71 pays your phone 254711000333",
                env.frappe.PermissionError):
        doc.validate_still_owed()
    env.frappe.db.table("User")[PAYER]["phone"] = None
    env.handler.refuse = "SR-1 was settled outside M-Pesa"
    with raises(env, "SR-1 was settled outside M-Pesa"):
        doc.validate_still_owed()


def test_a_request_by_hand_never_pays_a_party_less_row_and_rows_match_without_a_party(env):
    row, request = paid_payout(env, 1, status="Failed")
    doc = request_doc(env, request)
    assert env.ed.row_mismatch(doc) is None
    doc.flags = _dict()
    with raises(env, "Test Sheet pays its rows with the B2C Payment Disbursement it makes"):
        env.ed.check_b2c_request(doc_like(doc))
    # an unregistered row without a party still mismatches, as before
    env.frappe.db.table(ROW)[row]["reference_doctype"] = EC
    doc.reference_doctype = EC
    assert "is not that of MPESA-B2C-7 row" in env.ed.row_mismatch(doc)


def doc_like(doc):
    """The request as check_b2c_request reads it (doc.get / doc.flags)."""
    values = _dict(doc.__dict__)
    values.flags = _dict()
    return values


# ---------------------------------------------------------------- maker-checker, prints, names
def test_maker_checker_and_the_send_token_watch_the_paying_row(env):
    maker_checker = importlib.import_module("mpsa_suite.money_out.maker_checker")
    assert maker_checker.PAID[DISB][1][1][-1] == "reference_row"
    doc = new_disbursement(env).insert()
    before = env.send_confirm._destination(doc)
    doc.references[1].reference_row = "SR-3"
    assert env.send_confirm._destination(doc) != before


def test_a_batch_of_party_less_rows_is_sent_its_proofs_one_person_at_a_time(env):
    pp = importlib.import_module("mpsa_suite.money_out.payment_print")
    doc = _dict(party_type=None)
    rows = [_dict(party=None, party_type=None, partyb="0711000333", reference_row="SR-1", reference_doctype=SHEET,
                  reference_name="SHEET-1"),
            _dict(party=None, party_type=None, partyb="254711000444", reference_row="SR-2", reference_doctype=SHEET,
                  reference_name="SHEET-1")]
    assert [pp._row_party(doc, r) for r in rows] == [("Phone", "254711000333"), ("Phone", "254711000444")]
    assert pp._row_party(_dict(party_type="Employee"), _dict(party="EMP-1")) == ("Employee", "EMP-1")  # as before
    assert pp._email_of("Phone", "254711000333") is None
    assert pp._receiver(rows[1]) == "Peter Otieno" and pp._receiver(_dict(party="EMP-1")) == ""
    request = _dict(name="B2C-9", status="Failed", amount=1500, phone_number="254711000444", receiver_public_name=None,
                    party=None, reference_doctype=SHEET, reference_name="SHEET-1", mpesa_settings=None)
    assert pp._b2c_block(request, rows[1]).name == "Peter Otieno"


def test_the_name_lookups_read_party_less_payouts(env):
    _row, request = paid_payout(env, 1, receiver_public_name="254711000333 - JANE WANJIKU MUTHONI")
    # round 4.5: the names are read from the M-Pesa Known Name list, which the payout's Result adds to
    importlib.import_module("mpsa_suite.money_out.known_names").record_b2c(env.frappe.db.table(REQ)[request])
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")
    known = cp.mpesa_names(["0711 000 333"])["254711000333"]
    assert (known.name, known.payout) == ("JANE WANJIKU MUTHONI", "B2C-71")


# ---------------------------------------------------------------- every other DocType as before
def test_a_row_of_any_other_doctype_needs_its_party_and_names_no_paying_row(env):
    controller = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement_reference.b2c_payment_disbursement_reference")
    from test_payroll_schedule import Row

    def row(**kw):
        values = {"idx": 1, "party_type": "Employee", "party": "EMP-1", "reference_doctype": EC,
                  "reference_name": "HR-EXP-1", "allocated_amount": 1000, "outstanding_amount": None,
                  "partyb": "0722000111", "b2c_disbursement_request": None, **kw}
        return Row(controller.B2CPaymentDisbursementReference, **values)

    with raises(env, r"Row #1: Party is mandatory"):
        row(party=None).validate()
    with raises(env, "only a payout of a document that pays people no party names .* names a Paying Row; "
                     "Expense Claim is paid to its party"):
        row(reference_row="SR-1").validate()
    assert not [c for c in env.handler.calls if c[0] == "check_row"]


def test_a_disbursement_of_any_other_doctype_still_needs_party_type_and_account_paid_to(env):
    for field, label in (("party_type", "Party Type"), ("paid_to", "Paid To")):
        doc = new_disbursement(env, transaction_to_pay_against=EC, party_type="Employee", paid_to="Payable - AIL")
        doc.__dict__[field] = None
        with raises(env, f"Field {label} is mandatory"):
            doc.validate_mandatory_fields()
    doc = new_disbursement(env, transaction_to_pay_against=EC, party_type="Customer")
    with raises(env, "Party Type must be Employee or Supplier"):
        doc.validate_party_type()


def test_the_registry_answers_nothing_for_rows_it_does_not_know(env):
    partyless = env.partyless
    row = _dict(reference_doctype=EC, reference_name="HR-EXP-1", party="EMP-1")
    assert partyless.describe(row) == {} and partyless.receiver_of(row) == ""
    partyless.check_row(row)  # nothing to check
    assert partyless.self_pay_problem(_dict(references=[row]), PAYER) is None
    partyless.on_result(None, None)
    partyless.unregister(SHEET)
    paid_payout(env, 1)
    partyless.on_result("MPESA-B2C-7-R1", _dict(name="B2C-71"))  # no handler: nothing read
    assert not [e for e in env.log if e[:2] == ("row", ROW)]


# ---------------------------------------------------------------- the DocType JSON and the migrate
def _doctype(folder):
    return json.loads((APP / "mpsa_suite" / "doctype" / folder / f"{folder}.json").read_text(encoding="utf-8"))


def test_the_row_names_its_paying_row_and_party_type_is_not_required():
    data = _doctype("b2c_payment_disbursement_reference")
    fields = {f["fieldname"]: f for f in data["fields"]}
    field = fields["reference_row"]
    assert (field["fieldtype"], field["read_only"], field["search_index"]) == ("Data", 1, 1)
    assert data["field_order"].index("reference_row") == data["field_order"].index("reference_name") + 1
    assert data["modified"] >= "2026-11-13"  # newer than the site's: the migrate syncs it
    parent = _doctype("b2c_payment_disbursement")
    party_type = next(f for f in parent["fields"] if f["fieldname"] == "party_type")
    assert not party_type.get("reqd") and parent["modified"] >= "2026-11-13"
    js = (APP / "mpsa_suite" / "doctype" / "b2c_payment_disbursement" / "b2c_payment_disbursement.js").read_text(
        encoding="utf-8")
    assert "every((row) => row.reference_row)" in js


def test_account_paid_to_is_required_by_the_disbursement_not_the_custom_field(env, monkeypatch):
    custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    custom_field.create_custom_fields = lambda fields, update=False: None
    for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, custom_field.__name__, custom_field)
    migrate = importlib.import_module("mpsa_suite.mpsa_suite.migrate")
    made = []
    monkeypatch.setattr(migrate, "create_fields", lambda fields: made.append(fields))
    migrate.create_custom_erpnext_fields()
    fields = {f["fieldname"]: f for batch in made for f in batch.get(DISB, [])}
    assert fields["paid_to"]["reqd"] == 0 and fields["paid_from"]["reqd"] == 1 and fields["company"]["reqd"] == 1


# ---------------------------------------------------------------- the round's settings
SETTINGS_FOLDER = "m_pesa_suite_settings"


def test_the_new_settings_sit_on_the_money_out_tab_with_their_defaults():
    data = _doctype(SETTINGS_FOLDER)
    fields = {f["fieldname"]: f for f in data["fields"]}
    assert sorted(fields) == sorted(data["field_order"])
    order = data["field_order"]
    money_out = order[order.index("money_out_tab"):order.index("accounts_tab")]
    section = order.index("bulk_payouts_section")
    assert fields["bulk_payouts_section"]["label"] == "Bulk Payouts and Tills"
    for name, fieldtype, default in (("bulk_payout_book_after_hours", "Int", "2"),
                                     ("pay_via_staff_users", "Table MultiSelect", None),
                                     ("staff_advance_settle_days", "Int", "3"),
                                     ("petty_cash_alert_days", "Int", "7")):
        assert name in money_out and order.index(name) > section, name
        assert (fields[name]["fieldtype"], fields[name].get("default")) == (fieldtype, default), name
        assert fields[name].get("description"), name
    assert fields["pay_via_staff_users"]["options"] == "M-Pesa Pay via Staff User"
    assert "Empty: nobody" in fields["pay_via_staff_users"]["description"]
    assert data["modified"] >= "2026-11-13"


def test_the_pay_via_staff_user_table_is_a_child_of_the_suites_module_with_no_permissions():
    data = _doctype("m_pesa_pay_via_staff_user")
    assert (data["name"], data["module"], data["istable"], data["permissions"]) == (
        "M-Pesa Pay via Staff User", "Mpsa Suite", 1, [])
    (field,) = data["fields"]
    assert (field["fieldname"], field["fieldtype"], field["options"], field["reqd"]) == ("user", "Link", "User", 1)
    folder = APP / "mpsa_suite" / "doctype" / "m_pesa_pay_via_staff_user"
    assert (folder / "__init__.py").exists()
    assert "class MPesaPayviaStaffUser(Document)" in (folder / "m_pesa_pay_via_staff_user.py").read_text(
        encoding="utf-8")


def test_an_upgraded_site_gets_the_int_defaults(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        data = _doctype(SETTINGS_FOLDER)
        frappe.db.sql = lambda query, values=None, pluck=None, **kw: ["notify_role", "show_full_details"]
        frappe.get_meta = lambda doctype: types.SimpleNamespace(fields=[_dict(f) for f in data["fields"]])
        written = {}
        frappe.db.set_single_value = lambda doctype, field, value: written.__setitem__(field, value)
        install = importlib.import_module("mpsa_suite.install")
        install.backfill_single_defaults("M-Pesa Suite Settings")
        assert {k: written[k] for k in ("bulk_payout_book_after_hours", "staff_advance_settle_days",
                                        "petty_cash_alert_days")} == {"bulk_payout_book_after_hours": "2",
                                                                      "staff_advance_settle_days": "3",
                                                                      "petty_cash_alert_days": "7"}
        assert "pay_via_staff_users" not in written and "notify_role" not in written  # no default; kept as stored
    finally:
        fake_frappe.uninstall(saved)


def test_a_change_of_who_may_pay_via_staff_is_told(env):
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.m_pesa_suite_settings.m_pesa_suite_settings")
    assert "pay_via_staff_users" in module.WATCHED
    df = _dict(fieldtype="Table MultiSelect")
    assert module._shown(df, [_dict(user="janki@example.com"), _dict(user="owner@example.com")]) == (
        "janki@example.com, owner@example.com")
    assert module._shown(df, []) == module._shown(df, None) == "nobody"
