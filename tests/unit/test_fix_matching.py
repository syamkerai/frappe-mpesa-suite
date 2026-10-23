"""Receipt matching fixes, on an in-memory frappe that keeps this transaction's snapshot apart
from what is committed: plain reads see the snapshot, locking reads and writes see the rows as
committed (MariaDB REPEATABLE READ).

C05: an STK prompt neutralising the C2B register for its receipt decides on the committed row,
never on a stale snapshot, and raises when it cannot neutralise it (no second booking); the
register's own check sees a receipt a prompt committed after the booking's snapshot.
C06: the orphan-STK matcher only adopts prompts Safaricom never answered and last sent before the
payment (re-read with a lock), and a callback arriving after an adoption never overwrites the
adopted receipt or books again, also when the adoption only stamped a draft order.
C24: a paybill receipt naming a prompted payment intent is held only while the intent takes it
(refreshed by the matcher at any age) or books it, or when it was paid to another company's
number; a part payment is booked by the matcher.
C27: the STK review rows (Unverified / Mismatch) are written: "STK Callback" is a Match Log method.
"""

import datetime
import importlib
import json
import re
import sys
import types
from pathlib import Path

import pytest

import fake_frappe

NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)  # fake_frappe.utils.now_datetime
EXPRESS = "Mpesa Express Request"
REGISTER = "Mpesa C2B Payment Register"
INTENT = "Mpesa Payment Intent"
SUITE = "M-Pesa Suite Settings"
MATCH_LOG = "Mpesa Match Log"
REPO = Path(__file__).resolve().parents[2]
MATCH_LOG_JSON = REPO / "mpsa_suite/mpsa_suite/doctype/mpesa_match_log/mpesa_match_log.json"


def _select_options() -> dict:
    meta = json.loads(MATCH_LOG_JSON.read_text(encoding="utf-8"))
    return {f["fieldname"]: f["options"].split("\n") for f in meta["fields"] if f["fieldtype"] == "Select"}


class SnapshotDB(fake_frappe.FakeDB):
    """``snapshot[doctype]``, when set, is what this transaction's plain reads see; locking reads
    and every write use the committed tables."""

    def __init__(self, frappe):
        super().__init__(frappe)
        self.snapshot = {}
        self.db_type = "fake"  # named_lock is a no-op off MariaDB

    def _rows(self, doctype, filters, locking=False):
        rows = self.table(doctype) if locking or doctype not in self.snapshot else self.snapshot[doctype]
        if isinstance(filters, str):
            return [rows[filters]] if filters in rows else []
        return [r for r in rows.values() if fake_frappe._match(r, filters)]

    def get_value(self, doctype, filters, fieldname="name", as_dict=False, for_update=False, order_by=None, **kw):
        rows = self._rows(doctype, filters, locking=for_update)
        if not rows:
            return None
        row = rows[0]
        if for_update:
            self.locks.append((doctype, row["name"]))
        if isinstance(fieldname, (list, tuple)):
            values = {f: row.get(f) for f in fieldname}
            return fake_frappe._dict(values) if as_dict else tuple(values.values())
        return row.get(fieldname)

    def exists(self, doctype, filters):
        rows = self._rows(doctype, filters)
        return rows[0]["name"] if rows else None  # Frappe returns the name

    def commit(self):
        pass


class Doc(fake_frappe.FakeDoc):
    def __init__(self, frappe, doctype, row):
        super().__init__(frappe, doctype, row)
        object.__setattr__(self, "flags", fake_frappe._dict())

    def save(self, *a, **kw):
        self._frappe.db.table(self.doctype)[self._row["name"]] = self._row
        return self

    def reload(self):
        return self

    def set(self, key, value):
        self._row[key] = value


class MatchLogDoc(Doc):
    def insert(self, ignore_permissions=False):
        # Frappe's BaseDocument._validate_selects
        for field, options in _select_options().items():
            value = self._row.get(field)
            if value and value not in options:
                raise self._frappe.ValidationError(f'{field} cannot be "{value}"')
        return super().insert()


class RegisterDoc(Doc):
    """cancel / delete as Frappe does them on the row this doc was loaded from."""

    def cancel(self):
        f = self._frappe
        f.calls.append(("cancel", self.name))
        if f.fail_cancel:
            raise f.ValidationError("Payment Entry cannot be cancelled: it is reconciled")
        f.db.table(REGISTER)[self.name]["docstatus"] = 2

    def delete(self):
        f = self._frappe
        f.calls.append(("delete", self.name))
        if self.name in f.locked_by_other:
            # delete_doc locks with NOWAIT: a booking in flight holds the row
            raise f.QueryTimeoutError("Lock wait timeout exceeded")
        if f.delete_error:
            raise f.delete_error
        f.db.table(REGISTER).pop(self.name)


DOCS = {MATCH_LOG: MatchLogDoc, REGISTER: RegisterDoc}


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.db = SnapshotDB(frappe)
        frappe.LinkExistsError = type("LinkExistsError", (frappe.ValidationError,), {})
        frappe.TimestampMismatchError = type("TimestampMismatchError", (frappe.ValidationError,), {})
        frappe.QueryTimeoutError = type("QueryTimeoutError", (Exception,), {})
        frappe.utils.strip_html = lambda s: s
        frappe.utils.add_days = lambda d, n: d + datetime.timedelta(days=n)
        frappe.utils.add_to_date = lambda d, **kw: d + datetime.timedelta(**kw)  # seconds too (orphan window)
        frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)  # doctype package
        frappe.logger = lambda *a, **kw: types.SimpleNamespace(debug=print, info=print, error=print)
        frappe.calls, frappe.locked_by_other, frappe.fail_cancel, frappe.delete_error = [], set(), False, None
        meta = {EXPRESS: {"verification_status"}, "Mpesa Settings": {"company"}}
        frappe.get_meta = lambda dt: types.SimpleNamespace(has_field=lambda f: f in meta.get(dt, set()))
        frappe.new_doc = lambda dt: DOCS.get(dt, Doc)(frappe, dt, {})

        def get_doc(doctype, name=None, **kw):
            if isinstance(doctype, dict):
                row = dict(doctype)
                dt = row.pop("doctype")
                return DOCS.get(dt, Doc)(frappe, dt, row)
            rows = frappe.db.snapshot[doctype] if doctype in frappe.db.snapshot else frappe.db.table(doctype)
            if name not in rows:
                raise frappe.DoesNotExistError(f"{doctype} {name}")
            return DOCS.get(doctype, Doc)(frappe, doctype, rows[name])

        def get_cached_doc(doctype, name=None, **kw):
            row = frappe.db.table(doctype).get(name or doctype)
            if row is None:
                raise frappe.DoesNotExistError(doctype)
            return Doc(frappe, doctype, row)

        frappe.get_doc = get_doc
        frappe.get_cached_doc = get_cached_doc
        frappe.db.table("DocType")[MATCH_LOG] = {"name": MATCH_LOG}
        frappe.db.table(SUITE)[SUITE] = {"name": SUITE, "enable_matching": 1, "match_orphan_stk": 1,
                                         "amount_tolerance": 1, "orphan_stk_window_seconds": 600,
                                         "verify_stk_callbacks": 0}
        frappe.db.table("Sales Invoice")["SINV-1"] = {"name": "SINV-1", "customer": "Customer A", "docstatus": 1}
        yield fake_frappe._dict(frappe=frappe, monkeypatch=monkeypatch)
    finally:
        fake_frappe.uninstall(saved)


def match_logs(env, **filters):
    return [r for r in env.frappe.db.table(MATCH_LOG).values() if all(r.get(k) == v for k, v in filters.items())]


def prompt(env, name="STK-A", **kw):
    row = {"name": name, "docstatus": 1, "status": "In Progress", "transaction_id": None, "verification_status": None,
           "callback_payload": None, "amount": 1000, "reference_doctype": "Sales Invoice", "reference_name": "SINV-1",
           "creation": NOW - datetime.timedelta(minutes=3), "owner": "cashier@example.com", "is_reconciled": 0,
           "result_desc": None, "settings": "TEST"}
    row.update(kw)
    env.frappe.db.table(EXPRESS)[name] = row
    return row


def callback(receipt="R1A", paid=True, amount=1000) -> str:
    cb = {"MerchantRequestID": "1", "CheckoutRequestID": "ws_CO_1", "ResultCode": 0 if paid else 1032,
          "ResultDesc": "The service request is processed successfully." if paid else "Request cancelled by user"}
    if paid:
        cb["CallbackMetadata"] = {"Item": [{"Name": "Amount", "Value": amount},
                                           {"Name": "MpesaReceiptNumber", "Value": receipt},
                                           {"Name": "TransactionDate", "Value": 20261018115900}]}
    return json.dumps(cb)


# ================================================================ C27: STK review rows
def stk_verify(env, query=None, query_error=None):
    stk_poll = types.ModuleType("mpsa_suite.money_in.stk_poll")

    def stk_query(req):
        if query_error:
            raise query_error
        return query

    stk_poll.stk_query = stk_query
    sys.modules[stk_poll.__name__] = stk_poll  # dropped again by fake_frappe.uninstall
    module = importlib.import_module("mpsa_suite.money_in.stk_verify")
    env.booked = []
    env.monkeypatch.setattr(module, "book", lambda name: env.booked.append(name) or True)
    return module


def test_every_method_stk_verify_logs_is_a_match_log_option():
    options = _select_options()["method"]
    assert "STK Callback" in options
    source = (REPO / "mpsa_suite/money_in/stk_verify.py").read_text(encoding="utf-8")
    methods = set(re.findall(r'log_match\(\s*"[^"]+",\s*"([^"]+)"', source))
    assert methods and methods <= set(options), methods - set(options)


def test_unconfirmed_callback_is_left_for_review_in_the_match_log(env):
    env.frappe.db.table(SUITE)[SUITE]["verify_stk_callbacks"] = 1
    prompt(env, verification_status="Pending", callback_payload=callback(),
           creation=NOW - datetime.timedelta(hours=7))  # STK Query no longer answers
    module = stk_verify(env, query_error=ConnectionError("Daraja down"))
    assert module.settle("STK-A") == "Pending"
    assert env.frappe.db.table(EXPRESS)["STK-A"]["verification_status"] == "Unverified"
    [row] = match_logs(env, express_request="STK-A")
    assert (row["result"], row["method"]) == ("Error", "STK Callback")
    assert "Check it by hand" in row["details"]
    assert not any("Match Log write failed" in str(a) for a, _ in env.frappe.errors)
    assert env.booked == []


def test_contradicted_callback_is_left_for_review_in_the_match_log(env):
    env.frappe.db.table(SUITE)[SUITE]["verify_stk_callbacks"] = 1
    prompt(env, verification_status="Pending", callback_payload=callback())
    module = stk_verify(env, query={"ResultCode": "1032", "ResultDesc": "Request cancelled by user"})
    assert module.settle("STK-A") == "Mismatch"
    [row] = match_logs(env, express_request="STK-A")
    assert (row["result"], row["method"]) == ("Error", "STK Callback")
    assert env.booked == []


# ================================================================ C06: orphan STK adoption
def matching(env):
    return importlib.import_module("mpsa_suite.money_in.matching")


def orphan_receipt(amount=1000):
    # paid to the till at 11:58 with no account reference: only the orphan matcher can place it
    return fake_frappe._dict(name="MPC2B-B", transid="R2B", transamount=amount, transtime="20261018115800",
                             billrefnumber="", msisdn="")


@pytest.mark.parametrize("status,verification", [
    ("Failed", "Verified"),  # cancelled / wrong PIN, settled from its callback
    ("Failed", None),  # the status poller found it failed
    ("In Progress", "Pending"),  # paid: its receipt comes once Safaricom confirms the callback
    ("In Progress", "Unverified"),
    ("In Progress", "Mismatch"),
])
def test_prompt_safaricom_answered_is_never_adopted(env, status, verification):
    prompt(env, status=status, verification_status=verification)
    assert matching(env).adopt_orphan_stk(orphan_receipt()) is None


@pytest.mark.parametrize("status", ["In Progress", "Completed"])  # Completed: the poller said paid, no receipt
def test_unanswered_prompt_is_still_adopted(env, status):
    prompt(env, status=status)
    match = matching(env).adopt_orphan_stk(orphan_receipt())
    assert (match.method, match.doctype, match.name, match.customer, match.express) == (
        "Orphan STK Prompt", "Sales Invoice", "SINV-1", "Customer A", "STK-A")


def test_prompt_sent_again_after_the_payment_is_not_adopted(env):
    # STK-A failed at 11:56; B paid 11:58 with no reference; staff sent STK-A again at 11:59,
    # which cleared its Failed status and callback (same row, creation kept)
    prompt(env, creation=NOW - datetime.timedelta(minutes=5), timestamp=NOW - datetime.timedelta(minutes=1))
    assert matching(env).adopt_orphan_stk(orphan_receipt()) is None


def test_prompt_sent_again_before_the_payment_is_adopted(env):
    # created hours ago, sent again at 11:57, paid at 11:58 and the callback never came
    prompt(env, creation=NOW - datetime.timedelta(hours=3), timestamp=NOW - datetime.timedelta(minutes=3))
    assert matching(env).adopt_orphan_stk(orphan_receipt()).express == "STK-A"


def test_prompt_sent_again_after_the_payment_still_makes_another_ambiguous(env):
    # STK-A may have been the one paid before it was sent again: never pick STK-B by elimination
    prompt(env, creation=NOW - datetime.timedelta(minutes=5), timestamp=NOW - datetime.timedelta(minutes=1))
    prompt(env, "STK-B", creation=NOW - datetime.timedelta(minutes=4))
    match = matching(env).adopt_orphan_stk(orphan_receipt())
    assert match.ambiguous and sorted(match.candidates) == ["STK-A", "STK-B"]


def test_prompt_sent_outside_the_window_is_not_adopted(env):
    prompt(env, creation=NOW - datetime.timedelta(minutes=30), timestamp=NOW - datetime.timedelta(minutes=20))
    assert matching(env).adopt_orphan_stk(orphan_receipt()) is None


@pytest.mark.parametrize("answer", [{"verification_status": "Pending"}, {"status": "Failed"},
                                    {"transaction_id": "R1A", "status": "Completed"},
                                    {"timestamp": NOW - datetime.timedelta(minutes=1)}])  # sent again
def test_prompt_answered_after_the_matchers_snapshot_is_not_adopted(env, answer):
    row = prompt(env)
    env.frappe.db.snapshot[EXPRESS] = {"STK-A": dict(row)}  # a matcher batch started before the callback
    row.update(answer)
    assert matching(env).adopt_orphan_stk(orphan_receipt()) is None


def test_adopted_prompt_stays_locked_until_the_booking_commits(env):
    prompt(env)
    assert matching(env).adopt_orphan_stk(orphan_receipt()).express == "STK-A"
    assert (EXPRESS, "STK-A") in env.frappe.db.locks, "a callback stored meanwhile waits for the booking"


def adopted_prompt(env, payload, **kw):
    # the matcher booked receipt R2B for this prompt (booking._complete_orphan_prompt); then its callback came
    return prompt(env, status="Completed", transaction_id="R2B", verification_status="Pending", callback_payload=payload,
                  result_desc="Matched to C2B receipt R2B (no STK callback received)", **kw)


def test_callback_for_another_receipt_never_replaces_the_adopted_one(env):
    adopted_prompt(env, callback("R1A"))
    module = stk_verify(env)
    assert module.settle("STK-A") == "Mismatch"
    row = env.frappe.db.table(EXPRESS)["STK-A"]
    assert (row["transaction_id"], row["status"], row["verification_status"]) == ("R2B", "Completed", "Mismatch")
    assert "R1A" in row["result_desc"] and "R2B" in row["result_desc"]
    assert env.booked == [], "booking the prompt as well would credit the customer twice"
    [log] = match_logs(env, express_request="STK-A")
    assert (log["result"], log["method"], log["receipt"]) == ("Error", "STK Callback", "R2B")


def test_failed_callback_on_an_adopted_prompt_is_flagged_not_applied(env):
    adopted_prompt(env, callback(paid=False))
    module = stk_verify(env)
    assert module.settle("STK-A") == "Mismatch"
    row = env.frappe.db.table(EXPRESS)["STK-A"]
    assert (row["transaction_id"], row["status"], row["verification_status"]) == ("R2B", "Completed", "Mismatch")
    assert "cancelled" in row["result_desc"]
    assert match_logs(env, express_request="STK-A", result="Error")


def test_callback_confirming_the_adopted_receipt_is_settled_and_booked(env):
    adopted_prompt(env, callback("R2B"))
    module = stk_verify(env)
    assert module.settle("STK-A") == "Verified"
    row = env.frappe.db.table(EXPRESS)["STK-A"]
    assert (row["transaction_id"], row["verification_status"]) == ("R2B", "Verified")
    assert env.booked == ["STK-A"]


def adoption_left_on_the_order(env, docstatus=0, receipt="R2B"):
    # The matcher adopted STK-A for receipt R2B, but SO-1 is a draft that only took the stamp (part
    # payment, release refused, prepaid orders off): nothing is written on the prompt until the
    # register is booked, and booking it later (release, submitted order) does not write it either.
    env.frappe.db.table("Payment Request")["PR-1"] = {"name": "PR-1", "reference_doctype": "Sales Order",
                                                      "reference_name": "SO-1"}
    env.frappe.db.table(REGISTER)["MPC2B-B"] = {"name": "MPC2B-B", "transid": receipt, "docstatus": docstatus,
                                                "match_method": "Orphan STK Prompt", "creation": NOW,
                                                "matched_reference_doctype": "Sales Order",
                                                "matched_reference_name": "SO-1"}


@pytest.mark.parametrize("docstatus", [0, 1])  # 1: booked later, once the order was released
@pytest.mark.parametrize("reference", [("Payment Request", "PR-1"), ("Sales Order", "SO-1")])
def test_callback_after_an_adoption_the_prompt_does_not_carry_is_flagged(env, docstatus, reference):
    adoption_left_on_the_order(env, docstatus)
    prompt(env, reference_doctype=reference[0], reference_name=reference[1], verification_status="Pending",
           callback_payload=callback("R1A"))
    module = stk_verify(env)
    assert module.settle("STK-A") == "Mismatch"
    row = env.frappe.db.table(EXPRESS)["STK-A"]
    assert (row["transaction_id"], row["verification_status"]) == (None, "Mismatch")
    assert "R1A" in row["result_desc"] and "R2B" in row["result_desc"]
    assert env.booked == [], "the order would be credited with R1A and R2B"
    [log] = match_logs(env, express_request="STK-A")
    assert (log["result"], log["method"], log["receipt"]) == ("Error", "STK Callback", "R2B")


def test_callback_for_the_receipt_adopted_for_its_order_is_settled(env):
    adoption_left_on_the_order(env)
    prompt(env, reference_doctype="Sales Order", reference_name="SO-1", verification_status="Pending",
           callback_payload=callback("R2B"))  # the adoption was right: the prompt's booking replaces the register
    assert stk_verify(env).settle("STK-A") == "Verified"
    assert env.booked == ["STK-A"]


@pytest.mark.parametrize("change", [
    {"docstatus": 2},  # undone by hand
    {"match_method": "Customer Name"},
    {"matched_reference_name": "SO-2"},
])
def test_callback_is_settled_when_no_live_adoption_names_its_order(env, change):
    adoption_left_on_the_order(env)
    env.frappe.db.table(REGISTER)["MPC2B-B"].update(change)
    prompt(env, reference_doctype="Sales Order", reference_name="SO-1", verification_status="Pending",
           callback_payload=callback("R1A"))
    assert stk_verify(env).settle("STK-A") == "Verified"
    assert env.booked == ["STK-A"]


def test_adoption_another_prompt_carries_does_not_stop_this_callback(env):
    adoption_left_on_the_order(env, docstatus=1)
    prompt(env, "STK-Z", reference_doctype="Sales Order", reference_name="SO-1", status="Completed",
           transaction_id="R2B")  # booked at once: the matcher wrote the receipt on the prompt it adopted
    prompt(env, reference_doctype="Sales Order", reference_name="SO-1", verification_status="Pending",
           callback_payload=callback("R1A"))
    assert stk_verify(env).settle("STK-A") == "Verified"
    assert env.booked == ["STK-A"]


def test_adoption_committed_after_the_settle_snapshot_is_still_seen(env):
    row = adopted_prompt(env, callback("R1A"))
    env.frappe.db.snapshot[EXPRESS] = {"STK-A": {**row, "transaction_id": None, "status": "In Progress",
                                                 "result_desc": None}}
    module = stk_verify(env)
    assert module.settle("STK-A") == "Mismatch"
    assert env.frappe.db.table(EXPRESS)["STK-A"]["transaction_id"] == "R2B"
    assert env.booked == []


# ================================================================ C05: one booking per STK receipt
def express_module(env):
    exceptions = types.ModuleType("frappe.exceptions")
    exceptions.DoesNotExistError = env.frappe.DoesNotExistError
    env.monkeypatch.setitem(sys.modules, "frappe.exceptions", exceptions)
    api = types.ModuleType("mpsa_suite.mpsa_suite.api.m_pesa_api")
    api.check_transaction_status = api.initiate_stk_push = lambda **kw: None
    sys.modules[api.__name__] = api  # dropped again by fake_frappe.uninstall
    return importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.mpesa_express_request.mpesa_express_request"
    )


def stk_request(env, receipt="R5"):
    doc = express_module(env).MpesaExpressRequest()
    doc.__dict__.update(name="STK-5", transaction_id=receipt)
    return doc


def c2b_register(env, docstatus=0, name="MPC2B-5", receipt="R5"):
    row = {"name": name, "transid": receipt, "docstatus": docstatus, "payment_entry": "PE-5" if docstatus else None}
    env.frappe.db.table(REGISTER)[name] = row
    return row


def test_register_booked_after_the_snapshot_stops_the_prompt_booking(env):
    # The matcher booked the register (PE#1) while this STK booking ran: the snapshot shows a draft.
    row = c2b_register(env, docstatus=1)
    env.frappe.db.snapshot[REGISTER] = {"MPC2B-5": {**row, "docstatus": 0, "payment_entry": None}}
    env.frappe.locked_by_other.add("MPC2B-5")
    with pytest.raises(env.frappe.TimestampMismatchError):
        stk_request(env).validate_duplicate_c2b_records()
    assert env.frappe.calls == [], "nothing deleted or cancelled from a stale copy"
    assert env.frappe.db.table(REGISTER)["MPC2B-5"]["docstatus"] == 1
    assert (REGISTER, "MPC2B-5") in env.frappe.db.locks


def test_register_inserted_after_the_snapshot_stops_the_prompt_booking(env):
    c2b_register(env)
    env.frappe.db.snapshot[REGISTER] = {}
    with pytest.raises(env.frappe.TimestampMismatchError):
        stk_request(env).validate_duplicate_c2b_records()
    assert env.frappe.calls == []


def test_booked_register_is_cancelled_in_favour_of_the_prompt(env):
    c2b_register(env, docstatus=1)
    stk_request(env).validate_duplicate_c2b_records()
    assert env.frappe.calls == [("cancel", "MPC2B-5")]
    assert env.frappe.db.table(REGISTER)["MPC2B-5"]["docstatus"] == 2


def test_register_that_cannot_be_cancelled_is_raised_not_skipped(env):
    c2b_register(env, docstatus=1)
    env.frappe.fail_cancel = True
    with pytest.raises(env.frappe.ValidationError, match="cannot be cancelled"):
        stk_request(env).validate_duplicate_c2b_records()
    assert env.frappe.db.table(REGISTER)["MPC2B-5"]["docstatus"] == 1


def test_draft_register_is_deleted(env):
    c2b_register(env)
    stk_request(env).validate_duplicate_c2b_records()
    assert env.frappe.calls == [("delete", "MPC2B-5")]
    assert "MPC2B-5" not in env.frappe.db.table(REGISTER)


def test_draft_register_with_match_log_rows_is_left_as_a_draft(env):
    c2b_register(env)
    env.frappe.delete_error = env.frappe.LinkExistsError("Cannot delete: linked with Mpesa Match Log")
    stk_request(env).validate_duplicate_c2b_records()  # its own on_submit refuses it from now on
    assert env.frappe.db.table(REGISTER)["MPC2B-5"]["docstatus"] == 0


def test_draft_register_that_fails_to_delete_otherwise_is_raised(env):
    c2b_register(env)
    env.frappe.delete_error = RuntimeError("Deadlock found when trying to get lock")
    with pytest.raises(RuntimeError):
        stk_request(env).validate_duplicate_c2b_records()


def test_cancelled_register_or_none_is_left_alone(env):
    stk_request(env).validate_duplicate_c2b_records()
    c2b_register(env, docstatus=2)
    stk_request(env).validate_duplicate_c2b_records()
    stk_request(env, receipt=None).validate_duplicate_c2b_records()
    assert env.frappe.calls == []


def register_controller(env):
    pe = types.ModuleType("mpsa_suite.mpsa_suite.api.payment_entry")
    pe.create_and_reconcile_payment_reconciliation = pe.create_payment_entry = pe.get_outstanding_invoices = None
    sys.modules[pe.__name__] = pe  # dropped again by fake_frappe.uninstall
    module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.mpesa_c2b_payment_register.mpesa_c2b_payment_register"
    )
    doc = module.MpesaC2BPaymentRegister()
    env.steps = []
    doc.__dict__.update(name="MPC2B-6", transid="R6")
    doc.cancel = lambda: env.steps.append("cancel")
    doc._reconcile_payment = lambda: env.steps.append("reconcile")
    return doc


def test_register_refuses_a_receipt_a_prompt_committed_after_its_snapshot(env):
    env.frappe.db.table(EXPRESS)["STK-6"] = {"name": "STK-6", "transaction_id": "R6"}
    env.frappe.db.snapshot[EXPRESS] = {}  # the matcher's batch started before settle committed R6
    doc = register_controller(env)
    with pytest.raises(env.frappe.ValidationError, match="Duplicate C2B transaction R6"):
        doc.on_submit()
    assert env.steps == ["cancel"]
    assert (EXPRESS, "STK-6") in env.frappe.db.locks


def test_register_for_a_receipt_no_prompt_carries_is_booked(env):
    doc = register_controller(env)
    doc.on_submit()
    assert env.steps == ["reconcile"]


# ================================================================ C24: receipts naming a prompted intent
def gateway(env):
    f = env.frappe
    for name, code, company in (("Astral 4134019", "4134019", "Astral"), ("Other 555555", "555555", "Other Co")):
        f.db.table("Mpesa Settings")[name] = {"name": name, "business_shortcode": code, "till_number": None,
                                              "use_c2b": 1, "use_stk": 1, "company": company}
    f.db.table("Mpesa API Client")["Shop"] = {"name": "Shop", "mpesa_settings": "Astral 4134019"}
    f.db.table("Sales Invoice")["SINV-9"] = {"name": "SINV-9", "customer": "Jane", "docstatus": 1}
    intents = importlib.import_module("mpsa_suite.gateway.intents")
    webhooks = importlib.import_module("mpsa_suite.gateway.webhooks")
    env.booked = []

    def book_direct(intent, receipt, amount, paid_at, settings_name, note=None):
        env.booked.append((intent.name, receipt, amount))
        f.db.table("Payment Entry")[f"PE-{receipt}"] = {"name": f"PE-{receipt}", "reference_no": receipt, "docstatus": 1}

    env.monkeypatch.setattr(intents, "_book_direct", book_direct)
    env.monkeypatch.setattr(webhooks, "queue", lambda intent: None)
    return intents


def payment_intent(env, **kw):
    row = {"name": "MPI-1", "client": "Shop", "status": "Prompted", "amount": 1500, "customer": "Jane",
           "receipt": None, "expires_at": NOW + datetime.timedelta(minutes=10), "order_ref": "WA-1",
           "reference_doctype": "Sales Invoice", "reference_name": "SINV-9", "prompts_sent": 1}
    row.update(kw)
    env.frappe.db.table(INTENT)["MPI-1"] = row
    prompt(env, "STK-I", reference_doctype=INTENT, reference_name="MPI-1", amount=1500)
    return row


def paybill(env, amount, receipt="RI1", shortcode="4134019"):
    row = fake_frappe._dict(name=f"MPC2B-{receipt}", transid=receipt, transamount=amount, transtime="20261018115500",
                            billrefnumber="MPI-1", businessshortcode=shortcode, full_name="JANE DOE", docstatus=0,
                            payment_entry=None, matched_reference_name=None, creation=NOW)
    env.frappe.db.table(REGISTER)[row.name] = row
    return row


def test_part_payment_on_a_prompted_intent_is_booked_by_the_matcher(env):
    gateway(env)
    payment_intent(env)
    match = matching(env).resolve_reference(paybill(env, 1000))
    assert not match.get("hold"), match
    assert (match.doctype, match.name, match.customer) == ("Sales Invoice", "SINV-9", "Jane")
    assert env.frappe.db.table(INTENT)["MPI-1"]["status"] == "Prompted"
    assert env.booked == []


def test_part_payment_on_a_prompted_intent_without_document_goes_to_its_customer(env):
    gateway(env)
    payment_intent(env, reference_doctype=None, reference_name=None)
    match = matching(env).resolve_reference(paybill(env, 700))
    assert (match.doctype, match.name, match.customer) == ("Customer", "Jane", "Jane")


def test_late_paybill_payment_on_a_long_closed_intent_pays_it(env):
    # the prompt was ignored; the intent expired three days ago, beyond the one-day sync_open look-back
    gateway(env)
    payment_intent(env, status="Expired", expires_at=NOW - datetime.timedelta(days=3))
    match = matching(env).resolve_reference(paybill(env, 1500))
    assert match.hold and "booked by payment intent MPI-1" in match.details
    intent = env.frappe.db.table(INTENT)["MPI-1"]
    assert (intent["status"], intent["receipt"]) == ("Paid", "RI1")
    assert env.booked == [("MPI-1", "RI1", 1500)], "booked straight from the receipt, the register kept a draft"


def test_receipt_paying_an_open_intent_is_still_left_to_the_gateway(env):
    # e.g. the C2B confirmation of the STK payment itself: upstream would cancel a submitted register
    gateway(env)
    payment_intent(env)
    match = matching(env).resolve_reference(paybill(env, 1500))
    assert match.hold and "booked by payment intent MPI-1" in match.details
    assert env.booked == [("MPI-1", "RI1", 1500)]


def test_money_paid_again_on_a_paid_intent_is_left_to_the_gateway(env):
    gateway(env)
    payment_intent(env, status="Paid", receipt="R0")
    match = matching(env).resolve_reference(paybill(env, 1500, receipt="RI3"))
    assert match.hold, "sync_open books it on account (book_extra_receipts), straight from the receipt"
    assert env.booked == []


def test_receipt_paid_to_another_company_is_held_for_staff(env):
    gateway(env)
    payment_intent(env)
    match = matching(env).resolve_reference(paybill(env, 1500, shortcode="555555"))
    assert match.hold, "never booked against the intent's document from another company's money"
    assert env.frappe.db.table(INTENT)["MPI-1"]["status"] == "Prompted"
    assert env.booked == []


@pytest.mark.parametrize("amount", [1000, 1600])  # a part payment, or more than the intent
def test_any_amount_paid_to_another_company_is_held_for_staff(env, amount):
    gateway(env)
    payment_intent(env)
    match = matching(env).resolve_reference(paybill(env, amount, shortcode="555555"))
    assert match.hold and "555555, outside the client's company" in match.details, match
    assert not match.get("doctype"), "never booked on SINV-9 from Other Co's books"
    assert env.frappe.db.table(INTENT)["MPI-1"]["status"] == "Prompted"
    assert env.booked == []
    [log] = match_logs(env, register="MPC2B-RI1", result="Unmatched")  # the gateway's note to staff
    assert "book it by hand" in log["details"]


def test_money_paid_again_to_another_company_is_held_for_staff(env):
    # the gateway books extra money on a paid intent only from the client's own numbers
    gateway(env)
    payment_intent(env, status="Paid", receipt="R0")
    match = matching(env).resolve_reference(paybill(env, 1500, receipt="RI3", shortcode="555555"))
    assert match.hold and "outside the client's company" in match.details, match
    assert env.booked == []
