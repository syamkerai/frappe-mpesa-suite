"""Round 2 gateway review fixes, on an in-memory frappe.

Covers: a receipt with a live reversal (found by the register it was raised on or by the
receipt itself, so a register amended after its reversal stays held) never pays a payment
intent, is never booked by the gateway and is never offered to or taken by POS quick pay; a
draft already booked by a Payment Entry (as the gateway books prompted intents) or that paid a
payment intent is never put on a POS invoice, and a register a POS invoice consumed never pays
an intent (refresh, the pay page, mark_paid) and is never booked again by the gateway; the
gateway and POS take the register rows before the receipt and booking locks; a prompt whose STK
booking gave up is not re-booked after staff cancel its Payment Entry (only the prompt the STK
booking names is pending), an STK booking that cancelled its receipt's register entry never
commits without rebooking it, and one whose own receipt fails to book raises; an STK receipt on
any record of the client's company is found by verify_receipt.
"""

import datetime
import importlib
import inspect
import sys
import types

import pytest

import fake_frappe

NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)
INTENT = "Mpesa Payment Intent"
EXPRESS = "Mpesa Express Request"
REGISTER = "Mpesa C2B Payment Register"
REVERSAL = "Mpesa Reversal Request"
SUITE = "M-Pesa Suite Settings"
MATCH_LOG = "Mpesa Match Log"
CLIENT = "Shop"
MPI = "MPI-26-10-000001"


class Doc(fake_frappe.FakeDoc):
    """FakeDoc plus the Document methods the gateway and POS code call."""

    def __init__(self, frappe, doctype, row):
        super().__init__(frappe, doctype, row)
        object.__setattr__(self, "flags", fake_frappe._dict())

    def save(self, *a, **kw):
        self._frappe.db.table(self.doctype)[self._row["name"]] = self._row
        self._frappe.saved.append((self.doctype, self._row["name"]))
        return self

    def submit(self):
        self._row["docstatus"] = 1
        self._frappe.saved.append(("submit", self._row["name"]))

    def reload(self):
        return self

    def set(self, key, value):
        self._row[key] = value

    def append(self, table, row):
        self._row.setdefault(table, []).append(row)


def _match(row, filters):
    """fake_frappe's matcher plus "not in" (the POS list filters with it)."""
    rest = {}
    for key, cond in (filters or {}).items():
        if isinstance(cond, (list, tuple)) and cond[0] == "not in":
            if row.get(key) in cond[1]:
                return False
        else:
            rest[key] = cond
    return _plain_match(row, rest)


_plain_match = fake_frappe._match


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        monkeypatch.setattr(fake_frappe, "_match", _match)
        frappe.saved = []
        frappe.utils.strip_html = lambda s: s
        frappe.utils.add_days = lambda d, n: d + datetime.timedelta(days=n)
        frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
        frappe.logger = lambda *a, **kw: None
        frappe.db.db_type = "fake"  # named_lock is a no-op off MariaDB
        meta = {"Mpesa Settings": {"company"}, EXPRESS: {"verification_status"}}
        frappe.get_meta = lambda dt: types.SimpleNamespace(has_field=lambda f: f in meta.get(dt, set()))
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
        frappe.db.table(SUITE)[SUITE] = {"name": SUITE, "enable_matching": 1, "amount_tolerance": 1}
        for name, code, company in (("Astral 4134019", "4134019", "Astral"), ("Other 555555", "555555", "Other Co")):
            frappe.db.table("Mpesa Settings")[name] = {"name": name, "business_shortcode": code, "till_number": None,
                                                       "use_c2b": 1, "use_stk": 1, "company": company}
        frappe.db.table("Mpesa API Client")[CLIENT] = {"name": CLIENT, "mpesa_settings": "Astral 4134019",
                                                       "can_verify_receipts": 1}

        # the real _book_direct, with ERPNext's Payment Entry maker replaced
        pe_module = types.ModuleType("frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry")

        def create_payment_entry(company, customer, amount, currency, mop, party_type, posting_date, reference_no,
                                 reference_date, cost_center, submit, references=None):
            if reference_no in ns.fail_receipts:
                raise RuntimeError(f"cannot book {reference_no}")
            name = f"PE-{reference_no}"
            frappe.db.table("Payment Entry")[name] = {"name": name, "reference_no": reference_no, "docstatus": 1,
                                                      "party": customer, "paid_amount": amount}
            return fake_frappe._dict(name=name)

        pe_module.create_payment_entry = create_payment_entry
        sys.modules[pe_module.__name__] = pe_module  # dropped again by fake_frappe.uninstall

        intents = importlib.import_module("frappe_mpsa_payments.gateway.intents")
        webhooks = importlib.import_module("frappe_mpsa_payments.gateway.webhooks")
        common = importlib.import_module("frappe_mpsa_payments.money_in.common")
        queued = []
        monkeypatch.setattr(webhooks, "queue", lambda intent: queued.append((intent.name, intent.status)))
        ns = fake_frappe._dict(frappe=frappe, intents=intents, common=common, queued=queued, fail_receipts=set(),
                               monkeypatch=monkeypatch)
        yield ns
    finally:
        fake_frappe.uninstall(saved)


# ---------------------------------------------------------------- fixtures
def intent(env, name=MPI, **kw):
    row = {"name": name, "client": CLIENT, "status": "Prompted", "amount": 1500, "customer": "Jane",
           "receipt": None, "expires_at": NOW + datetime.timedelta(minutes=10), "prompts_sent": 1,
           "phone": "254712345678", "reference_doctype": None, "reference_name": None, "order_ref": "WA-1",
           "creation": NOW - datetime.timedelta(minutes=5)}
    row.update(kw)
    env.frappe.db.table(INTENT)[name] = row
    return env.frappe.get_doc(INTENT, name)


def prompt(env, name, intent_name=MPI, **kw):
    row = {"name": name, "docstatus": 1, "reference_doctype": INTENT, "reference_name": intent_name,
           "status": "In Progress", "transaction_id": None, "amount": 1500, "transaction_date": None,
           "settings": "Astral 4134019", "is_reconciled": 0, "creation": NOW - datetime.timedelta(minutes=5),
           "verification_status": None, "callback_payload": None}
    row.update(kw)
    env.frappe.db.table(EXPRESS)[name] = row
    return row


def register(env, name, receipt, shortcode="4134019", billref=MPI, amount=1500, **kw):
    row = {"name": name, "docstatus": 0, "transid": receipt, "transamount": amount, "transtime": "20261018115500",
           "full_name": "JANE DOE", "businessshortcode": shortcode, "billrefnumber": billref, "payment_entry": None,
           "matched_reference_name": None, "mode_of_payment": "Mpesa-Astral 4134019",
           "creation": NOW - datetime.timedelta(minutes=1)}
    row.update(kw)
    env.frappe.db.table(REGISTER)[name] = row
    return row


def reversal(env, register_name=None, receipt=None, status="Reversed", docstatus=1):
    name = f"MRV-{len(env.frappe.db.table(REVERSAL)) + 1}"
    env.frappe.db.table(REVERSAL)[name] = {"name": name, "c2b_register": register_name, "transaction_id": receipt,
                                           "status": status, "docstatus": docstatus, "creation": name}
    return name


def payment_entry(env, receipt, docstatus=1):
    name = f"PE-{receipt}-{docstatus}"
    env.frappe.db.table("Payment Entry")[name] = {"name": name, "reference_no": receipt, "docstatus": docstatus}
    return name


def booked(env):
    return sorted(r["reference_no"] for r in env.frappe.db.table("Payment Entry").values() if r["docstatus"] < 2)


def match_logs(env, receipt=None):
    return [r for r in env.frappe.db.table(MATCH_LOG).values() if receipt is None or r.get("receipt") == receipt]


def status(env, name=MPI):
    row = env.frappe.db.table(INTENT)[name]
    return row["status"], row["receipt"]


# ================================================================ item 2: reversals keyed by receipt too
def test_register_amended_after_its_reversal_stays_held(env):
    # booked, reversed, then cancelled and amended: a new register name for the same money
    register(env, "REG-1", "RCPT000001", docstatus=2)
    register(env, "REG-1-1", "RCPT000001", amended_from="REG-1")
    mrv = reversal(env, "REG-1", "RCPT000001")
    assert env.common.reversal_holding("REG-1-1").name == mrv
    assert env.common.reversed_registers(["REG-1-1", "REG-9"]) == ["REG-1-1"]
    assert set(env.common.reversed_registers()) == {"REG-1", "REG-1-1"}


def test_receipt_reversal_needs_a_receipt_and_a_live_state(env):
    register(env, "REG-1", "")
    reversal(env, "REG-X", "")  # a reversal with no receipt never holds a register without one
    reversal(env, "REG-Y", "RCPT000002", status="Failed")
    reversal(env, "REG-Z", "RCPT000003", docstatus=2)
    assert env.common.receipt_reversal("") is None and env.common.receipt_reversal(None) is None
    assert env.common.reversal_holding("REG-1") is None
    assert env.common.receipt_reversal("RCPT000002") is None and env.common.receipt_reversal("RCPT000003") is None
    assert env.common.reversed_registers(["REG-1"]) == []
    assert None not in env.common.reversed_registers() and "" not in env.common.reversed_registers()


def test_reversal_holding_uses_the_receipt_the_caller_has(env):
    # e.g. a register being inserted already submitted: nothing to read from the table yet
    reversal(env, "REG-OLD", "RCPT000004", status="Unknown")
    assert env.common.reversal_holding("REG-NEW", receipt="RCPT000004").status == "Unknown"
    assert env.common.reversal_holding(None, receipt="RCPT000004").status == "Unknown"


# ================================================================ item 1: the gateway refuses refunded money
def v1(env):
    return importlib.import_module("frappe_mpsa_payments.gateway.v1")


def verify_receipt(api, client, **kw):
    """The endpoint's body, past the HTTP envelope."""
    return inspect.unwrap(api.verify_receipt)(client, **kw)


def client(env):
    return env.frappe.get_cached_doc("Mpesa API Client", CLIENT)


def test_reversed_draft_register_is_claimed_and_never_attached(env):
    from frappe_mpsa_payments.gateway.auth import ApiError

    intent(env, status="Prompted")
    prompt(env, "STK-1", status="Failed")  # prompted: Manual Attach would book straight from the receipt
    register(env, "REG-1", "RCPT000001", billref="xyz")
    reversal(env, "REG-1", "RCPT000001")
    api = v1(env)
    found = api._find_receipt(client(env), "RCPT000001")
    assert api._claimed_by("RCPT000001", found) == "reversed"
    assert verify_receipt(api, client(env), receipt="RCPT000001")["claimed"] is True
    with pytest.raises(ApiError) as ctx:
        verify_receipt(api, client(env), receipt="RCPT000001", intent_id=MPI)
    assert (ctx.value.status, ctx.value.code) == (409, "receipt_already_used")
    assert "reversed" in ctx.value.message
    assert status(env) == ("Prompted", None) and env.queued == [] and booked(env) == []


def test_reversed_stk_receipt_is_claimed(env):
    # the STK receipt's register is gone (upstream cancels it); the reversal still carries the receipt
    prompt(env, "STK-9", intent_name="OTHER", reference_doctype="Sales Invoice", status="Completed",
           transaction_id="RCPT000009")
    reversal(env, "REG-GONE", "RCPT000009", status="Initiated")
    api = v1(env)
    found = api._find_receipt(client(env), "RCPT000009")
    assert found and found.get("express")
    assert api._claimed_by("RCPT000009", found) == "reversed"


def test_refresh_never_takes_a_reversed_register(env):
    intent(env)
    prompt(env, "STK-1", status="Failed")
    register(env, "REG-1", "RCPT000001")
    register(env, "REG-2-1", "RCPT000002", amended_from="REG-2")  # amended after its reversal
    reversal(env, "REG-1", "RCPT000001")
    reversal(env, "REG-2", "RCPT000002", status="Not Initiated")
    env.intents.refresh(MPI)
    assert status(env) == ("Failed", None)  # the failed prompt, not the refunded money
    assert booked(env) == []


def test_refresh_takes_the_next_register_when_one_is_reversed(env):
    intent(env)
    prompt(env, "STK-1", status="Failed")
    register(env, "REG-1", "RCPT000001")
    register(env, "REG-2", "RCPT000002")
    reversal(env, "REG-1", "RCPT000001")
    env.intents.refresh(MPI)
    assert status(env) == ("Paid", "RCPT000002")
    assert booked(env) == ["RCPT000002"]


def test_refresh_never_takes_a_reversed_stk_receipt(env):
    # the callback was lost, staff refunded the customer, then the late callback was verified
    intent(env)
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001")
    reversal(env, "REG-1", "RCPT000001", status="Reversed")
    env.intents.refresh(MPI)
    assert status(env) == ("Prompted", None) and env.queued == [] and booked(env) == []
    # and the customer may be prompted again: that prompt's money went back
    assert env.intents._paid_prompt_unsettled(MPI) is False


def test_mark_paid_refuses_a_receipt_reversed_meanwhile(env):
    from frappe_mpsa_payments.gateway.auth import ApiError

    doc = intent(env)
    register(env, "REG-1", "RCPT000001")
    reversal(env, "REG-1", "RCPT000001", status="Initiated")
    with pytest.raises(ApiError) as ctx:
        env.intents.mark_paid(doc, "RCPT000001", 1500, NOW, "JANE", register="REG-1", via="Manual Attach")
    assert ctx.value.status == 409
    assert status(env) == ("Prompted", None) and env.queued == [] and booked(env) == []
    assert (REVERSAL, "MRV-1") in env.frappe.db.locks, "locking read: waits for a reversal being submitted"


def test_book_direct_refuses_a_reversed_receipt_and_logs_it_once(env):
    doc = intent(env, status="Paid", receipt="RCPT000001")
    reversal(env, "REG-1", "RCPT000001")
    for _ in range(2):
        env.intents._book_direct(doc, "RCPT000001", 1500, NOW, "Astral 4134019")
    assert booked(env) == []
    rows = match_logs(env, "RCPT000001")
    assert [r["result"] for r in rows] == ["Error"] and "MRV-1" in rows[0]["details"]


def test_book_direct_leaves_a_receipt_its_submitted_register_booked(env):
    # a cashier put the draft on a POS invoice (submitted, no Payment Entry) before the gateway got it
    doc = intent(env, status="Paid", receipt="RCPT000001")
    register(env, "REG-1", "RCPT000001", docstatus=1, pos_invoice="POS-1")
    for _ in range(2):
        env.intents._book_direct(doc, "RCPT000001", 1500, NOW, "Astral 4134019")
    assert booked(env) == []
    assert (REGISTER, "REG-1") in env.frappe.db.locks, "locking read: sees a submit committed meanwhile"
    rows = match_logs(env, "RCPT000001")
    assert [r["result"] for r in rows] == ["Error"] and "POS Invoice POS-1" in rows[0]["details"]


def test_book_direct_still_books_a_draft_receipt(env):
    doc = intent(env, status="Paid", receipt="RCPT000001")
    register(env, "REG-1", "RCPT000001")
    register(env, "REG-0", "RCPT000001", docstatus=2)  # cancelled in the prompt's favour
    env.intents._book_direct(doc, "RCPT000001", 1500, NOW, "Astral 4134019")
    assert booked(env) == ["RCPT000001"]


def test_extra_receipt_reversed_by_receipt_is_skipped(env):
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    register(env, "REG-2-1", "PAYBILL002", amount=700)  # amended after its reversal
    reversal(env, "REG-2", "PAYBILL002")
    env.intents.book_extra_receipts(env.frappe.get_doc(INTENT, MPI))
    assert booked(env) == []


# ================================================================ item 4: C23 prompts left unreconciled
def on_payment_authorized(env, name=MPI):
    module = importlib.import_module(
        "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_payment_intent.mpesa_payment_intent"
    )
    doc = module.MpesaPaymentIntent()
    doc.name = name
    doc.on_payment_authorized("Completed")


def sync_extras(env, *names):
    env.monkeypatch.setattr(env.intents, "_paid_with_extra_money", lambda limit: list(names))
    env.intents.sync_open()


def test_given_up_prompt_is_not_rebooked_after_staff_cancel_its_entry(env):
    # STK-2's booking gave up (3 attempts) and stays unreconciled; sync_open booked its receipt,
    # then staff cancelled that entry (Safaricom reversed the double payment outside the app)
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002", booking_attempts=3)
    payment_entry(env, "RCPT000002", docstatus=2)
    sync_extras(env, MPI)
    sync_extras(env, MPI)
    assert booked(env) == []


def test_prompt_booked_by_sync_open_is_marked_reconciled(env):
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002", booking_attempts=3)
    sync_extras(env, MPI)
    assert booked(env) == ["RCPT000002"]
    assert env.frappe.db.table(EXPRESS)["STK-2"]["is_reconciled"] == 1
    # staff cancel the entry: it stays cancelled, also when another prompt of the intent is booked
    env.frappe.db.table("Payment Entry")["PE-RCPT000002"]["docstatus"] = 2
    prompt(env, "STK-3", status="Completed", transaction_id="RCPT000003")
    on_payment_authorized(env)
    sync_extras(env, MPI)
    assert booked(env) == ["RCPT000003"]


def test_failed_extra_booking_leaves_the_prompt_unreconciled(env):
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002", booking_attempts=3)
    env.fail_receipts.add("RCPT000002")
    sync_extras(env, MPI)
    assert env.frappe.db.table(EXPRESS)["STK-2"]["is_reconciled"] == 0
    assert [r["result"] for r in match_logs(env, "RCPT000002")] == ["Error"]


def test_stk_booking_that_cancelled_its_register_entry_never_commits_unbooked(env):
    # STK-1 is being booked: its booking cancelled the receipt's register (staff had booked it,
    # PE cancelled) and refresh marked the intent Paid with the newer STK-2. If STK-1's own
    # receipt then fails to book, logging and committing would leave RCPT000001 in no ledger.
    intent(env, status="Prompted")
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002",
           creation=NOW - datetime.timedelta(minutes=1))
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001")
    register(env, "REG-1", "RCPT000001", docstatus=2, payment_entry="PE-RCPT000001-2")
    payment_entry(env, "RCPT000001", docstatus=2)
    env.fail_receipts.add("RCPT000001")
    with pytest.raises(RuntimeError, match="RCPT000001"):
        on_payment_authorized(env)


def test_stk_booking_failure_with_nothing_cancelled_is_logged_for_sync_open(env):
    intent(env, status="Prompted")
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002",
           creation=NOW - datetime.timedelta(minutes=1))
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001")
    env.fail_receipts.add("RCPT000001")
    on_payment_authorized(env)  # Paid with RCPT000002 stands; sync_open retries RCPT000001
    assert status(env) == ("Paid", "RCPT000002")
    assert [r["result"] for r in match_logs(env, "RCPT000001")] == ["Error"]
    env.fail_receipts.clear()
    sync_extras(env, MPI)
    assert booked(env) == ["RCPT000001", "RCPT000002"]


# ================================================================ item 4: C25 STK receipts on any company record
def test_stk_receipt_on_the_clients_newer_record_for_a_shared_number_is_found(env):
    # legacy layout: two collecting records on one number; the client uses the newer STK one
    settings = env.frappe.db.table("Mpesa Settings")
    settings.clear()
    settings["4134019 C2B"] = {"name": "4134019 C2B", "business_shortcode": "4134019", "use_c2b": 1, "use_stk": 0,
                               "company": "Astral"}
    settings["4134019 Express"] = {"name": "4134019 Express", "business_shortcode": "4134019", "use_c2b": 0,
                                   "use_stk": 1, "company": "Astral"}
    settings["Other 555555"] = {"name": "Other 555555", "business_shortcode": "555555", "use_c2b": 1, "use_stk": 1,
                                "company": "Other Co"}
    env.frappe.db.table("Mpesa API Client")[CLIENT]["mpesa_settings"] = "4134019 Express"
    prompt(env, "STK-1", intent_name="SINV-1", reference_doctype="Sales Invoice", status="Completed",
           transaction_id="RCPT000001", settings="4134019 Express", transaction_date=NOW)
    prompt(env, "STK-2", intent_name="SINV-2", reference_doctype="Sales Invoice", status="Completed",
           transaction_id="RCPT000002", settings="Other 555555", transaction_date=NOW)
    api = v1(env)
    found = api._find_receipt(client(env), "RCPT000001")
    assert found and found["settings"] == "4134019 Express"
    assert api._find_receipt(client(env), "RCPT000002") is None, "another company's receipt stays hidden"


# ================================================================ item 3: POS quick pay
def pos(env):
    f = env.frappe
    f.has_permission = lambda *a, **kw: True
    f.db.count = lambda doctype, filters=None: len(f.db._rows(doctype, filters or {}))

    def sql(query, values=None, pluck=False, **kw):
        # the claimed-draft query of _unavailable_registers (its SQL runs in the bench test)
        assert "pe.reference_no = reg.transid" in query and "it.receipt = reg.transid" in query and pluck
        live = {r["reference_no"] for r in f.db.table("Payment Entry").values() if r["docstatus"] < 2}
        live |= {r["receipt"] for r in f.db.table(INTENT).values() if r.get("receipt")}
        return [r["name"] for r in f.db.table(REGISTER).values()
                if r["docstatus"] == 0 and r["businessshortcode"] in values["codes"] and r.get("transid") in live]

    f.db.sql = sql
    f.db.table("Mpesa C2B Payment Register URL")["URL-1"] = {
        "name": "URL-1", "company": "Astral", "mode_of_payment": "Mpesa-Astral 4134019", "register_status": "Success",
        "business_shortcode": "4134019"}
    f.db.table("Mode of Payment Account")["MOPA-1"] = {"name": "MOPA-1", "parent": "Mpesa-Astral 4134019",
                                                      "company": "Astral", "default_account": "Mpesa - A"}
    f.db.table("POS Invoice")["POS-1"] = {"name": "POS-1", "docstatus": 0, "company": "Astral", "payments": []}
    f.session.user = "Administrator"
    return importlib.import_module("frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_quick_pay")


def pos_registers(env):
    register(env, "REG-OK", "RCPTOK0001", billref="JANE")
    register(env, "REG-LINKED", "RCPTLINK01", billref="JANE", payment_entry="PE-X")
    register(env, "REG-GATEWAY", "RCPTGWAY01", billref="JANE")  # the gateway booked it straight from the receipt
    payment_entry(env, "RCPTGWAY01")
    register(env, "REG-UNDONE", "RCPTUNDO01", billref="JANE")  # its entry was cancelled: free again
    payment_entry(env, "RCPTUNDO01", docstatus=2)
    register(env, "REG-REV", "RCPTREV001", billref="JANE")
    reversal(env, "REG-REV", "RCPTREV001")
    register(env, "REG-AMENDED", "RCPTREV002", billref="JANE")  # amended after its reversal
    reversal(env, "REG-OLD", "RCPTREV002", status="Unknown")


def test_pos_list_hides_booked_and_reversed_drafts(env):
    quick_pay = pos(env)
    pos_registers(env)
    env.frappe.form_dict = fake_frappe._dict(company="Astral", search="JANE", mode_of_payment="Mpesa-Astral 4134019")
    out = quick_pay.get_mpesa_payments()
    assert sorted(p.name for p in out["payments"]) == ["REG-OK", "REG-UNDONE"]
    assert out["count"] == 2


@pytest.mark.parametrize("name,reason", [("REG-LINKED", "PE-X"), ("REG-GATEWAY", "PE-RCPTGWAY01-1"),
                                         ("REG-REV", "MRV-1"), ("REG-AMENDED", "MRV-2")])
def test_pos_refuses_booked_or_reversed_drafts(env, name, reason):
    quick_pay = pos(env)
    pos_registers(env)
    env.frappe.form_dict = fake_frappe._dict(doctype="POS Invoice", invoice_name="POS-1", customer="Jane",
                                             mpesa_payments=name)
    with pytest.raises(env.frappe.ValidationError, match=reason):
        quick_pay.process_mpesa()
    assert env.frappe.saved == [] and env.frappe.db.table(REGISTER)[name]["docstatus"] == 0
    assert env.frappe.db.table("POS Invoice")["POS-1"]["payments"] == []


def test_pos_still_takes_a_free_draft(env):
    quick_pay = pos(env)
    pos_registers(env)
    env.frappe.form_dict = fake_frappe._dict(doctype="POS Invoice", invoice_name="POS-1", customer="Jane",
                                             mpesa_payments="REG-OK,REG-UNDONE")
    out = quick_pay.process_mpesa()
    assert [p["reference"] for p in out["payments_added"]] == ["REG-OK", "REG-UNDONE"]
    invoice = env.frappe.db.table("POS Invoice")["POS-1"]
    assert [r["reference_no"] for r in invoice["payments"]] == ["REG-OK", "REG-UNDONE"]
    assert {env.frappe.db.table(REGISTER)[n]["docstatus"] for n in ("REG-OK", "REG-UNDONE")} == {1}


def test_pos_after_invoice_submission_refuses_a_booked_draft(env):
    quick_pay = pos(env)
    pos_registers(env)
    env.frappe.db.table("POS Invoice")["POS-1"]["docstatus"] = 1
    env.frappe.form_dict = fake_frappe._dict(doctype="POS Invoice", invoice_name="POS-1", customer="Jane",
                                             mpesa_payments="REG-GATEWAY")
    with pytest.raises(env.frappe.ValidationError, match="already booked"):
        quick_pay.update_mpesa_after_invoice_submission()
    assert env.frappe.db.table(REGISTER)["REG-GATEWAY"]["docstatus"] == 0


# ================================================================ repair: POS first, gateway second
def consumed_register(env, name="REG-1", receipt="RCPT000001", **kw):
    """A draft a cashier put on a POS invoice (process_mpesa): submitted, no Payment Entry."""
    return register(env, name, receipt, docstatus=1, pos_invoice="POS-9", **kw)


def assert_untouched(env, expected_status):
    assert status(env) == (expected_status, None)
    assert [q for q in env.queued if q[1] == "Paid"] == [], "no payment.succeeded webhook"
    assert booked(env) == []


def test_refresh_never_takes_a_register_a_pos_invoice_consumed(env):
    # an expired pay link without a customer: the matcher held the register, then a cashier took it
    intent(env, status="Expired", customer=None)
    consumed_register(env)
    for _ in range(2):
        env.intents.refresh(MPI)
    assert_untouched(env, "Expired")
    rows = match_logs(env, "RCPT000001")
    assert [r["result"] for r in rows] == ["Error"] and "POS Invoice POS-9" in rows[0]["details"]


def test_refresh_never_takes_a_pos_register_on_a_prompted_intent(env):
    intent(env, status="Prompted")
    prompt(env, "STK-1", status="Failed")
    consumed_register(env)
    env.intents.refresh(MPI)
    assert_untouched(env, "Failed")  # the failed prompt, not the money a POS sale used


def test_refresh_takes_the_next_register_when_one_is_on_an_invoice(env):
    intent(env, status="Expired", customer=None)
    consumed_register(env)
    register(env, "REG-2", "RCPT000002")
    env.intents.refresh(MPI)
    assert status(env) == ("Paid", "RCPT000002")


def test_refresh_still_takes_a_register_the_matcher_booked(env):
    # unprompted intent with a customer: the matcher submits the register with its Payment Entry
    intent(env, status="Created")
    register(env, "REG-1", "RCPT000001", docstatus=1, payment_entry="PE-RCPT000001-1")
    payment_entry(env, "RCPT000001")
    env.intents.refresh(MPI)
    assert status(env) == ("Paid", "RCPT000001")
    assert booked(env) == ["RCPT000001"], "booked once, by the matcher"


def test_refresh_never_takes_an_stk_receipt_a_pos_invoice_consumed(env):
    # the callback is in, its booking is not: a cashier put the receipt's register on a POS invoice
    intent(env)
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001")
    consumed_register(env, billref="something")
    env.intents.refresh(MPI)
    assert_untouched(env, "Prompted")
    assert [r["result"] for r in match_logs(env, "RCPT000001")] == ["Error"]


def test_page_status_never_marks_paid_with_a_pos_register(env):
    api = v1(env)
    env.monkeypatch.setattr(api, "rate_limit", lambda *a, **kw: None)
    intent(env, status="Expired", customer=None, pay_token="t" * 32, description=None, return_url=None,
           failure_reason=None, metadata=None)
    consumed_register(env)
    page = inspect.unwrap(api.page_status)(token="t" * 32)
    assert page["payment"]["status"] == "expired" and page["payment"]["receipt"] is None
    assert_untouched(env, "Expired")


def test_mark_paid_refuses_a_register_put_on_an_invoice_meanwhile(env):
    # refresh read REG-1 as a draft; the cashier's submit committed before the receipt lock
    from frappe_mpsa_payments.gateway.auth import ApiError

    doc = intent(env, status="Expired", customer=None)
    consumed_register(env)
    with pytest.raises(ApiError) as ctx:
        env.intents.mark_paid(doc, "RCPT000001", 1500, NOW, "JANE", register="REG-1")
    assert (ctx.value.status, ctx.value.code) == (409, "receipt_already_used") and "POS-9" in ctx.value.message
    assert_untouched(env, "Expired")
    assert (REGISTER, "REG-1") in env.frappe.db.locks, "locking read: waits for a POS submit in flight"


def record_locks(env):
    """Named locks and locking reads, in the order they are taken."""
    import contextlib

    events = []

    @contextlib.contextmanager
    def named_lock(key, timeout=10):
        events.append(("lock", key.split(":")[0]))
        yield

    env.monkeypatch.setattr(env.intents, "named_lock", named_lock)
    env.monkeypatch.setattr(env.common, "named_lock", named_lock)
    raw = env.frappe.db.get_value

    def get_value(doctype, filters, *a, **kw):
        if kw.get("for_update"):
            events.append(("row", doctype))
        return raw(doctype, filters, *a, **kw)

    env.monkeypatch.setattr(env.frappe.db, "get_value", get_value)
    return events


def first(events, event):
    return events.index(event)


def test_gateway_locks_the_register_rows_before_the_receipt_and_booking_locks(env):
    # the STK booking and POS quick pay take them in this order too (no lock wait cycle)
    intent(env)
    prompt(env, "STK-1", status="Failed")
    register(env, "REG-1", "RCPT000001")
    events = record_locks(env)
    env.intents.refresh(MPI)
    assert status(env) == ("Paid", "RCPT000001")
    assert events[0] == ("row", REGISTER)
    assert first(events, ("lock", "receipt")) < first(events, ("lock", "book"))


def test_extra_booking_locks_the_register_rows_before_the_booking_lock(env):
    doc = intent(env, status="Paid", receipt="RCPT000001")
    events = record_locks(env)
    env.intents._book_direct(doc, "RCPT000002", 700, NOW, "Astral 4134019")
    assert events[:2] == [("row", REGISTER), ("lock", "book")]
    assert booked(env) == ["RCPT000002"]


def test_pos_locks_the_register_row_before_the_receipt_and_booking_locks(env):
    quick_pay = pos(env)
    register(env, "REG-OK", "RCPTOK0001", billref="JANE")
    events = record_locks(env)
    env.frappe.form_dict = fake_frappe._dict(doctype="POS Invoice", invoice_name="POS-1", customer="Jane",
                                             mpesa_payments="REG-OK")
    quick_pay.process_mpesa()
    assert events[0] == ("row", REGISTER)
    assert first(events, ("lock", "receipt")) < first(events, ("row", INTENT)) < first(events, ("lock", "book"))


def test_pos_refuses_a_register_booked_after_the_list_loaded_it(env):
    quick_pay = pos(env)
    row = register(env, "REG-1", "RCPT000001", billref="JANE")
    stale = Doc(env.frappe, REGISTER, dict(row))  # loaded as a draft; the matcher booked it since
    row.update(docstatus=1, payment_entry="PE-RCPT000001-1")
    with pytest.raises(env.frappe.ValidationError, match="changed meanwhile"):
        quick_pay._refuse_if_unavailable(stale)


# ================================================================ repair: POS and receipts that paid an intent
def test_pos_hides_and_refuses_a_receipt_that_paid_an_intent(env):
    # intent paid by REG-1 but without a customer: the gateway left it a draft for staff
    quick_pay = pos(env)
    intent(env, status="Paid", receipt="RCPT000001", customer=None)
    register(env, "REG-1", "RCPT000001")
    register(env, "REG-2", "RCPT000002")
    env.frappe.form_dict = fake_frappe._dict(company="Astral", search="RCPT00000",
                                             mode_of_payment="Mpesa-Astral 4134019")
    assert [p.name for p in quick_pay.get_mpesa_payments()["payments"]] == ["REG-2"]

    env.frappe.form_dict = fake_frappe._dict(doctype="POS Invoice", invoice_name="POS-1", customer="Walkin",
                                             mpesa_payments="REG-1")
    with pytest.raises(env.frappe.ValidationError, match=MPI):
        quick_pay.process_mpesa()
    env.frappe.db.table("POS Invoice")["POS-1"]["docstatus"] = 1
    with pytest.raises(env.frappe.ValidationError, match=MPI):
        quick_pay.update_mpesa_after_invoice_submission()
    assert env.frappe.db.table(REGISTER)["REG-1"]["docstatus"] == 0
    assert env.frappe.db.table("POS Invoice")["POS-1"]["payments"] == []
    assert (INTENT, MPI) in env.frappe.db.locks, "locking read: sees an intent marked paid meanwhile"


# ================================================================ repair: STK booking failure of the intent's own receipt
def test_stk_booking_raises_when_the_intents_own_receipt_fails_to_book(env):
    # the STK booking removed RCPT000001's register already; logged and committed, it is in no ledger
    intent(env, status="Prompted")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001")
    env.fail_receipts.add("RCPT000001")
    with pytest.raises(RuntimeError, match="RCPT000001"):
        on_payment_authorized(env)
    assert not env.frappe.flags.get(env.intents.STK_BOOKING_FLAG), "flag cleared after the booking"


def test_refresh_outside_an_stk_booking_still_logs_a_booking_failure(env):
    intent(env, status="Prompted")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001")
    env.fail_receipts.add("RCPT000001")
    env.intents.refresh(MPI)  # sync_open / pay page: the Paid status stands, staff see the error
    assert status(env) == ("Paid", "RCPT000001")
    assert [r["result"] for r in match_logs(env) if r.get("reference_name") == MPI] == ["Error"]


# ================================================================ repair: C23, only the prompt being booked is pending
def test_only_the_prompt_the_stk_booking_names_ignores_a_cancelled_entry(env):
    # STK-Y gave up; staff booked its receipt by hand, then cancelled that entry (refunded outside
    # the app). STK-X of the same paid intent is being booked and names its receipt.
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    prompt(env, "STK-Y", status="Completed", transaction_id="RCPT000002", booking_attempts=3)
    payment_entry(env, "RCPT000002", docstatus=2)
    prompt(env, "STK-X", status="Completed", transaction_id="RCPT000003")
    payment_entry(env, "RCPT000003", docstatus=2)  # its register's entry, cancelled by this booking
    env.frappe.flags[env.intents.BOOKING_RECEIPT_FLAG] = "RCPT000003"
    on_payment_authorized(env)
    assert booked(env) == ["RCPT000003"], "the refunded RCPT000002 is not booked again"
