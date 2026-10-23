"""Round 3 review fixes, on an in-memory frappe.

Covers: an STK prompt never cancels a C2B register a cashier put on a POS or sales invoice
(submitted, no Payment Entry): its duplicate check raises, so the STK booking and a manager's
Reconcile book nothing, while a register booked with its Payment Entry is still cancelled and a
draft still deleted (P1); a disbursement submit counts Paid payouts nothing books (entry
cancelled, Cancel All): another disbursement's refuse it, this disbursement's own (an amended
copy's linked row) take their share of what is owed, and a Salary Slip is left to its own owed
(P2); a reversal result on a draft register whose receipt an STK prompt booked asks for the
booking to be undone by hand (P3); the STK booking names its receipt in frappe.flags while the
reference document books it, also clearing it when that raises, so a payment intent only counts
that prompt as pending (P7); the prepaid-order booker skips a register whose receipt has a live
reversal raised on another register (P8); a register submitted before it is in the table still
finds its receipt's reversal (P9); malformed Daraja ResultParameters give no items instead of
raising (P10); a submitted register with neither Payment Entry nor invoice still pays a payment
intent, one on an invoice does not, and without ERPNext no register query names the ERPNext
fields (P11).
"""

import contextlib
import datetime
import importlib
import re
import sys
import types

import pytest

import fake_frappe

NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)  # fake_frappe.utils.now_datetime
EXPRESS = "Mpesa Express Request"
REGISTER = "Mpesa C2B Payment Register"
REVERSAL = "Mpesa Reversal Request"
INTENT = "Mpesa Payment Intent"
SUITE = "M-Pesa Suite Settings"
MATCH_LOG = "Mpesa Match Log"
PARENT = "B2C Payment Disbursement"
REQ = "B2C Disbursement Request"
CLIENT = "Shop"
MPI = "MPI-26-10-000001"
EXPRESS_MODULE = "mpsa_suite.mpsa_suite.doctype.mpesa_express_request.mpesa_express_request"
INTENT_MODULE = "mpsa_suite.mpsa_suite.doctype.mpesa_payment_intent.mpesa_payment_intent"
REGISTER_MODULE = (
    "mpsa_suite.mpsa_suite.doctype.mpesa_c2b_payment_register.mpesa_c2b_payment_register"
)
REQ_MODULE = "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request"
DISB_MODULE = "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement"
PE_MODULE = "mpsa_suite.mpsa_suite.api.payment_entry"
# The register's fields that come with ERPNext (custom fields made by migrate.py)
ERPNEXT_REGISTER_FIELDS = {"payment_entry", "pos_invoice", "sales_invoice"}


class UnknownColumn(Exception):
    """What MariaDB raises for a column that does not exist (1054)."""


class Doc(fake_frappe.FakeDoc):
    """FakeDoc plus the Document methods the code under test calls."""

    def __init__(self, frappe, doctype, row):
        super().__init__(frappe, doctype, row)
        object.__setattr__(self, "flags", fake_frappe._dict())

    def save(self, *a, **kw):
        self._frappe.db.table(self.doctype)[self._row["name"]] = self._row
        return self

    def submit(self):
        self._row["docstatus"] = 1

    def reload(self):
        return self

    def set(self, key, value):
        self._row[key] = value

    def append(self, table, row):
        self._row.setdefault(table, []).append(row)

    def cancel(self):
        # Frappe's cancel; the register's on_cancel cancels its Payment Entry with it
        self._frappe.calls.append(("cancel", self.name))
        self._row["docstatus"] = 2

    def delete(self):
        self._frappe.calls.append(("delete", self.name))
        self._frappe.db.table(self.doctype).pop(self.name)

    def run_method(self, method, *args):
        if self.doctype != INTENT:
            raise AssertionError(f"unexpected run_method on {self.doctype}")
        controller = importlib.import_module(INTENT_MODULE).MpesaPaymentIntent()
        controller.name = self.name
        return getattr(controller, method)(*args)


def _support_docs(frappe):
    """Doc instances from get_doc, and what the doctype packages need on import."""
    frappe.calls = []
    frappe.utils.strip_html = lambda s: s
    frappe.utils.add_days = lambda d, n: d + datetime.timedelta(days=n)
    frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
    frappe.logger = lambda *a, **kw: types.SimpleNamespace(debug=print, info=print, error=print)
    frappe.db.db_type = "fake"  # named_lock is a no-op off MariaDB
    frappe.db.commit = lambda: None
    raw_get_doc = frappe.get_doc

    def get_doc(doctype, name=None, **kw):
        d = raw_get_doc(doctype, name, **kw)
        return Doc(frappe, d.doctype, d._row)

    def get_cached_doc(doctype, name=None, **kw):
        row = frappe.db.table(doctype).get(name or doctype)
        if row is None:
            raise frappe.DoesNotExistError(doctype)
        return Doc(frappe, doctype, row)

    frappe.get_doc = get_doc
    frappe.get_cached_doc = get_cached_doc
    frappe.new_doc = lambda doctype: Doc(frappe, doctype, {})
    frappe.db.table("DocType")[MATCH_LOG] = {"name": MATCH_LOG}


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


# ================================================================ P1: the prompt never cancels a register on an invoice
@pytest.fixture
def express(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        _support_docs(frappe)
        frappe.LinkExistsError = type("LinkExistsError", (frappe.ValidationError,), {})
        frappe.TimestampMismatchError = type("TimestampMismatchError", (frappe.ValidationError,), {})
        exceptions = types.ModuleType("frappe.exceptions")
        exceptions.DoesNotExistError = frappe.DoesNotExistError
        monkeypatch.setitem(sys.modules, "frappe.exceptions", exceptions)
        api = types.ModuleType("mpsa_suite.mpsa_suite.api.m_pesa_api")
        api.check_transaction_status = api.initiate_stk_push = lambda **kw: None
        sys.modules[api.__name__] = api  # dropped again by fake_frappe.uninstall
        mod = importlib.import_module(EXPRESS_MODULE)
        booked = []
        monkeypatch.setattr(mod, "handle_successful_transaction", lambda req, settings: booked.append(req.name))

        class Express(mod.MpesaExpressRequest):
            doctype = EXPRESS

            def __init__(self, name):
                self.name = name
                self.flags = fake_frappe._dict()
                self.reload()

            def reload(self):
                self.__dict__.update(frappe.db.table(EXPRESS)[self.name])

            def get(self, key, default=None):
                return self.__dict__.get(key, default)

        doc_for = frappe.get_doc

        def get_doc(doctype, name=None, **kw):
            if doctype == EXPRESS and name:
                return Express(name)
            return doc_for(doctype, name, **kw)

        frappe.get_doc = get_doc
        frappe.db.table("Mpesa Settings")["TEST"] = {"name": "TEST"}
        frappe.db.table(EXPRESS)["STK-5"] = {"name": "STK-5", "docstatus": 1, "status": "Completed",
                                             "transaction_id": "R5", "amount": 1000, "settings": "TEST",
                                             "is_reconciled": 0, "booking_attempts": 0,
                                             "reference_doctype": "Sales Invoice", "reference_name": "SINV-1"}
        yield fake_frappe._dict(frappe=frappe, mod=mod, Express=Express, booked=booked, monkeypatch=monkeypatch,
                                stk_verify=importlib.import_module("mpsa_suite.money_in.stk_verify"))
    finally:
        fake_frappe.uninstall(saved)


def c2b(env, docstatus, name="MPC2B-5", **kw):
    row = {"name": name, "transid": "R5", "transamount": 1000, "docstatus": docstatus, "payment_entry": None,
           "pos_invoice": None, "sales_invoice": None}
    row.update(kw)
    env.frappe.db.table(REGISTER)[name] = row
    return row


ON_INVOICE = [({"pos_invoice": "POS-1"}, "POS Invoice POS-1"), ({"sales_invoice": "SINV-1"}, "Sales Invoice SINV-1")]


@pytest.mark.parametrize("invoice,where", ON_INVOICE, ids=["pos-invoice", "sales-invoice"])
def test_prompt_never_cancels_a_register_a_cashier_put_on_an_invoice(express, invoice, where):
    c2b(express, 1, **invoice)  # POS quick pay: submitted, no Payment Entry, the invoice's payment row holds it
    for check in ("validate_duplicate_c2b_records", "on_update_after_submit"):
        with pytest.raises(express.frappe.ValidationError, match=where):
            getattr(express.Express("STK-5"), check)()
    assert express.frappe.calls == [], "cancelling would not take the money off the invoice"
    assert express.frappe.db.table(REGISTER)["MPC2B-5"]["docstatus"] == 1
    assert (REGISTER, "MPC2B-5") in express.frappe.db.locks, "decided on the register as committed"


@pytest.mark.parametrize("invoice,where", ON_INVOICE, ids=["pos-invoice", "sales-invoice"])
def test_stk_booking_of_a_receipt_on_an_invoice_books_nothing(express, invoice, where):
    c2b(express, 1, **invoice)
    assert express.stk_verify.book("STK-5") is False
    assert express.booked == [], "the prompt would book the money a second time"
    assert express.frappe.calls == [] and express.frappe.db.table(REGISTER)["MPC2B-5"]["docstatus"] == 1
    assert express.frappe.db.table(EXPRESS)["STK-5"]["is_reconciled"] == 0
    assert [kw.get("title", a[1] if len(a) > 1 else None) for a, kw in express.frappe.errors] == [
        "M-Pesa STK booking failed for STK-5"]


@pytest.mark.parametrize("invoice,where", ON_INVOICE, ids=["pos-invoice", "sales-invoice"])
def test_reconcile_click_on_a_receipt_on_an_invoice_is_refused(express, invoice, where):
    c2b(express, 1, **invoice)
    with pytest.raises(express.frappe.ValidationError, match=where):
        express.Express("STK-5").book_completed_payment()
    assert express.booked == [] and express.frappe.calls == []


def test_register_booked_with_its_payment_entry_is_still_cancelled(express):
    c2b(express, 1, payment_entry="PE-5")
    express.Express("STK-5").validate_duplicate_c2b_records()
    assert express.frappe.calls == [("cancel", "MPC2B-5")]
    express.stk_verify.book("STK-5")  # and the booking goes on (the register is cancelled now)
    assert express.booked == ["STK-5"]


def test_draft_register_is_still_deleted(express):
    c2b(express, 0)
    express.Express("STK-5").validate_duplicate_c2b_records()
    assert express.frappe.calls == [("delete", "MPC2B-5")]
    assert "MPC2B-5" not in express.frappe.db.table(REGISTER)


def test_without_erpnext_the_prompt_still_deletes_its_receipts_draft_register(express):
    express.frappe.get_installed_apps = lambda: ["frappe", "mpsa_suite"]
    raw = express.frappe.db.get_value

    def get_value(doctype, filters, fieldname="name", *a, **kw):
        named = set(fieldname) if isinstance(fieldname, (list, tuple)) else {fieldname}
        if doctype == REGISTER and named & ERPNEXT_REGISTER_FIELDS:
            raise UnknownColumn(f"Unknown column {sorted(named & ERPNEXT_REGISTER_FIELDS)} in {REGISTER}")
        return raw(doctype, filters, fieldname, *a, **kw)

    express.monkeypatch.setattr(express.frappe.db, "get_value", get_value)
    express.frappe.db.table(REGISTER)["MPC2B-5"] = {"name": "MPC2B-5", "transid": "R5", "transamount": 1000,
                                                    "docstatus": 0}
    express.Express("STK-5").validate_duplicate_c2b_records()
    assert express.frappe.calls == [("delete", "MPC2B-5")]


# ================================================================ P2: Paid payouts nothing books count at submit
@pytest.fixture
def payout(monkeypatch):
    frappe, saved = fake_frappe.install()
    b2c_api = types.ModuleType("mpsa_suite.mpsa_suite.api.b2c")
    b2c_api.make_b2c_payment_request = lambda name: None
    query_builder = types.ModuleType("frappe.query_builder")
    query_builder.DocType = lambda name: None
    try:
        with _stub_modules(**{b2c_api.__name__: b2c_api, "frappe.query_builder": query_builder}):
            frappe.utils.now = lambda: "2026-10-18 12:00:00"
            frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
            frappe.logger = lambda *a, **kw: types.SimpleNamespace(debug=print, info=print, error=print)
            frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
            req_mod = importlib.import_module(REQ_MODULE)
            common = importlib.import_module("mpsa_suite.money_in.common")
            disb = importlib.import_module(DISB_MODULE)
            owed_mod = importlib.import_module("mpsa_suite.money_out.owed")
            env = fake_frappe._dict(frappe=frappe, open=[], stale=[], b2b=[], owed=100000, calls=[], locks=[])

            @contextlib.contextmanager
            def named_lock(key, timeout=10):
                env.locks.append(key)
                try:
                    yield
                finally:
                    env.locks.remove(key)

            def open_payouts(doctype, name, exclude_parent=None, exclude_requests=None):
                env.calls.append(("open_payouts", doctype, name, exclude_parent, list(exclude_requests or [])))
                return [fake_frappe._dict(o) for o in env.open]

            def paid_without_entry(doctype, name):
                env.calls.append(("paid_without_entry", doctype, name, list(env.locks)))
                return [fake_frappe._dict(o) for o in env.stale]

            def current_outstanding(doctype, name, for_update=False):
                env.calls.append(("current_outstanding", doctype, name, for_update))
                return env.owed

            def b2b_open(doctype, name, exclude=None, for_update=False):
                env.calls.append(("b2b_open", doctype, name, for_update, list(env.locks)))
                return [fake_frappe._dict(o) for o in env.b2b]

            monkeypatch.setattr(common, "named_lock", named_lock)
            monkeypatch.setattr(disb, "open_payouts", open_payouts)
            monkeypatch.setattr(disb, "current_outstanding", current_outstanding)
            monkeypatch.setattr(req_mod, "paid_without_entry", paid_without_entry)
            monkeypatch.setattr(owed_mod, "b2b_open", b2b_open)

            def disbursement(name, *rows):
                doc = disb.B2CPaymentDisbursement()
                doc.name = name
                doc.references = [fake_frappe._dict({"reference_doctype": "Purchase Invoice",
                                                     "reference_name": "PI-1", "b2c_disbursement_request": None,
                                                     **row}) for row in rows]
                return doc

            env.disbursement = disbursement
            yield env
    finally:
        fake_frappe.uninstall(saved)


def stale_payout(request, parent, amount, parents=None):
    """A row of paid_without_entry: Paid, not listed by open_payouts, booked by no entry."""
    return {"request": request, "request_status": "Paid", "parent": parent, "parents": parents or [parent],
            "allocated_amount": amount, "reference_doctype": "Purchase Invoice", "reference_name": "PI-1"}


def test_submit_refused_by_another_disbursements_paid_payout_nothing_books(payout):
    # D-OLD paid PI-1 50,000; its Payment Entry was cancelled since, or D-OLD was cancelled with
    # its requests (Cancel All): open_payouts no longer lists it and PI-1 still owes it all
    payout.stale = [stale_payout("B2C-OLD", "D-OLD", 50000)]
    doc = payout.disbursement("D-NEW", {"allocated_amount": 50000})
    with pytest.raises(payout.frappe.ValidationError, match="awaiting accounting: D-OLD"):
        doc.validate_no_double_payout()
    key = "b2c-pay:Purchase Invoice:PI-1"
    assert ("paid_without_entry", "Purchase Invoice", "PI-1", [key]) in payout.calls, "read under the document lock"


def test_submit_refused_while_a_b2b_request_pays_the_invoice(payout):
    # an Initiated M-Pesa B2B request for PI-1: B2C's guard counts B2B too, under the same lock (R3)
    payout.b2b = [{"parent": "MB2B-26-10-000001", "allocated_amount": 30000, "request_status": "Initiated"}]
    doc = payout.disbursement("D-NEW", {"allocated_amount": 50000})
    with pytest.raises(payout.frappe.ValidationError, match="in progress or awaiting accounting: MB2B-26-10-000001"):
        doc.validate_no_double_payout()
    key = "b2c-pay:Purchase Invoice:PI-1"
    assert ("b2b_open", "Purchase Invoice", "PI-1", True, [key]) in payout.calls, "a locking read under the lock"


def test_b2b_is_not_asked_about_for_other_documents(payout):
    doc = payout.disbursement("D-NEW", {"allocated_amount": 500, "reference_doctype": "Expense Claim",
                                        "reference_name": "EC-1"})
    doc.validate_no_double_payout()
    assert not [c for c in payout.calls if c[0] == "b2b_open"]


def test_amended_disbursement_counts_its_own_paid_payout_against_what_is_owed(payout):
    # D-1 paid row 1 (B2C-1, 50,000) and was cancelled with it (Cancel All); its amendment D-1-1
    # keeps that row linked (not sent again) and adds a new row for PI-1, which owes 100,000
    payout.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "status": "Paid", "docstatus": 2}
    payout.stale = [stale_payout("B2C-1", "D-1", 50000, parents=["D-1", "D-1-1"])]
    copied = {"allocated_amount": 50000, "b2c_disbursement_request": "B2C-1"}

    too_much = payout.disbursement("D-1-1", copied, {"allocated_amount": 60000})
    with pytest.raises(payout.frappe.ValidationError, match=r"owes 100000.0; this disbursement allocates 110000.0"):
        too_much.validate_no_double_payout()

    payout.calls.clear()
    payout.disbursement("D-1-1", copied, {"allocated_amount": 50000}).validate_no_double_payout()
    assert ("current_outstanding", "Purchase Invoice", "PI-1", True) in payout.calls, "not refused by its own payout"


def test_payout_whose_row_lost_its_link_but_names_this_disbursement_is_its_own(payout):
    # found from the request, whose row (a row of this disbursement) no longer links it
    payout.stale = [stale_payout("B2C-9", "D-1-1", 50000, parents=["D-1-1"])]
    with pytest.raises(payout.frappe.ValidationError, match="allocates 110000.0"):
        payout.disbursement("D-1-1", {"allocated_amount": 60000}).validate_no_double_payout()


def test_salary_slip_is_left_to_its_own_owed(payout):
    # a Salary Slip's owed already takes off every Paid payout: counting it again refuses the rest
    payout.stale = [stale_payout("B2C-OLD", "D-OLD", 250000)]
    payout.owed = 50000
    doc = payout.disbursement("D-NEW", {"reference_doctype": "Salary Slip", "reference_name": "SS-1",
                                        "allocated_amount": 50000})
    doc.validate_no_double_payout()
    assert "paid_without_entry" not in [c[0] for c in payout.calls]


# ================================================================ P3: reversal result on a receipt an STK prompt booked
@pytest.fixture
def reversal_env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        _support_docs(frappe)
        frappe.roles = {}
        frappe.get_roles = lambda user=None: []
        frappe.db.table(SUITE)[SUITE] = {"name": SUITE, "amount_tolerance": 1}
        mod = importlib.import_module("mpsa_suite.money_out.reversal")
        frappe.db.table(REGISTER)["MPC2B-1"] = {"name": "MPC2B-1", "docstatus": 0, "transid": "RCPT000001",
                                                "transamount": 5000.0, "payment_entry": None}
        frappe.db.table(REVERSAL)["MRV-1"] = {"name": "MRV-1", "docstatus": 1, "status": "Initiated",
                                              "c2b_register": "MPC2B-1", "transaction_id": "RCPT000001",
                                              "conversation_id": "AG_1", "originator_conversation_id": "OCID-1"}
        yield fake_frappe._dict(frappe=frappe, mod=mod, monkeypatch=monkeypatch)
    finally:
        fake_frappe.uninstall(saved)


RESULT = {"ResultCode": 0, "ResultDesc": "Reversed", "ConversationID": "AG_1", "OriginatorConversationID": "OCID-1",
          "TransactionID": "SGT7H7RSMC", "ResultParameters": {"ResultParameter": [{"Key": "Charge", "Value": 0}]}}


def reversal_note(env, result=RESULT):
    env.mod.handle_result(result, "MRV-1")
    [text] = [c[2] for c in env.frappe.comments if c[1] == "MPC2B-1"]
    return text, env.frappe.db.table(REGISTER)["MPC2B-1"].get("match_method")


def stk_payment_entry(env, docstatus=1):
    env.frappe.db.table("Payment Entry")[f"PE-{docstatus}"] = {"name": f"PE-{docstatus}", "reference_no": "RCPT000001",
                                                               "docstatus": docstatus}


def stk_prompt(env, is_reconciled=1):
    env.frappe.db.table(EXPRESS)["STK-1"] = {"name": "STK-1", "transaction_id": "RCPT000001",
                                             "is_reconciled": is_reconciled}


@pytest.mark.parametrize("booking", [stk_payment_entry, stk_prompt], ids=["payment-entry", "reconciled-prompt"])
def test_reversal_of_an_stk_booked_receipt_asks_to_undo_the_booking(reversal_env, booking):
    # the prompt booked the receipt straight from it: its draft register was never submitted
    booking(reversal_env)
    text, marked = reversal_note(reversal_env)
    assert "must be cancelled or reversed by hand" in text and "not booked" not in text
    assert marked is None, "it was booked: not marked as a draft the reversal keeps unbooked"


def test_cancelled_entry_or_unreconciled_prompt_is_not_a_booking(reversal_env):
    stk_payment_entry(reversal_env, docstatus=2)
    stk_prompt(reversal_env, is_reconciled=0)
    text, marked = reversal_note(reversal_env)
    assert "It was not booked and will not be booked now." in text
    assert marked == "Reversed"


def without_payment_entries(env):
    """No ERPNext: no Payment Entry table to read."""
    env.frappe.get_installed_apps = lambda: ["frappe", "mpsa_suite"]
    raw = env.frappe.db.get_value

    def get_value(doctype, *a, **kw):
        if doctype == "Payment Entry":
            raise AssertionError("Table 'tabPayment Entry' doesn't exist")
        return raw(doctype, *a, **kw)

    env.monkeypatch.setattr(env.frappe.db, "get_value", get_value)


def test_without_erpnext_only_a_reconciled_prompt_is_a_booking(reversal_env):
    without_payment_entries(reversal_env)
    stk_prompt(reversal_env)
    text, marked = reversal_note(reversal_env)
    assert "must be cancelled or reversed by hand" in text and marked is None


def test_without_erpnext_and_no_prompt_the_draft_is_not_booked(reversal_env):
    without_payment_entries(reversal_env)
    text, marked = reversal_note(reversal_env)
    assert "It was not booked" in text and marked == "Reversed"


# ================================================================ P10: malformed ResultParameters
def daraja():
    return importlib.import_module("mpsa_suite.daraja.money_out")


@pytest.mark.parametrize("result", [
    "Result",
    [{"ResultParameters": {"ResultParameter": [{"Key": "Charge", "Value": 0}]}}],
    {"ResultParameters": "ResultParameter"},
    {"ResultParameters": [{"Key": "Charge", "Value": 0}]},
    {"ResultParameters": {"ResultParameter": 7}},
], ids=["str-result", "list-result", "str-holder", "list-holder", "int-parameter"])
def test_malformed_result_parameters_give_no_items(result):
    # these raised AttributeError / TypeError, so the result callback failed before recording it
    assert daraja().result_items(result) == []
    assert daraja().result_values(result) == {}


def test_parameters_without_a_text_key_are_dropped():
    result = {"ResultParameters": {"ResultParameter": [
        {"Key": 5, "Value": "five"}, {"Key": {"nested": 1}, "Value": "unhashable"}, {"Key": ["x"], "Value": 1},
        {"Key": None}, {"Key": ""}, "junk", {"Key": "Charge", "Value": 12},
    ]}}
    assert daraja().result_items(result) == [{"Key": "Charge", "Value": 12}]
    assert daraja().result_values(result) == {"Charge": 12}


def test_well_formed_parameters_still_read():
    single = {"ResultParameters": {"ResultParameter": {"Key": "Charge", "Value": 3}}}
    several = {"ResultParameters": {"ResultParameter": [{"Key": "A", "Value": 1}, {"Key": "A", "Value": 2},
                                                        {"Key": "B", "Value": None}]}}
    assert daraja().result_values(single) == {"Charge": 3}
    assert daraja().result_values(several) == {"A": 1, "B": None}  # the first occurrence wins
    for empty in (None, {}, {"ResultParameters": None}, {"ResultParameters": {"ResultParameter": None}}):
        assert daraja().result_items(empty) == []


def test_reversal_result_with_malformed_parameters_is_still_recorded(reversal_env):
    reversal_env.mod.handle_result(dict(RESULT, ResultParameters="garbage"), "MRV-1")
    row = reversal_env.frappe.db.table(REVERSAL)["MRV-1"]
    assert (row["status"], row["reversal_transaction_id"], row["charge"]) == ("Reversed", "SGT7H7RSMC", 0)


# ================================================================ P8: prepaid orders skip a receipt reversed elsewhere
def orders_sql(frappe):
    """book_for_submitted_orders' query, judged by the conditions it actually asks.

    The NOT EXISTS subquery's ``rv.<field> = reg.<field>`` terms (joined by OR) decide which
    reversal holds a register; SQL '=' never matches a NULL.
    """

    def sql(query, values=None, pluck=None, **kw):
        q = " ".join(query.split())
        assert "FROM `tabMpesa C2B Payment Register` reg JOIN `tabSales Order` so" in q, q
        sub = re.search(r"FROM `tabMpesa Reversal Request` rv WHERE (.+?) ORDER BY", q).group(1)
        assert "rv.docstatus = 1 AND rv.status IN %s" in sub, sub
        pairs = re.findall(r"rv\.(\w+) = reg\.(\w+)", sub)
        holds, limit = values
        reversals = [r for r in frappe.db.table(REVERSAL).values() if r["docstatus"] == 1 and r["status"] in holds]
        out = []
        for reg in sorted(frappe.db.table(REGISTER).values(), key=lambda r: r["creation"]):
            so = frappe.db.table("Sales Order").get(reg.get("matched_reference_name"))
            if (reg["docstatus"] != 0 or reg.get("matched_reference_doctype") != "Sales Order"
                    or reg.get("payment_entry") or not so or so["docstatus"] != 1
                    or so["status"] in ("Closed", "Completed")):
                continue
            if any(rv.get(a) is not None and rv.get(a) == reg.get(b) for rv in reversals for a, b in pairs):
                continue
            out.append(reg[pluck])
        return out[:limit]

    return sql


@pytest.fixture
def orders(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        _support_docs(frappe)
        frappe.db.sql = orders_sql(frappe)
        frappe.db.table(SUITE)[SUITE] = {"name": SUITE, "enable_matching": 1, "amount_tolerance": 1}
        matching = importlib.import_module("mpsa_suite.money_in.matching")
        booking = importlib.import_module("mpsa_suite.money_in.booking")
        booked = []
        monkeypatch.setattr(booking, "book_register", lambda name, method=None: booked.append(name))
        frappe.db.table("Sales Order")["SO-1"] = {"name": "SO-1", "docstatus": 1, "status": "To Deliver and Bill"}
        yield fake_frappe._dict(frappe=frappe, matching=matching, booked=booked)
    finally:
        fake_frappe.uninstall(saved)


def order_receipt(env, name, receipt, i, **kw):
    env.frappe.db.table(REGISTER)[name] = {"name": name, "docstatus": 0, "transid": receipt, "payment_entry": None,
                                           "matched_reference_doctype": "Sales Order", "matched_reference_name": "SO-1",
                                           "creation": i, **kw}


def order_reversal(env, register_name, receipt, status="Reversed", docstatus=1):
    name = f"MRV-{len(env.frappe.db.table(REVERSAL)) + 1}"
    env.frappe.db.table(REVERSAL)[name] = {"name": name, "c2b_register": register_name, "transaction_id": receipt,
                                           "status": status, "docstatus": docstatus}


def test_order_booker_skips_a_receipt_reversed_on_another_register(orders):
    order_receipt(orders, "REG-1", "R1", 1, docstatus=2)  # booked, reversed, then cancelled ...
    order_receipt(orders, "REG-1-1", "R1", 2, amended_from="REG-1")  # ... and amended: the same refunded money
    order_reversal(orders, "REG-1", "R1")
    order_receipt(orders, "REG-2", "R2", 3)  # reversed on itself
    order_reversal(orders, "REG-2", "R2", status="Unknown")
    order_receipt(orders, "REG-3", "R3", 4)  # its reversal failed: the money stayed with us
    order_reversal(orders, "REG-OLD", "R3", status="Failed")
    order_receipt(orders, "REG-4", "R4", 5)
    order_reversal(orders, "REG-OLD-2", "R4", docstatus=2)  # cancelled reversal
    orders.matching.book_for_submitted_orders()
    assert orders.booked == ["REG-3", "REG-4"]


# ================================================================ P9: a register submitted on insert finds its receipt's reversal
def test_register_not_yet_in_the_table_finds_its_receipts_reversal(reversal_env):
    pe = types.ModuleType(PE_MODULE)
    pe.create_and_reconcile_payment_reconciliation = pe.create_payment_entry = pe.get_outstanding_invoices = None
    sys.modules[pe.__name__] = pe  # dropped again by fake_frappe.uninstall
    doc = importlib.import_module(REGISTER_MODULE).MpesaC2BPaymentRegister()
    # inserted already submitted (e.g. auto-submit): the row is not in the table yet
    doc.__dict__.update(name="MPC2B-NEW", transid="RCPT000001", transamount=5000)
    with pytest.raises(reversal_env.frappe.ValidationError, match="reversal MRV-1 is Initiated"):
        doc.refuse_if_reversed()
    assert (REVERSAL, "MRV-1") in reversal_env.frappe.db.locks, "locking read"


# ================================================================ P7 / P11: the gateway
@pytest.fixture
def gateway(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        _support_docs(frappe)
        meta = {"Mpesa Settings": {"company"}, EXPRESS: {"verification_status"}}
        frappe.get_meta = lambda dt: types.SimpleNamespace(has_field=lambda f: f in meta.get(dt, set()))
        frappe.db.table(SUITE)[SUITE] = {"name": SUITE, "enable_matching": 1, "amount_tolerance": 1}
        frappe.db.table("Mpesa Settings")["Astral 4134019"] = {
            "name": "Astral 4134019", "business_shortcode": "4134019", "till_number": None, "use_c2b": 1,
            "use_stk": 1, "company": "Astral"}
        frappe.db.table("Mpesa API Client")[CLIENT] = {"name": CLIENT, "mpesa_settings": "Astral 4134019",
                                                       "can_verify_receipts": 1}
        pe_module = types.ModuleType(PE_MODULE)

        def create_payment_entry(company, customer, amount, currency, mop, party_type, posting_date, reference_no,
                                 reference_date, cost_center, submit, references=None):
            if reference_no in ns.fail_receipts:
                raise RuntimeError(f"cannot book {reference_no}")
            name = f"PE-{reference_no}"
            frappe.db.table("Payment Entry")[name] = {"name": name, "reference_no": reference_no, "docstatus": 1}
            return fake_frappe._dict(name=name)

        pe_module.create_payment_entry = create_payment_entry
        sys.modules[pe_module.__name__] = pe_module  # dropped again by fake_frappe.uninstall
        intents = importlib.import_module("mpsa_suite.gateway.intents")
        webhooks = importlib.import_module("mpsa_suite.gateway.webhooks")
        common = importlib.import_module("mpsa_suite.money_in.common")
        queued = []
        monkeypatch.setattr(webhooks, "queue", lambda intent: queued.append((intent.name, intent.status)))
        ns = fake_frappe._dict(frappe=frappe, intents=intents, common=common, queued=queued, fail_receipts=set(),
                               monkeypatch=monkeypatch)
        yield ns
    finally:
        fake_frappe.uninstall(saved)


def intent(env, name=MPI, **kw):
    row = {"name": name, "client": CLIENT, "status": "Prompted", "amount": 1500, "customer": "Jane",
           "receipt": None, "expires_at": NOW + datetime.timedelta(minutes=10), "prompts_sent": 1,
           "phone": "254712345678", "reference_doctype": None, "reference_name": None, "order_ref": "WA-1",
           "creation": NOW - datetime.timedelta(minutes=5)}
    row.update(kw)
    env.frappe.db.table(INTENT)[name] = row
    return env.frappe.get_doc(INTENT, name)


def prompt(env, name, **kw):
    row = {"name": name, "docstatus": 1, "reference_doctype": INTENT, "reference_name": MPI,
           "status": "In Progress", "transaction_id": None, "amount": 1500, "transaction_date": None,
           "settings": "Astral 4134019", "is_reconciled": 0, "creation": NOW - datetime.timedelta(minutes=5),
           "verification_status": None, "phone_number": "254712345678", "owner": "Administrator"}
    row.update(kw)
    env.frappe.db.table(EXPRESS)[name] = row
    return row


def register(env, name, receipt, billref=MPI, **kw):
    row = {"name": name, "docstatus": 0, "transid": receipt, "transamount": 1500, "transtime": "20261018115500",
           "full_name": "JANE DOE", "businessshortcode": "4134019", "billrefnumber": billref, "payment_entry": None,
           "pos_invoice": None, "sales_invoice": None, "matched_reference_name": None,
           "creation": NOW - datetime.timedelta(minutes=1)}
    row.update(kw)
    env.frappe.db.table(REGISTER)[name] = row
    return row


def payment_entry(env, receipt, docstatus=1):
    name = f"PE-{receipt}-{docstatus}"
    env.frappe.db.table("Payment Entry")[name] = {"name": name, "reference_no": receipt, "docstatus": docstatus}


def booked(env):
    return sorted(r["reference_no"] for r in env.frappe.db.table("Payment Entry").values() if r["docstatus"] < 2)


def status(env):
    row = env.frappe.db.table(INTENT)[MPI]
    return row["status"], row["receipt"]


def match_logs(env, receipt):
    return [r for r in env.frappe.db.table(MATCH_LOG).values() if r.get("receipt") == receipt]


def utils(env):
    return importlib.import_module("mpsa_suite.utils.utils")


def stk_booking(env, prompt_name):
    """What stk_verify.book ends in: the prompt's reference document books it (ERPNext installed)."""
    utils(env).handle_successful_transaction(env.frappe.get_doc(EXPRESS, prompt_name),
                                             types.SimpleNamespace(auto_create_sales_invoice=0))


# ---------------------------------------------------------------- P7: the booking names its receipt
def test_stk_booking_names_its_receipt_only_while_the_document_books_it(gateway):
    seen = []

    class Reference:
        def run_method(self, method, *args):
            seen.append((method, gateway.frappe.flags.get(gateway.common.BOOKING_RECEIPT_FLAG)))

    prompt(gateway, "STK-1", status="Completed", transaction_id="RCPT000001", reference_doctype="Sales Order",
           reference_name="SO-1")
    express = gateway.frappe.get_doc(EXPRESS, "STK-1")
    raw_get_doc = gateway.frappe.get_doc
    gateway.monkeypatch.setattr(gateway.frappe, "get_doc", lambda dt, name=None, **kw: (
        Reference() if dt == "Sales Order" else raw_get_doc(dt, name, **kw)))
    utils(gateway).handle_successful_transaction(express, None)
    assert seen == [("on_payment_authorized", "RCPT000001")]
    assert gateway.frappe.flags.get(gateway.common.BOOKING_RECEIPT_FLAG) is None
    assert gateway.frappe.db.table(EXPRESS)["STK-1"]["is_reconciled"] == 1


def test_receipt_flag_is_cleared_when_the_document_fails_to_book(gateway):
    class Reference:
        def run_method(self, method, *args):
            raise RuntimeError("accounting period closed")

    prompt(gateway, "STK-1", status="Completed", transaction_id="RCPT000001", reference_doctype="Sales Order",
           reference_name="SO-1")
    express = gateway.frappe.get_doc(EXPRESS, "STK-1")
    raw_get_doc = gateway.frappe.get_doc
    gateway.monkeypatch.setattr(gateway.frappe, "get_doc", lambda dt, name=None, **kw: (
        Reference() if dt == "Sales Order" else raw_get_doc(dt, name, **kw)))
    with pytest.raises(RuntimeError, match="accounting period closed"):
        utils(gateway).handle_successful_transaction(express, None)
    assert gateway.frappe.flags.get(gateway.common.BOOKING_RECEIPT_FLAG) is None, "a later booking would inherit it"
    assert gateway.frappe.db.table(EXPRESS)["STK-1"]["is_reconciled"] == 0


def test_gateway_reads_the_flag_the_stk_booking_sets(gateway):
    assert gateway.intents.BOOKING_RECEIPT_FLAG == gateway.common.BOOKING_RECEIPT_FLAG


def test_stk_booking_of_a_paid_intent_leaves_a_given_up_prompts_cancelled_entry(gateway):
    # STK-Y gave up; staff booked its receipt by hand, then cancelled that entry (refunded outside
    # the app). STK-X of the same paid intent is now booked by the STK booking: its own
    # register's entry was just cancelled in its favour, so only STK-X counts as pending.
    intent(gateway, status="Paid", receipt="RCPT000001")
    prompt(gateway, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    prompt(gateway, "STK-Y", status="Completed", transaction_id="RCPT000002", booking_attempts=3)
    payment_entry(gateway, "RCPT000002", docstatus=2)
    prompt(gateway, "STK-X", status="Completed", transaction_id="RCPT000003")
    payment_entry(gateway, "RCPT000003", docstatus=2)
    stk_booking(gateway, "STK-X")
    assert booked(gateway) == ["RCPT000003"], "the refunded RCPT000002 is not booked again"
    assert gateway.frappe.db.table(EXPRESS)["STK-X"]["is_reconciled"] == 1
    assert gateway.frappe.db.table(EXPRESS)["STK-Y"]["is_reconciled"] == 0
    assert gateway.frappe.flags.get(gateway.common.BOOKING_RECEIPT_FLAG) is None


def test_stk_booking_whose_receipt_fails_to_book_raises_and_clears_the_flag(gateway):
    intent(gateway, status="Prompted")
    prompt(gateway, "STK-1", status="Completed", transaction_id="RCPT000001")
    gateway.fail_receipts.add("RCPT000001")
    with pytest.raises(RuntimeError, match="RCPT000001"):
        stk_booking(gateway, "STK-1")
    assert gateway.frappe.flags.get(gateway.common.BOOKING_RECEIPT_FLAG) is None
    assert not gateway.frappe.flags.get(gateway.intents.STK_BOOKING_FLAG)
    assert gateway.frappe.db.table(EXPRESS)["STK-1"]["is_reconciled"] == 0


# ---------------------------------------------------------------- P11: consumed means on an invoice
def test_stk_receipt_whose_register_was_submitted_without_entry_or_invoice_pays_the_intent(gateway):
    # e.g. auto-reconcile with Submit Payment off: submitted, no Payment Entry, on no invoice
    intent(gateway, status="Prompted")
    prompt(gateway, "STK-1", status="Completed", transaction_id="RCPT000001")
    register(gateway, "REG-1", "RCPT000001", billref="something", docstatus=1)
    gateway.intents.refresh(MPI)
    assert status(gateway) == ("Paid", "RCPT000001")
    assert (MPI, "Paid") in gateway.queued


def test_register_naming_the_intent_submitted_without_entry_or_invoice_pays_it(gateway):
    intent(gateway, status="Expired", customer=None)
    register(gateway, "REG-1", "RCPT000001", docstatus=1)
    gateway.intents.refresh(MPI)
    assert status(gateway) == ("Paid", "RCPT000001")


def test_mark_paid_takes_such_a_register_and_still_locks_its_rows(gateway):
    doc = intent(gateway, status="Expired", customer=None)
    register(gateway, "REG-1", "RCPT000001", docstatus=1)
    gateway.intents.mark_paid(doc, "RCPT000001", 1500, NOW, "JANE", register="REG-1")
    assert status(gateway) == ("Paid", "RCPT000001")
    assert (REGISTER, "REG-1") in gateway.frappe.db.locks, "locking read: waits for a POS submit in flight"


@pytest.mark.parametrize("invoice,where", ON_INVOICE, ids=["pos-invoice", "sales-invoice"])
def test_register_on_an_invoice_still_never_pays_the_intent(gateway, invoice, where):
    from mpsa_suite.gateway.auth import ApiError

    doc = intent(gateway, status="Prompted")
    prompt(gateway, "STK-1", status="Completed", transaction_id="RCPT000001")
    register(gateway, "REG-1", "RCPT000001", docstatus=1, **invoice)
    register(gateway, "REG-2", "RCPT000002", docstatus=1, **invoice)
    gateway.intents.refresh(MPI)
    assert status(gateway) == ("Prompted", None) and booked(gateway) == []
    assert [r["result"] for r in match_logs(gateway, "RCPT000001")] == ["Error"]
    assert where in match_logs(gateway, "RCPT000002")[0]["details"]
    with pytest.raises(ApiError) as ctx:
        gateway.intents.mark_paid(doc, "RCPT000002", 1500, NOW, "JANE", register="REG-2")
    assert ctx.value.status == 409 and where in ctx.value.message


def without_erpnext(env):
    """No ERPNext: the register has no payment_entry / pos_invoice / sales_invoice columns."""
    f = env.frappe
    f.get_installed_apps = lambda: ["frappe", "mpsa_suite"]
    f.db.table(SUITE)[SUITE]["enable_matching"] = 0  # matching books Payment Entries: ERPNext only
    env.register_queries = []

    def check(doctype, filters, fields):
        if doctype != REGISTER:
            return
        named = set(filters) if isinstance(filters, dict) else set()
        named |= set(fields) if isinstance(fields, (list, tuple)) else {fields}
        env.register_queries.append(sorted(n for n in named if n))
        if named & ERPNEXT_REGISTER_FIELDS:
            raise UnknownColumn(f"Unknown column {sorted(named & ERPNEXT_REGISTER_FIELDS)} in {REGISTER}")

    raw_get_value, raw_get_all, raw_exists = f.db.get_value, f.db.get_all, f.db.exists

    def get_value(doctype, filters, fieldname="name", *a, **kw):
        check(doctype, filters, fieldname)
        return raw_get_value(doctype, filters, fieldname, *a, **kw)

    def get_all(doctype, filters=None, pluck=None, fields=None, **kw):
        check(doctype, filters, fields or [pluck])
        return raw_get_all(doctype, filters=filters, pluck=pluck, fields=fields, **kw)

    def exists(doctype, filters):
        check(doctype, filters, None)
        return raw_exists(doctype, filters)

    env.monkeypatch.setattr(f.db, "get_value", get_value)
    env.monkeypatch.setattr(f.db, "get_all", get_all)
    env.monkeypatch.setattr(f.db, "exists", exists)


def test_without_erpnext_no_register_is_consumed_and_none_is_read_for_it(gateway):
    without_erpnext(gateway)
    register(gateway, "REG-1", "RCPT000001", docstatus=1, pos_invoice="POS-1")  # (no such column for real)
    assert gateway.intents._consumed_register("RCPT000001") is None
    assert gateway.intents._consumed_register("RCPT000001", for_update=True) is None
    assert gateway.register_queries == []


def test_without_erpnext_a_submitted_register_pays_the_intent(gateway):
    # without ERPNext a register is submitted with no entry, and there are no invoices to be on
    without_erpnext(gateway)
    intent(gateway, status="Expired", customer=None)
    register(gateway, "REG-1", "RCPT000001", docstatus=1)
    gateway.intents.refresh(MPI)
    assert status(gateway) == ("Paid", "RCPT000001")
    assert gateway.register_queries, "the register was read, without the ERPNext fields"


def test_without_erpnext_an_stk_receipt_with_a_submitted_register_pays_the_intent(gateway):
    without_erpnext(gateway)
    intent(gateway, status="Prompted")
    prompt(gateway, "STK-1", status="Completed", transaction_id="RCPT000001")
    register(gateway, "REG-1", "RCPT000001", billref="something", docstatus=1)
    gateway.intents.refresh(MPI)
    assert status(gateway) == ("Paid", "RCPT000001")
