"""STK prompts paid on draft (prepaid) Sales Orders, on an in-memory frappe.

ERPNext books no payment on a draft order ("Sales Order must be submitted"). Before this fix a
paid prompt on a draft order failed its three booking attempts and stopped: the order stayed
in its approval workflow and the money stayed on an unreconciled statement line (Astral,
9 Nov 2026: MEXP-26-11-000043/44/48/49). Now the prompt counts toward the order like a C2B
receipt: once the order is paid in full it is released through its Workflow (as the approver
user) and then the prompt is booked. Waiting is not a booking attempt.

A prompt whose Payment Request a re-prompt superseded (cancelled) while the customer paid is
booked on the request that replaced it (MEXP-26-11-000046). When Safaricom also sends a C2B
register for a prompt's receipt, the prompt books it and the register is not counted again.
"""

import datetime
import importlib
import sys
import types

import pytest

import fake_frappe
from test_fix2_stk2 import DB, REPO, ExpressDoc, Meta

NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)
EXPRESS = "Mpesa Express Request"
REGISTER = "Mpesa C2B Payment Register"
SUITE = "M-Pesa Suite Settings"


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.db = DB(frappe)
        frappe.utils.add_to_date = lambda d, **kw: d + datetime.timedelta(**kw)
        frappe.utils.strip_html = lambda s: s
        frappe.metas = {EXPRESS: Meta({"verification_status", "timestamp"})}
        frappe.get_meta = lambda doctype: frappe.metas.get(doctype, Meta(()))
        frappe.db.table("DocType")["Mpesa Match Log"] = {"name": "Mpesa Match Log"}

        def get_cached_doc(doctype, name=None, **kw):
            row = frappe.db.table(doctype).get(name or doctype)
            if row is None:
                raise frappe.DoesNotExistError(doctype)
            return fake_frappe.FakeDoc(frappe, doctype, row)

        frappe.get_cached_doc = get_cached_doc
        frappe.db.table(SUITE)[SUITE] = {
            "name": SUITE, "enable_sales_order_payments": 1, "amount_tolerance": 1,
            "sales_order_details_field": "custom_payment_details", "matching_lookback_days": 45,
        }
        enc = types.ModuleType("mpsa_suite.utils.encoding_initiator_password")
        enc.generate_security_credential = lambda *a, **kw: ""
        monkeypatch.setitem(sys.modules, enc.__name__, enc)

        frappe.calls = []
        plain = frappe.get_doc
        frappe.get_doc = lambda dt, name=None, **kw: (
            Prompt(frappe, dt, frappe.db.table(dt)[name]) if dt == EXPRESS else plain(dt, name, **kw)
        )
        mods = fake_frappe._dict(
            frappe=frappe,
            monkeypatch=monkeypatch,
            stk_verify=importlib.import_module("mpsa_suite.money_in.stk_verify"),
            sales_order=importlib.import_module("mpsa_suite.money_in.sales_order"),
            booking=importlib.import_module("mpsa_suite.money_in.booking"),
            logged=[],
            released=[],
        )
        mods.refuse = False

        def release_order(so_name):
            mods.released.append(so_name)
            if not mods.refuse:
                frappe.db.table("Sales Order")[so_name]["docstatus"] = 1

        monkeypatch.setattr(mods.sales_order, "release_order", release_order)
        monkeypatch.setattr(mods.sales_order, "log_match", lambda *a, **kw: mods.logged.append((a, kw)))
        monkeypatch.setattr(mods.stk_verify, "log_match", lambda *a, **kw: mods.logged.append((a, kw)))
        monkeypatch.setattr(mods.booking, "book_register", lambda name, method=None: frappe.calls.append(("register", name)))
        monkeypatch.setattr(mods.booking, "stamp", lambda reg, match: None)
        yield mods
    finally:
        fake_frappe.uninstall(saved)


class Prompt(ExpressDoc):
    def book_completed_payment(self):
        self._frappe.calls.append(("book", self.name, self.reference_name))
        self._frappe.db.table(EXPRESS)[self.name]["is_reconciled"] = 1

    def add_comment(self, kind, text):
        self._frappe.comments.append((EXPRESS, self.name, text))


class SalesOrderMeta:
    @staticmethod
    def has_field(fieldname):
        return fieldname in ("custom_payment_details", "custom_payment_date_time_")


def order(env, name="SO-1", total=10000.0, docstatus=0):
    env.frappe.db.table("Sales Order")[name] = {
        "name": name, "docstatus": docstatus, "status": "Draft" if docstatus == 0 else "To Deliver and Bill",
        "customer": "Cash Cust - Isinya", "grand_total": total, "rounded_total": total, "per_billed": 0,
        "advance_paid": 0, "custom_payment_details": None, "meta": SalesOrderMeta(), "reload": lambda: None,
    }


def request(env, name="PR-1", so="SO-1", docstatus=1, status="Requested", creation=None):
    env.frappe.db.table("Payment Request")[name] = {
        "name": name, "reference_doctype": "Sales Order", "reference_name": so, "docstatus": docstatus,
        "status": status, "payment_request_type": "Inward", "payment_gateway": "Mpesa-208911",
        "payment_gateway_account": "Mpesa-208911 - KES", "creation": creation or NOW - datetime.timedelta(minutes=5),
    }


def paid_prompt(env, name="MEXP-1", pr="PR-1", amount=10000.0, receipt="UK9B01MY3N", **kw):
    row = {"name": name, "docstatus": 1, "status": "Completed", "verification_status": "Verified",
           "transaction_id": receipt, "transaction_date": NOW - datetime.timedelta(minutes=1), "is_reconciled": 0,
           "booking_attempts": 0, "amount": amount, "reference_doctype": "Payment Request", "reference_name": pr,
           "creation": NOW - datetime.timedelta(minutes=2), "timestamp": None}
    row.update(kw)
    env.frappe.db.table(EXPRESS)[name] = row
    return row


def c2b(env, name="MPC2B-1", amount=5000.0, receipt="UK9C2B0001", ref="PR-1"):
    env.frappe.db.table(REGISTER)[name] = {
        "name": name, "docstatus": 0, "transid": receipt, "transamount": amount, "billrefnumber": ref,
        "matched_reference_doctype": "Sales Order", "matched_reference_name": "SO-1", "payment_entry": None,
        "full_name": "PAYER", "transtime": "20261018115900", "customer": "Cash Cust - Isinya",
    }


def so_row(env, name="SO-1"):
    return env.frappe.db.table("Sales Order")[name]


def prompt_row(env, name="MEXP-1"):
    return env.frappe.db.table(EXPRESS)[name]


# ---------------------------------------------------------------- full payment on a draft order
def test_paid_prompt_on_a_draft_order_releases_the_order_then_books_the_prompt(env):
    order(env)
    request(env)
    paid_prompt(env)
    assert env.stk_verify.book("MEXP-1") is True
    assert env.released == ["SO-1"]
    assert env.frappe.calls == [("neutralise", "MEXP-1"), ("book", "MEXP-1", "PR-1")]
    assert so_row(env)["docstatus"] == 1
    assert prompt_row(env)["booking_attempts"] == 1, "only the real booking is an attempt"
    assert "M-Pesa UK9B01MY3N KES 10,000.00 STK prompt MEXP-1" in so_row(env)["custom_payment_details"]


def test_part_paid_draft_order_waits_and_spends_no_attempt(env):
    order(env)
    request(env)
    paid_prompt(env, amount=4000.0)
    for _ in range(5):  # every sweep while the order waits
        assert env.stk_verify.book("MEXP-1") is False
    assert env.released == [] and env.frappe.calls == []
    assert prompt_row(env)["booking_attempts"] == 0
    assert so_row(env)["docstatus"] == 0


def test_prompt_and_paybill_receipt_together_cover_the_order(env):
    order(env)
    request(env)
    paid_prompt(env, amount=6000.0)
    c2b(env, amount=4000.0)
    assert env.stk_verify.book("MEXP-1") is True
    assert env.released == ["SO-1"]
    assert env.frappe.calls == [("register", "MPC2B-1"), ("neutralise", "MEXP-1"), ("book", "MEXP-1", "PR-1")]


def test_refused_release_books_nothing_and_spends_no_attempt(env):
    order(env)
    request(env)
    paid_prompt(env)
    env.refuse = True  # credit limit, SO Rules, a missing role ...
    assert env.stk_verify.book("MEXP-1") is False
    assert env.released == ["SO-1"] and env.frappe.calls == []
    assert prompt_row(env)["booking_attempts"] == 0


def test_order_approved_by_hand_books_on_the_next_sweep(env):
    order(env, docstatus=1)  # staff approved it while the prompt waited
    request(env)
    paid_prompt(env)
    assert env.stk_verify.book("MEXP-1") is True
    assert env.released == []
    assert env.frappe.calls == [("neutralise", "MEXP-1"), ("book", "MEXP-1", "PR-1")]


def test_switch_off_keeps_the_old_behaviour(env):
    env.frappe.db.table(SUITE)[SUITE]["enable_sales_order_payments"] = 0
    order(env)
    request(env)
    paid_prompt(env)
    env.stk_verify.book("MEXP-1")
    assert env.released == []
    assert prompt_row(env)["booking_attempts"] == 1, "tried (and in ERPNext refused) as before"


# ---------------------------------------------------------------- what counts toward the order
def test_unconfirmed_or_booked_prompts_do_not_count(env):
    order(env)
    request(env)
    paid_prompt(env, "MEXP-U", amount=10000.0, receipt="UNVERIFIED", verification_status="Mismatch")
    paid_prompt(env, "MEXP-B", amount=10000.0, receipt="BOOKEDELSE")
    env.frappe.db.table("Payment Entry")["PE-1"] = {"name": "PE-1", "reference_no": "BOOKEDELSE", "docstatus": 1}
    paid_prompt(env, "MEXP-F", amount=10000.0, receipt=None, status="Failed")
    assert env.sales_order.paid_prompts_for("SO-1") == []
    env.sales_order.process_order("SO-1")
    assert env.released == []


def test_c2b_register_for_a_prompts_receipt_is_not_counted_twice(env):
    """Safaricom sometimes also sends a C2B confirmation for a paybill STK payment: one receipt,
    two records. The prompt books it; the register would cancel itself as a duplicate."""
    order(env, total=56000.0)
    request(env)
    paid_prompt(env, amount=56000.0, receipt="UK9AD1LR57")
    c2b(env, amount=56000.0, receipt="UK9AD1LR57")
    assert [r.name for r in env.sales_order.receipts_for("SO-1")] == []
    env.sales_order.process_order("SO-1")
    assert env.released == ["SO-1"]
    assert ("register", "MPC2B-1") not in env.frappe.calls
    assert ("book", "MEXP-1", "PR-1") in env.frappe.calls


def test_scheduler_finds_orders_waiting_on_paid_prompts(env):
    order(env)
    request(env)
    paid_prompt(env)
    order(env, "SO-2", docstatus=1)
    request(env, "PR-2", so="SO-2")
    paid_prompt(env, "MEXP-2", pr="PR-2", receipt="UK9GAVEUP1", booking_attempts=3)  # submitted, gave up: staff
    order(env, "SO-3")
    request(env, "PR-3", so="SO-3")
    # spent its attempts on the draft under the old code (MEXP-26-11-000044): still taken
    paid_prompt(env, "MEXP-3", pr="PR-3", receipt="UK9R81I0PF", booking_attempts=3)
    assert env.sales_order.orders_with_paid_prompts() == ["SO-1", "SO-3"]
    env.sales_order.process_pending_orders()
    assert env.released == ["SO-1", "SO-3"]
    assert ("book", "MEXP-1", "PR-1") in env.frappe.calls and ("book", "MEXP-3", "PR-3") in env.frappe.calls
    assert ("book", "MEXP-2", "PR-2") not in env.frappe.calls


def test_invoiced_order_books_its_paid_prompt(env):
    order(env, docstatus=1)
    so_row(env)["per_billed"] = 100
    env.frappe.db.table("Sales Order Item")  # no rows needed: is_fully_billed reads per_billed
    request(env)
    paid_prompt(env)
    env.monkeypatch.setattr(env.sales_order, "is_fully_billed", lambda so: True)
    env.sales_order.process_order("SO-1")
    assert env.frappe.calls == [("neutralise", "MEXP-1"), ("book", "MEXP-1", "PR-1")]


def test_closed_order_leaves_its_paid_prompt_to_staff(env):
    order(env, docstatus=1)
    so_row(env)["status"] = "Closed"
    request(env)
    paid_prompt(env)
    env.sales_order.process_order("SO-1")
    assert env.frappe.calls == []
    [(args, kw)] = env.logged
    assert args[0] == "Error" and "book it by hand" in kw["details"]


# ---------------------------------------------------------------- the release's patch
def test_patch_gives_back_attempts_erpnext_refused_on_a_draft_order(env):
    patch = importlib.import_module("mpsa_suite.mpsa_suite.patches.retry_stk_prompts_refused_on_draft_orders")
    env.frappe.db.columns = {(EXPRESS, "booking_attempts"), (EXPRESS, "verification_status")}
    order(env, docstatus=1)  # approved by hand since (SAL-ORD-2026-07004)
    request(env)
    env.frappe.db.table("Payment Request")["PR-SI"] = {"name": "PR-SI", "reference_doctype": "Sales Invoice"}
    errors = env.frappe.db.table("Error Log")

    clock = iter(range(1000))

    def failed(prompt, text, n=3):
        for _ in range(n):
            i = next(clock)
            errors[f"{prompt}-{i}"] = {"name": f"{prompt}-{i}", "method": f"M-Pesa STK booking failed for {prompt}",
                                       "error": f"Traceback ...\nValidationError: {text}\n",
                                       "creation": NOW + datetime.timedelta(seconds=i)}

    paid_prompt(env, "MEXP-LATER", receipt="UK9LATER01", booking_attempts=3)  # draft refusal, then a real one
    failed("MEXP-LATER", "must be submitted", 2)
    failed("MEXP-LATER", "Payment Entry Creation Error: has already been fully paid", 1)
    paid_prompt(env, "MEXP-EARLIER", receipt="UK9EARLY01", booking_attempts=3)  # a real one, then draft refusals
    failed("MEXP-EARLIER", "Payment Entry Creation Error: has already been fully paid", 1)
    failed("MEXP-EARLIER", "must be submitted", 2)

    paid_prompt(env, "MEXP-DRAFT", receipt="UK9B01MY3N", booking_attempts=3)
    failed("MEXP-DRAFT", "Sales Order SAL-ORD-2026-07004 must be submitted")
    paid_prompt(env, "MEXP-SUPER", receipt="UK9AD1LR57", booking_attempts=3)
    failed("MEXP-SUPER", "Cannot link cancelled document: Reference Name: ACC-PRQ-2026-00541")
    paid_prompt(env, "MEXP-OTHER", receipt="UK9OTHER01", booking_attempts=3)  # a real failure: left to staff
    failed("MEXP-OTHER", "Sales Order SO-1 has already been fully paid.")
    paid_prompt(env, "MEXP-BOOKED", receipt="UK9BOOKED1", booking_attempts=3)
    failed("MEXP-BOOKED", "must be submitted")
    env.frappe.db.table("Payment Entry")["PE-9"] = {"name": "PE-9", "reference_no": "UK9BOOKED1", "docstatus": 1}
    paid_prompt(env, "MEXP-SI", pr="PR-SI", receipt="UK9INVOICE", booking_attempts=3)
    failed("MEXP-SI", "must be submitted")
    paid_prompt(env, "MEXP-NOLOG", receipt="UK9NOLOG01", booking_attempts=3)  # no log says why: left alone

    patch.execute()
    attempts = {n: prompt_row(env, n)["booking_attempts"] for n in env.frappe.db.table(EXPRESS)}
    assert attempts == {"MEXP-DRAFT": 0, "MEXP-SUPER": 0, "MEXP-OTHER": 3, "MEXP-BOOKED": 3, "MEXP-SI": 3,
                        "MEXP-NOLOG": 3, "MEXP-LATER": 3, "MEXP-EARLIER": 0}
    assert "mpsa_suite.mpsa_suite.patches.retry_stk_prompts_refused_on_draft_orders" in (
        REPO / "mpsa_suite" / "patches.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------- superseded request
def test_prompt_paid_on_a_superseded_request_books_on_the_request_that_replaced_it(env):
    order(env, total=56000.0, docstatus=1)
    request(env, "PR-OLD", docstatus=2, status="Cancelled", creation=NOW - datetime.timedelta(minutes=9))
    request(env, "PR-NEW", creation=NOW - datetime.timedelta(minutes=8))
    paid_prompt(env, pr="PR-OLD", amount=56000.0, receipt="UK9AD1LR57")
    assert env.stk_verify.book("MEXP-1") is True
    assert env.frappe.calls == [("neutralise", "MEXP-1"), ("book", "MEXP-1", "PR-NEW")]
    assert prompt_row(env)["reference_name"] == "PR-NEW"
    [(doctype, name, text)] = env.frappe.comments
    assert (doctype, name) == (EXPRESS, "MEXP-1") and "PR-OLD was superseded" in text


def test_superseded_request_on_a_draft_order_counts_then_books_on_the_live_request(env):
    order(env, total=56000.0)
    request(env, "PR-OLD", docstatus=2, status="Cancelled", creation=NOW - datetime.timedelta(minutes=9))
    request(env, "PR-NEW", creation=NOW - datetime.timedelta(minutes=8))
    paid_prompt(env, pr="PR-OLD", amount=56000.0, receipt="UK9AD1LR57")
    assert env.stk_verify.book("MEXP-1") is True
    assert env.released == ["SO-1"]
    assert ("book", "MEXP-1", "PR-NEW") in env.frappe.calls


def test_superseded_request_without_a_live_one_is_left_as_it_is(env):
    order(env, total=56000.0, docstatus=1)
    request(env, "PR-OLD", docstatus=2, status="Cancelled")
    paid_prompt(env, pr="PR-OLD", amount=56000.0)
    env.stk_verify.book("MEXP-1")
    assert prompt_row(env)["reference_name"] == "PR-OLD"
    assert env.frappe.comments == []
