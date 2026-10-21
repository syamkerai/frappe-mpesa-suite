"""Payments API (gateway) fixes, on an in-memory frappe.

Covers: a webhook that cannot be queued never undoes or blocks a status change (or the
booking after it) and the delivery URL holds a long callback_url; a second payment on an
already-paid intent is booked on account, not marked reconciled unbooked; a resend is refused
while a paid prompt awaits confirmation; paybill money is booked with the shortcode that
received it, and money paid to another company's shortcode never settles the intent.
"""

import datetime
import importlib
import json
import types
from pathlib import Path

import pytest

import fake_frappe

NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)
INTENT = "Mpesa Payment Intent"
EXPRESS = "Mpesa Express Request"
REGISTER = "Mpesa C2B Payment Register"
SUITE = "M-Pesa Suite Settings"
MATCH_LOG = "Mpesa Match Log"
CLIENT = "Shop"
REPO = Path(__file__).resolve().parents[2]


class Doc(fake_frappe.FakeDoc):
    """FakeDoc plus the Document methods the gateway calls (rows are shared with the table)."""

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


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
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
        for name, code, company in (("Astral 4134019", "4134019", "Astral"), ("Astral 208911", "208911", "Astral"),
                                    ("Other 555555", "555555", "Other Co")):
            frappe.db.table("Mpesa Settings")[name] = {"name": name, "business_shortcode": code, "till_number": None,
                                                       "use_c2b": 1, "use_stk": 1, "company": company}
        frappe.db.table("Mpesa API Client")[CLIENT] = {"name": CLIENT, "mpesa_settings": "Astral 4134019",
                                                       "webhook_url": "https://shop.example.com/hook"}

        intents = importlib.import_module("frappe_mpsa_payments.gateway.intents")
        webhooks = importlib.import_module("frappe_mpsa_payments.gateway.webhooks")
        booked, queued = [], []

        def fake_book_direct(intent, receipt, amount, paid_at, settings_name, note=None):
            booked.append(fake_frappe._dict(intent=intent.name, receipt=receipt, amount=amount,
                                            settings=settings_name, note=note))
            if receipt in env_ns.fail_receipts:
                raise RuntimeError(f"cannot book {receipt}")
            frappe.db.table("Payment Entry")[f"PE-{receipt}"] = {"name": f"PE-{receipt}", "reference_no": receipt,
                                                                 "docstatus": 1}

        real_queue = webhooks.queue
        monkeypatch.setattr(intents, "_book_direct", fake_book_direct)
        monkeypatch.setattr(webhooks, "queue", lambda intent: queued.append((intent.name, intent.status)))
        env_ns = fake_frappe._dict(frappe=frappe, intents=intents, webhooks=webhooks, booked=booked, queued=queued,
                                   fail_receipts=set(), monkeypatch=monkeypatch, real_queue=real_queue)
        yield env_ns
    finally:
        fake_frappe.uninstall(saved)


def intent(env, name="MPI-26-10-000001", **kw):
    row = {"name": name, "client": CLIENT, "status": "Prompted", "amount": 1500, "customer": "Jane",
           "receipt": None, "expires_at": NOW + datetime.timedelta(minutes=10), "prompts_sent": 1,
           "phone": "254712345678", "reference_doctype": None, "reference_name": None, "order_ref": "WA-1",
           "creation": NOW - datetime.timedelta(minutes=5)}
    row.update(kw)
    env.frappe.db.table(INTENT)[name] = row
    return env.frappe.get_doc(INTENT, name)


def prompt(env, name, intent_name="MPI-26-10-000001", **kw):
    row = {"name": name, "docstatus": 1, "reference_doctype": INTENT, "reference_name": intent_name,
           "status": "In Progress", "transaction_id": None, "amount": 1500, "transaction_date": None,
           "settings": "Astral 4134019", "is_reconciled": 0, "creation": NOW - datetime.timedelta(minutes=5),
           "verification_status": None, "callback_payload": None}
    row.update(kw)
    env.frappe.db.table(EXPRESS)[name] = row
    return row


def register(env, name, receipt, shortcode="4134019", billref="MPI-26-10-000001", amount=1500, **kw):
    row = {"name": name, "docstatus": 0, "transid": receipt, "transamount": amount, "transtime": "20261018115500",
           "full_name": "JANE DOE", "businessshortcode": shortcode, "billrefnumber": billref, "payment_entry": None,
           "creation": NOW - datetime.timedelta(minutes=1)}
    row.update(kw)
    env.frappe.db.table(REGISTER)[name] = row
    return row


def match_logs(env, receipt=None):
    return [r for r in env.frappe.db.table(MATCH_LOG).values() if receipt is None or r.get("receipt") == receipt]


def on_payment_authorized(env, name="MPI-26-10-000001"):
    module = importlib.import_module(
        "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_payment_intent.mpesa_payment_intent"
    )
    doc = module.MpesaPaymentIntent()
    doc.name = name
    doc.on_payment_authorized("Completed")


# ---------------------------------------------------------------- C22: webhooks never block a status
def test_delivery_url_field_holds_a_long_callback_url():
    meta = json.loads((REPO / "frappe_mpsa_payments/frappe_mpsa_payments/doctype/mpesa_webhook_delivery/"
                               "mpesa_webhook_delivery.json").read_text(encoding="utf-8"))
    url = next(f for f in meta["fields"] if f["fieldname"] == "url")
    # Data is varchar(140) and refuses longer values; intents accept callback_url up to 1000 chars
    assert url["fieldtype"] in ("Small Text", "Text", "Long Text", "Code")


def test_webhook_queue_failure_keeps_the_status(env, monkeypatch):
    doc = intent(env, status="Prompted")

    def broken_queue(_intent):
        raise env.frappe.ValidationError("Value too big")

    monkeypatch.setattr(env.webhooks, "queue", broken_queue)
    env.intents._set_status(doc, "Expired")
    assert env.frappe.db.table(INTENT)[doc.name]["status"] == "Expired"
    assert any(doc.name in a[1] for a, _ in env.frappe.errors)


def test_webhook_queue_failure_does_not_block_paid_or_its_booking(env, monkeypatch):
    doc = intent(env)
    prompt(env, "STK-1")  # prompted intent: booked straight from the receipt

    def broken_queue(_intent):
        raise env.frappe.ValidationError("Value too big")

    monkeypatch.setattr(env.webhooks, "queue", broken_queue)
    env.intents.mark_paid(doc, "RCPT000001", 1500, NOW, "JANE", via="STK callback", settings="Astral 4134019")
    assert env.frappe.db.table(INTENT)[doc.name]["status"] == "Paid"
    assert [b.receipt for b in env.booked] == ["RCPT000001"]


def test_enqueue_failure_keeps_the_delivery_for_retry(env, monkeypatch):
    # v16 checks the queue size (and reaches Redis) inside enqueue, before the commit
    doc = intent(env, status="Paid", receipt="RCPT000001")

    def overloaded(method, **kw):
        raise env.frappe.ValidationError("Too many queued background jobs")

    monkeypatch.setattr(env.frappe, "enqueue", overloaded)
    name = env.real_queue(doc)
    delivery = env.frappe.db.table("Mpesa Webhook Delivery")[name]
    assert (delivery["status"], delivery["event"]) == ("Pending", "payment.succeeded")
    assert delivery["next_attempt_at"] and json.loads(delivery["payload"])["data"]["receipt"] == "RCPT000001"
    assert any(name in a[1] for a, _ in env.frappe.errors)


# ---------------------------------------------------------------- C23: second payment on a paid intent
def test_second_prompt_on_paid_intent_is_booked_on_account(env):
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002", amount=1500,
           transaction_date="2026-10-18 11:58:00", settings="Astral 208911")
    on_payment_authorized(env)
    assert [(b.receipt, b.amount, b.settings) for b in env.booked] == [("RCPT000002", 1500, "Astral 208911")]
    assert "extra payment" in env.booked[0].note and "RCPT000001" in env.booked[0].note


def test_prompt_after_paybill_payment_is_booked(env):
    intent(env, status="Paid", receipt="PAYBILL001")
    register(env, "REG-1", "PAYBILL001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000002")
    on_payment_authorized(env)
    assert [b.receipt for b in env.booked] == ["RCPT000002"]


def payment_entry(env, receipt, docstatus=1):
    env.frappe.db.table("Payment Entry")[f"PE-{receipt}-{docstatus}"] = {
        "name": f"PE-{receipt}-{docstatus}", "reference_no": receipt, "docstatus": docstatus}


def test_extra_receipts_skip_what_is_booked_refunded_or_taken(env):
    intent(env, status="Paid", receipt="RCPT000001")
    intent(env, name="MPI-26-10-000002", status="Paid", receipt="TAKEN00001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001")  # its own receipt
    prompt(env, "STK-2", status="Completed", transaction_id="BOOKED0001", is_reconciled=1)
    prompt(env, "STK-3", status="Completed", transaction_id="UNDONE0001", is_reconciled=1)  # PE cancelled by hand
    prompt(env, "STK-4", status="Completed", transaction_id="REVERSED01")
    prompt(env, "STK-5", status="Failed")
    register(env, "REG-1", "TAKEN00001")
    register(env, "REG-2", "OTHERCO001", shortcode="555555")  # another company's paybill
    register(env, "REG-3", "LINKED0001", payment_entry="PE-X")
    register(env, "REG-4", "PAYBILL002", shortcode="208911", amount=700)
    register(env, "REG-5", "UNDONE0002")
    payment_entry(env, "BOOKED0001")
    payment_entry(env, "UNDONE0001", docstatus=2)
    payment_entry(env, "UNDONE0002", docstatus=2)
    env.frappe.db.table("Mpesa Reversal Request")["REV-1"] = {"name": "REV-1", "transaction_id": "REVERSED01",
                                                               "docstatus": 1, "status": "Reversed"}
    env.intents.book_extra_receipts(env.frappe.get_doc(INTENT, "MPI-26-10-000001"))
    assert [(b.receipt, b.amount, b.settings) for b in env.booked] == [("PAYBILL002", 700, "Astral 208911")]


def test_prompt_being_booked_ignores_the_cancelled_entry_of_its_neutralised_register(env):
    # staff booked the C2B register while the prompt was being verified; the STK booking then
    # cancelled that register (and its Payment Entry) in the prompt's favour
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002")
    payment_entry(env, "RCPT000002", docstatus=2)
    on_payment_authorized(env)
    assert [b.receipt for b in env.booked] == ["RCPT000002"]


def test_register_matched_to_an_order_is_left_to_the_order_booking(env):
    # paid by paybill against the draft order before the first prompt: the matcher stamped the
    # order on it, and book_for_submitted_orders books it once the order is submitted
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    register(env, "REG-1", "PAYBILL700", amount=700, matched_reference_doctype="Sales Order",
             matched_reference_name="SO-1")
    env.intents.book_extra_receipts(env.frappe.get_doc(INTENT, "MPI-26-10-000001"))
    assert env.booked == []


def test_registers_of_unprompted_intents_are_left_to_the_matcher(env):
    intent(env, status="Paid", receipt="PAYBILL001")
    register(env, "REG-1", "PAYBILL001")
    register(env, "REG-2", "PAYBILL002")
    env.intents.book_extra_receipts(env.frappe.get_doc(INTENT, "MPI-26-10-000001"))
    assert env.booked == []


def test_extra_receipts_wait_for_paid_status_and_matching(env):
    intent(env, status="Prompted")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000002")
    env.intents.book_extra_receipts(env.frappe.get_doc(INTENT, "MPI-26-10-000001"))
    env.frappe.db.table(INTENT)["MPI-26-10-000001"].update(status="Paid", receipt="RCPT000001")
    env.frappe.db.table(SUITE)[SUITE]["enable_matching"] = 0
    env.intents.book_extra_receipts(env.frappe.get_doc(INTENT, "MPI-26-10-000001"))
    assert env.booked == []


def test_extra_receipt_without_customer_is_logged_once(env):
    intent(env, status="Paid", receipt="RCPT000001", customer=None)
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002")
    for _ in range(2):
        env.intents.book_extra_receipts(env.frappe.get_doc(INTENT, "MPI-26-10-000001"))
    assert env.booked == []
    rows = match_logs(env, "RCPT000002")
    assert len(rows) == 1 and rows[0]["result"] == "Matched" and "book it manually" in rows[0]["details"]


def test_extra_receipt_failure_on_already_paid_intent_is_raised_for_retry(env):
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002")
    env.fail_receipts.add("RCPT000002")
    # stk_verify.book rolls back and leaves the prompt unreconciled, so its sweep retries
    with pytest.raises(RuntimeError, match="RCPT000002"):
        on_payment_authorized(env)


def test_failing_extra_register_never_blocks_the_prompt_booking(env):
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002")
    register(env, "REG-1", "PAYBILL002", amount=200)
    env.fail_receipts.add("PAYBILL002")
    on_payment_authorized(env)  # the register is not this prompt's money: logged, not raised
    assert {b.receipt for b in env.booked} == {"RCPT000002", "PAYBILL002"}
    assert "PE-RCPT000002" in env.frappe.db.table("Payment Entry")
    assert [r["result"] for r in match_logs(env, "PAYBILL002")] == ["Error"]


def test_extra_receipt_failure_never_undoes_paid_set_in_the_same_callback(env):
    intent(env, status="Prompted")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001")
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002")
    env.fail_receipts.add("RCPT000002")
    on_payment_authorized(env)  # must not raise: refresh just marked the intent Paid with RCPT000001
    row = env.frappe.db.table(INTENT)["MPI-26-10-000001"]
    assert (row["status"], row["receipt"]) == ("Paid", "RCPT000001")
    assert [b.receipt for b in env.booked] == ["RCPT000001", "RCPT000002"]
    assert [r["result"] for r in match_logs(env, "RCPT000002")] == ["Error"]


def test_refresh_failure_on_unpaid_intent_is_raised_for_retry(env, monkeypatch):
    # another booking marked the intent Paid after this transaction began: the save sees a newer
    # version and raises; swallowing it would mark this prompt reconciled with nothing booked
    intent(env, status="Prompted")
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002")

    def stale(name):
        raise env.frappe.ValidationError("Document has been modified after you have opened it")

    monkeypatch.setattr(env.intents, "refresh", stale)
    with pytest.raises(env.frappe.ValidationError, match="modified"):
        on_payment_authorized(env)
    assert env.booked == []


def test_refresh_failure_on_paid_intent_is_logged_and_extras_still_booked(env, monkeypatch):
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002")

    def broken(name):
        raise RuntimeError("refresh broke")

    monkeypatch.setattr(env.intents, "refresh", broken)
    on_payment_authorized(env)
    assert [b.receipt for b in env.booked] == ["RCPT000002"]
    assert env.frappe.errors


def test_failing_reconciled_prompt_never_blocks_the_prompt_being_booked(env):
    intent(env, status="Paid", receipt="RCPT000000")
    # an earlier booking logged this one's failure and marked its prompt reconciled
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002")
    env.fail_receipts.add("RCPT000001")
    on_payment_authorized(env)
    assert {b.receipt for b in env.booked} == {"RCPT000001", "RCPT000002"}
    assert "PE-RCPT000002" in env.frappe.db.table("Payment Entry")
    assert [r["result"] for r in match_logs(env, "RCPT000001")] == ["Error"]


def sync_extras(env, monkeypatch, *names):
    """sync_open with its candidate query (SQL, covered by the bench test) answering ``names``."""
    monkeypatch.setattr(env.intents, "_paid_with_extra_money", lambda limit: list(names))
    env.intents.sync_open()


def test_sync_open_books_a_paybill_payment_on_a_paid_prompted_intent(env, monkeypatch):
    # paid by STK; the customer then also paid the paybill with the intent as account number.
    # The matcher holds that register for the gateway and refresh stops at Paid.
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    register(env, "REG-3", "PAYBILL003", shortcode="208911")
    sync_extras(env, monkeypatch, "MPI-26-10-000001")
    assert [(b.receipt, b.settings) for b in env.booked] == [("PAYBILL003", "Astral 208911")]
    sync_extras(env, monkeypatch, "MPI-26-10-000001")
    assert len(env.booked) == 1, "booked once"


def test_sync_open_retries_a_prompt_receipt_whose_booking_failed(env, monkeypatch):
    # two prompts completed before either was booked: STK-1's booking marked the intent Paid
    # with the newer receipt, logged its own receipt's failure and was marked reconciled
    intent(env, status="Paid", receipt="RCPT000002")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    prompt(env, "STK-2", status="Completed", transaction_id="RCPT000002", is_reconciled=1)
    env.fail_receipts.add("RCPT000001")
    sync_extras(env, monkeypatch, "MPI-26-10-000001")
    assert [r["result"] for r in match_logs(env, "RCPT000001")] == ["Error"]
    env.fail_receipts.clear()
    sync_extras(env, monkeypatch, "MPI-26-10-000001")
    assert [b.receipt for b in env.booked] == ["RCPT000001", "RCPT000001"]
    assert "PE-RCPT000001" in env.frappe.db.table("Payment Entry")


def test_sync_open_extra_booking_failure_is_contained(env, monkeypatch):
    intent(env, status="Paid", receipt="RCPT000001")
    intent(env, name="MPI-26-10-000002", status="Paid", receipt="RCPT000009")
    register(env, "REG-3", "PAYBILL003", billref="MPI-26-10-000002")
    prompt(env, "STK-9", intent_name="MPI-26-10-000002", status="Completed", transaction_id="RCPT000009")

    def broken(intent, raise_errors=False):
        if intent.name == "MPI-26-10-000001":
            raise RuntimeError("broken intent")
        return real(intent, raise_errors)

    real = env.intents.book_extra_receipts
    monkeypatch.setattr(env.intents, "book_extra_receipts", broken)
    sync_extras(env, monkeypatch, "MPI-26-10-000001", "MPI-26-10-000002")
    assert [b.receipt for b in env.booked] == ["PAYBILL003"]
    assert any("MPI-26-10-000001" in a[1] for a, _ in env.frappe.errors)


def test_sync_open_books_no_extras_with_matching_off(env, monkeypatch):
    env.frappe.db.table(SUITE)[SUITE]["enable_matching"] = 0

    def must_not_query(limit):
        raise AssertionError("queried with matching off")

    monkeypatch.setattr(env.intents, "_paid_with_extra_money", must_not_query)
    env.intents.sync_open()


def test_resend_refused_while_a_paid_prompt_awaits_confirmation(env, monkeypatch):
    from frappe_mpsa_payments.gateway.auth import ApiError

    monkeypatch.setattr(env.intents, "refresh", lambda name: None)
    paid_callback = {"ResultCode": 0, "CallbackMetadata": {"Item": [{"Name": "MpesaReceiptNumber", "Value": "R1"}]}}
    cancelled_callback = {"ResultCode": 1032, "ResultDesc": "Request cancelled by user"}
    cases = [
        ({"status": "Completed"}, True),  # status query said paid; the receipt is still on its way
        ({"verification_status": "Pending", "callback_payload": json.dumps(paid_callback)}, True),
        ({"verification_status": "Pending", "callback_payload": json.dumps(cancelled_callback)}, False),
        ({"status": "Failed"}, False),
    ]
    for kw, refused in cases:
        env.frappe.db.table(EXPRESS).clear()
        doc = intent(env)
        prompt(env, "STK-1", **kw)  # older than the 90 s resend wait
        try:
            env.intents.send_prompt(doc)
        except ApiError as e:
            outcome = e.code
        except Exception:
            outcome = "sent"  # went on to build a new prompt (the fake has no submit)
        else:
            outcome = "sent"
        assert (outcome == "prompt_pending") == refused, kw


# ---------------------------------------------------------------- C25: paybill money and its shortcode
def test_paybill_receipt_books_with_the_receiving_shortcode(env):
    intent(env)
    prompt(env, "STK-1", status="Failed")  # prompted: booked straight from the receipt
    register(env, "REG-1", "PAYBILL001", shortcode="208911")
    env.intents.refresh("MPI-26-10-000001")
    assert env.frappe.db.table(INTENT)["MPI-26-10-000001"]["status"] == "Paid"
    assert [(b.receipt, b.settings) for b in env.booked] == [("PAYBILL001", "Astral 208911")]


def test_paybill_receipt_on_another_companys_shortcode_does_not_settle(env):
    intent(env)
    prompt(env, "STK-1", status="Failed")
    register(env, "REG-1", "OTHERCO001", shortcode="555555")
    env.intents.refresh("MPI-26-10-000001")
    row = env.frappe.db.table(INTENT)["MPI-26-10-000001"]
    assert (row["status"], row["receipt"]) == ("Failed", None)  # the failed prompt, not the other company's money
    assert env.booked == []
    # the matcher holds it for the gateway ("booked by the payment intent"): staff must hear otherwise
    env.intents.refresh("MPI-26-10-000001")
    rows = match_logs(env, "OTHERCO001")
    assert [(r["result"], r["register"]) for r in rows] == [("Unmatched", "REG-1")]
    assert "555555" in rows[0]["details"] and "by hand" in rows[0]["details"]


def test_other_companys_paybill_on_a_paid_intent_is_flagged_once(env):
    intent(env, status="Paid", receipt="RCPT000001")
    prompt(env, "STK-1", status="Completed", transaction_id="RCPT000001", is_reconciled=1)
    register(env, "REG-2", "OTHERCO002", shortcode="555555")
    for _ in range(2):
        env.intents.book_extra_receipts(env.frappe.get_doc(INTENT, "MPI-26-10-000001"))
    assert env.booked == []
    assert [r["result"] for r in match_logs(env, "OTHERCO002")] == ["Unmatched"]


def test_other_companys_paybill_on_an_unprompted_intent_is_left_to_the_matcher(env):
    intent(env, status="Created")
    register(env, "REG-1", "OTHERCO001", shortcode="555555")
    env.intents.refresh("MPI-26-10-000001")
    assert match_logs(env) == []  # the matcher routes it to the intent's document and logs that


def test_one_companys_records_on_a_number_book_with_the_collecting_one(env):
    # the client's record collects; a newer payout-only record of the same company shares the number
    env.frappe.db.table("Mpesa Settings")["Astral 4134019 B2C"] = {
        "name": "Astral 4134019 B2C", "business_shortcode": "4134019", "use_c2b": 0, "use_stk": 0, "use_b2c": 1,
        "company": "Astral"}
    intent(env)
    prompt(env, "STK-1", status="Failed")
    register(env, "REG-1", "PAYBILL001", shortcode="4134019")
    env.intents.refresh("MPI-26-10-000001")
    assert [(b.receipt, b.settings) for b in env.booked] == [("PAYBILL001", "Astral 4134019")]


def test_client_shortcodes_choose_like_settings_for_shortcode(env):
    from frappe_mpsa_payments.money_in.common import settings_for_shortcode

    settings = env.frappe.db.table("Mpesa Settings")
    rows = dict(settings)
    settings.clear()  # the payout-only record is older here
    settings["Astral Payouts"] = {"name": "Astral Payouts", "business_shortcode": "208911", "use_c2b": 0,
                                  "use_stk": 0, "use_b2c": 1, "company": "Astral"}
    settings.update(rows)
    client = env.frappe.get_cached_doc("Mpesa API Client", CLIENT)
    codes = env.intents.client_shortcodes(client)
    assert codes["208911"] == settings_for_shortcode("208911") == "Astral 208911"
    assert codes["4134019"] == "Astral 4134019" and "555555" not in codes


def test_number_shared_by_companies_books_with_the_clients_company_record(env):
    settings = env.frappe.db.table("Mpesa Settings")
    rows = dict(settings)
    settings.clear()  # the other company's record is older, so it is the number's default
    settings["Other 777777"] = {"name": "Other 777777", "business_shortcode": "777777", "use_c2b": 1,
                                "company": "Other Co"}
    settings.update(rows)
    settings["Astral 777777"] = {"name": "Astral 777777", "business_shortcode": "777777", "use_c2b": 1,
                                 "company": "Astral"}
    intent(env)
    prompt(env, "STK-1", status="Failed")
    register(env, "REG-1", "SHARED0001", shortcode="777777")
    env.intents.refresh("MPI-26-10-000001")
    assert [(b.receipt, b.settings) for b in env.booked] == [("SHARED0001", "Astral 777777")]


def test_register_shortcode_not_checked_when_company_is_unknown(env):
    env.frappe.get_meta = lambda dt: types.SimpleNamespace(has_field=lambda f: False)
    doc = intent(env)
    register(env, "REG-1", "OTHERCO001", shortcode="555555")
    assert env.intents._register_for(doc) == "REG-1"
