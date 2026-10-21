"""Receipt register safety, on an in-memory frappe.

Covers: a receipt with a live reversal is never stamped, booked or counted toward a prepaid
order (Failed / cancelled / draft reversals do not hold it); the reversal result says why a
draft stays unbooked; a refused prepaid-order release (direct or through a workflow) is
contained, logged with the credit controller hint, leaves no error dialog behind and is retried
with backoff; order errors are logged again only when they change.
"""

import datetime
import importlib
import sys
import types

import pytest

import fake_frappe

REGISTER = "Mpesa C2B Payment Register"
REVERSAL = "Mpesa Reversal Request"
SUITE = "M-Pesa Suite Settings"
MATCH_LOG = "Mpesa Match Log"


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.utils.strip_html = lambda text: text.replace("<br>", " ")
        frappe.roles = {}
        frappe.get_roles = lambda user=None: frappe.roles.get(user or frappe.session.user, [])
        frappe.db.table(SUITE)[SUITE] = {"name": SUITE, "enable_sales_order_payments": 1, "amount_tolerance": 1}
        frappe.get_cached_doc = lambda doctype, *a, **kw: frappe.get_doc(doctype, doctype)
        mods = fake_frappe._dict(
            frappe=frappe,
            common=importlib.import_module("frappe_mpsa_payments.money_in.common"),
            booking=importlib.import_module("frappe_mpsa_payments.money_in.booking"),
            sales_order=importlib.import_module("frappe_mpsa_payments.money_in.sales_order"),
            reversal=importlib.import_module("frappe_mpsa_payments.money_out.reversal"),
            logged=[],
        )
        for mod in (mods.booking, mods.sales_order):
            monkeypatch.setattr(mod, "log_match", lambda *a, **kw: mods.logged.append((a, kw)))
        yield mods
    finally:
        fake_frappe.uninstall(saved)


def register(env, name="MPC2B-1", **kw):
    row = {"name": name, "docstatus": 0, "transid": f"T{name[-1]}", "transamount": 5000.0,
           "billrefnumber": "", "customer": None, "payment_entry": None}
    row.update(kw)
    env.frappe.db.table(REGISTER)[name] = row
    return row


def reversal(env, register_name, status="Reversed", docstatus=1, name=None):
    name = name or f"MRV-{len(env.frappe.db.table(REVERSAL)) + 1}"
    env.frappe.db.table(REVERSAL)[name] = {"name": name, "c2b_register": register_name, "status": status,
                                           "docstatus": docstatus, "creation": name}
    return name


# ---------------------------------------------------------------- which reversals hold a receipt
def test_holding_states_are_the_reversal_live_states(env):
    assert set(env.common.REVERSAL_HOLDS) == set(env.reversal.LIVE)


@pytest.mark.parametrize("status", ["Not Initiated", "Initiated", "Unknown", "Reversed", "Already Reversed"])
def test_submitted_live_reversal_holds_the_receipt(env, status):
    register(env)
    mrv = reversal(env, "MPC2B-1", status=status)
    assert env.common.reversal_holding("MPC2B-1").name == mrv
    assert env.common.reversed_registers() == ["MPC2B-1"]
    assert env.common.reversed_registers(["MPC2B-1", "MPC2B-2"]) == ["MPC2B-1"]


def test_held_list_never_holds_an_empty_register(env):
    # callers filter with "name not in": one NULL there and v16 selects no register at all
    register(env)
    reversal(env, "MPC2B-1")
    reversal(env, "MPC2B-1", status="Unknown")
    reversal(env, None)
    reversal(env, "")
    assert env.common.reversed_registers() == ["MPC2B-1"]


@pytest.mark.parametrize("status,docstatus", [("Failed", 1), ("Unknown", 2), ("Not Initiated", 2), ("Not Initiated", 0)])
def test_failed_cancelled_or_draft_reversal_does_not_hold(env, status, docstatus):
    register(env)
    reversal(env, "MPC2B-1", status=status, docstatus=docstatus)
    assert env.common.reversal_holding("MPC2B-1") is None
    assert env.common.reversed_registers() == []
    assert env.common.reversed_registers([]) == []


# ---------------------------------------------------------------- booking
def test_book_register_never_submits_a_reversed_receipt(env):
    row = register(env, customer="CUST-1")
    reversal(env, "MPC2B-1")
    assert env.booking.book_register("MPC2B-1", "Customer Name") is None
    assert row["docstatus"] == 0 and not row.get("submit_payment") and not row["payment_entry"]


def test_apply_match_leaves_a_reversed_receipt_unstamped(env):
    row = register(env)
    reversal(env, "MPC2B-1", status="Unknown")
    match = env.frappe._dict(method="Customer Name", doctype="Customer", name="CUST-1", customer="CUST-1")
    env.booking.apply_match(env.frappe.get_doc(REGISTER, "MPC2B-1"), match)
    assert row["customer"] is None and not row.get("matched_reference_name") and not row.get("match_method")
    assert env.logged == []


# ---------------------------------------------------------------- prepaid orders
def sales_order(env, name="SO-1", total=10000.0, docstatus=0, status="Draft"):
    env.frappe.db.table("Sales Order")[name] = {"name": name, "docstatus": docstatus, "status": status,
                                               "customer": "CUST-1", "grand_total": total, "rounded_total": total}
    return name


def test_reversed_receipt_does_not_count_toward_an_order(env):
    sales_order(env)
    register(env, "MPC2B-1", billrefnumber="SO-1")
    register(env, "MPC2B-2", matched_reference_doctype="Sales Order", matched_reference_name="SO-1")
    reversal(env, "MPC2B-1")
    assert [r.name for r in env.sales_order.receipts_for("SO-1")] == ["MPC2B-2"]


def test_refunded_deposit_cannot_release_a_draft_order(env, monkeypatch):
    # 10,000 order: a 5,000 deposit was reversed, then the customer paid 5,000 again
    sales_order(env)
    for name in ("MPC2B-1", "MPC2B-2"):
        register(env, name, customer="CUST-1", matched_reference_doctype="Sales Order", matched_reference_name="SO-1")
    reversal(env, "MPC2B-1")
    released = []
    monkeypatch.setattr(env.sales_order, "release_order", released.append)
    env.sales_order.process_order("SO-1")
    assert released == []


# ---------------------------------------------------------------- reversal result on the register
RESULT = {"ResultCode": 0, "ResultDesc": "Reversed", "ConversationID": "AG_1", "OriginatorConversationID": "OCID-1",
          "TransactionID": "SGT7H7RSMC", "ResultParameters": {"ResultParameter": [{"Key": "Charge", "Value": 0}]}}


@pytest.mark.parametrize(
    "state,note,marked",
    [
        ({"docstatus": 0}, "will not be booked now", True),
        ({"docstatus": 0, "payment_entry": "ACC-PAY-1"}, "cancelled or reversed by hand", False),
        ({"docstatus": 1, "payment_entry": "ACC-PAY-1"}, "cancelled or reversed by hand", False),
    ],
)
def test_reversal_result_explains_the_register(env, state, note, marked):
    row = register(env, **state)
    env.frappe.db.table(REVERSAL)["MRV-1"] = {"name": "MRV-1", "docstatus": 1, "status": "Initiated",
                                              "c2b_register": "MPC2B-1", "conversation_id": "AG_1",
                                              "originator_conversation_id": "OCID-1"}
    env.reversal.handle_result(RESULT, "MRV-1")
    [(_, _, text)] = [c for c in env.frappe.comments if c[1] == "MPC2B-1"]
    assert note in text
    assert (row.get("match_method") == "Reversed") is marked


# ---------------------------------------------------------------- prepaid order release refused
@pytest.fixture
def no_workflow(monkeypatch):
    workflow = types.ModuleType("frappe.model.workflow")
    workflow.get_workflow_name = lambda doctype: None
    workflow.get_transitions = lambda doc: []
    workflow.apply_workflow = lambda doc, action: None
    monkeypatch.setitem(sys.modules, "frappe.model.workflow", workflow)


def test_refused_release_is_contained_and_logged_with_the_hint(env, no_workflow):
    frappe = env.frappe
    frappe.db.table(SUITE)[SUITE]["sales_order_approver"] = "approver@example.com"
    frappe.db.table("Accounts Settings")["Accounts Settings"] = {"credit_controller": "Credit Controller"}
    frappe.roles["approver@example.com"] = ["Sales User"]
    rolled_back = []
    frappe.db.rollback = lambda save_point=None: rolled_back.append(save_point)
    frappe.local.message_log = ["earlier message"]

    def refuse():
        frappe.local.message_log.append("Credit limit has been crossed<br>")
        raise frappe.ValidationError("Credit limit has been crossed for customer CUST-1 (11000/10000)<br>")

    sales_order(env)
    frappe.db.table("Sales Order")["SO-1"]["submit"] = refuse

    env.sales_order.release_order("SO-1")  # must not raise

    assert rolled_back == ["mpesa_so_release"]
    assert frappe.local.message_log == ["earlier message"]
    assert frappe.session.user == "payer@example.com"  # approver session undone
    [(args, kw)] = env.logged
    assert args[0] == "Error" and kw["on_change"] and kw["reference_name"] == "SO-1"
    assert "Credit limit has been crossed for customer CUST-1" in kw["details"]
    assert "give approver@example.com the 'Credit Controller' role" in kw["details"]
    assert env.sales_order.REFUSED in kw["details"]  # what the backoff recognises


def test_refused_workflow_release_undoes_every_step(env, monkeypatch):
    frappe = env.frappe
    frappe.db.table(SUITE)[SUITE].update(sales_order_approver="approver@example.com",
                                         sales_order_workflow_actions="Submit for Approval\nApprove")
    frappe.db.table("Accounts Settings")["Accounts Settings"] = {"credit_controller": "Credit Controller"}
    rolled_back, applied = [], []
    frappe.db.rollback = lambda save_point=None: rolled_back.append(save_point)
    frappe.local.message_log = []

    def apply_workflow(doc, action):
        applied.append((action, frappe.session.user))
        if action == "Approve":  # the step that submits: ERPNext's credit check refuses it
            frappe.local.message_log.append("Credit limit has been crossed")
            raise frappe.ValidationError("Credit limit has been crossed for customer CUST-1")

    workflow = types.ModuleType("frappe.model.workflow")
    workflow.get_workflow_name = lambda doctype: "Paid Orders"
    workflow.get_transitions = lambda doc: [frappe._dict(action="Submit for Approval"), frappe._dict(action="Approve")]
    workflow.apply_workflow = apply_workflow
    monkeypatch.setitem(sys.modules, "frappe.model.workflow", workflow)
    sales_order(env)
    row = frappe.db.table("Sales Order")["SO-1"]
    row["as_dict"] = lambda: dict(row)

    env.sales_order.release_order("SO-1")  # must not raise

    assert applied == [("Submit for Approval", "approver@example.com"), ("Approve", "approver@example.com")]
    assert rolled_back == ["mpesa_so_release"]  # the first, applied step is undone too
    assert frappe.local.message_log == []
    assert frappe.session.user == "payer@example.com"
    [(args, kw)] = env.logged
    assert args[0] == "Error" and kw["on_change"] and "Credit limit has been crossed" in kw["details"]
    assert "give approver@example.com the 'Credit Controller' role" in kw["details"]


# ---------------------------------------------------------------- refused release backoff
def _minutes_ago(frappe, minutes):
    return None if minutes is None else frappe.utils.now_datetime() - datetime.timedelta(minutes=minutes)


@pytest.mark.parametrize(
    "first,last,order_saved,customer_saved,attempt",
    [
        (3, 1, None, None, False),  # 2 minutes after the first refusal
        (3, 2, None, None, True),
        (10, 4, None, None, False),  # refused again 6 minutes after the first: wait 6
        (10, 6, None, None, True),
        (2880, 300, None, None, False),  # capped at 6 hours
        (2880, 360, None, None, True),
        (10, 4, 1, None, True),  # order saved since the last refusal
        (10, 4, None, 1, True),  # customer saved (credit limit raised)
        (10, 4, 5, 5, False),  # saved before it: no reason to try sooner
    ],
)
def test_refused_release_is_retried_with_backoff(env, no_workflow, first, last, order_saved, customer_saved, attempt):
    frappe = env.frappe
    frappe.db.table(MATCH_LOG)["MML-1"] = {
        "name": "MML-1", "result": "Error", "reference_doctype": "Sales Order", "reference_name": "SO-1",
        "creation": _minutes_ago(frappe, first), "modified": _minutes_ago(frappe, last),
        "details": f"Fully paid but a@x.com {env.sales_order.REFUSED}: Credit limit has been crossed",
    }
    frappe.db.table("Customer")["CUST-1"] = {"name": "CUST-1", "modified": _minutes_ago(frappe, customer_saved)}
    sales_order(env)
    tried = []
    frappe.db.table("Sales Order")["SO-1"].update(modified=_minutes_ago(frappe, order_saved),
                                                  submit=lambda: tried.append(1))
    env.sales_order.release_order("SO-1")
    assert bool(tried) is attempt


def test_other_order_errors_do_not_hold_the_release(env, no_workflow):
    frappe = env.frappe
    frappe.db.table(MATCH_LOG)["MML-1"] = {
        "name": "MML-1", "result": "Error", "reference_doctype": "Sales Order", "reference_name": "SO-1",
        "creation": _minutes_ago(frappe, 0), "modified": _minutes_ago(frappe, 0),
        "details": "Fully paid but still in 'Draft'.",
    }
    sales_order(env)
    tried = []
    frappe.db.table("Sales Order")["SO-1"]["submit"] = lambda: tried.append(1)
    env.sales_order.release_order("SO-1")
    assert tried == [1]


# ---------------------------------------------------------------- Match Log on_change
def test_log_match_on_change_writes_a_new_reason_and_marks_a_repeat(env, monkeypatch):
    frappe = env.frappe
    frappe.db.table("DocType")[MATCH_LOG] = {"name": MATCH_LOG}
    frappe.new_doc = lambda doctype: frappe.get_doc({"doctype": doctype, "flags": frappe._dict()})
    monkeypatch.setattr(fake_frappe.FakeDoc, "set", lambda self, key, value: self._row.__setitem__(key, value),
                        raising=False)
    hour_ago = _minutes_ago(frappe, 60)
    rows = frappe.db.table(MATCH_LOG)
    # an earlier, different error for the order: 'once' would have swallowed the new reason
    rows["MML-1"] = {"name": "MML-1", "result": "Error", "reference_name": "SO-1", "creation": hour_ago,
                     "modified": hour_ago, "details": "Order is fully paid but no approver is set."}

    def log():
        env.common.log_match("Error", "Sales Order", on_change=True, reference_doctype="Sales Order",
                             reference_name="SO-1", details="Credit limit <crossed> for CUST-1")

    log()
    assert len(rows) == 2

    del rows["MML-1"]
    [new] = rows.values()
    new.update(details="Credit limit &lt;crossed&gt; for CUST-1", modified=hour_ago)  # as stored: sanitised
    log()
    assert len(rows) == 1 and new["modified"] == frappe.utils.now_datetime()  # same reason: only marked as seen


@pytest.mark.parametrize(
    "role,roles,expected",
    [
        ("Credit Controller", ["Credit Controller"], ""),
        ("Credit Controller", ["Sales User"], "give a@x.com the 'Credit Controller' role"),
        ("", ["System Manager"], "set a Credit Controller role in Accounts Settings and give it to a@x.com"),
    ],
)
def test_credit_controller_hint(env, role, roles, expected):
    env.frappe.db.table("Accounts Settings")["Accounts Settings"] = {"credit_controller": role}
    env.frappe.roles["a@x.com"] = roles
    hint = env.sales_order._credit_controller_hint("a@x.com")
    assert (expected in hint) if expected else hint == ""
