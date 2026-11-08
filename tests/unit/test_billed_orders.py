"""Receipts for orders that are already invoiced, on an in-memory frappe.

ERPNext takes no payment against a fully billed Sales Order. A receipt naming such an order pays
the order's own open invoice (never the customer's oldest invoices: walk-in sales share one
customer); several open invoices: the one whose balance equals the amount, else staff decide;
all paid: held for staff by the matcher, a customer advance in the prepaid-order flow. A Payment
Request on an order settled through its invoices is marked Paid.
"""

import importlib

import pytest

import fake_frappe

REGISTER = "Mpesa C2B Payment Register"
SUITE = "M-Pesa Suite Settings"


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.utils.strip_html = lambda text: text
        frappe.db.table(SUITE)[SUITE] = {"name": SUITE, "enable_sales_order_payments": 1, "amount_tolerance": 1}
        frappe.get_cached_doc = lambda doctype, *a, **kw: frappe.get_doc(doctype, doctype)
        mods = fake_frappe._dict(
            frappe=frappe,
            common=importlib.import_module("mpsa_suite.money_in.common"),
            matching=importlib.import_module("mpsa_suite.money_in.matching"),
            booking=importlib.import_module("mpsa_suite.money_in.booking"),
            sales_order=importlib.import_module("mpsa_suite.money_in.sales_order"),
            logged=[],
            booked=[],
            stamped=[],
        )
        monkeypatch.setattr(mods.sales_order, "log_match", lambda *a, **kw: mods.logged.append((a, kw)))
        monkeypatch.setattr(mods.booking, "book_register", lambda name, method=None: mods.booked.append(name))
        monkeypatch.setattr(mods.booking, "stamp", lambda reg, match: mods.stamped.append((reg.name, match.doctype, match.name)))
        yield mods
    finally:
        fake_frappe.uninstall(saved)


def order(env, name="SO-1", per_billed=100, docstatus=1, customer="Cash Cust - Isinya", total=36000.0):
    env.frappe.db.table("Sales Order")[name] = {
        "name": name, "docstatus": docstatus, "per_billed": per_billed, "customer": customer,
        "status": "Completed" if per_billed >= 100 else "To Deliver and Bill", "grand_total": total,
        "rounded_total": total, "advance_paid": 0,
    }


def invoice(env, name, so="SO-1", outstanding=36000.0, customer="Cash Cust - Isinya", docstatus=1, is_return=0):
    env.frappe.db.table("Sales Invoice")[name] = {
        "name": name, "customer": customer, "outstanding_amount": outstanding, "docstatus": docstatus,
        "is_return": is_return,
    }
    items = env.frappe.db.table("Sales Invoice Item")
    items[f"{name}-1"] = {"name": f"{name}-1", "parent": name, "sales_order": so, "docstatus": docstatus}


def register(env, name="MPC2B-1", amount=36000.0):
    row = {"name": name, "docstatus": 0, "transid": "UK7KO1K9TA", "transamount": amount, "billrefnumber": "SO-1",
           "customer": None, "payment_entry": None}
    env.frappe.db.table(REGISTER)[name] = row
    return env.frappe._dict(row)


def so_match(env, **kw):
    return env.matching.Match(method="Reference", doctype="Sales Order", name="SO-1", customer="Cash Cust - Isinya", **kw)


def test_billed_order_receipt_pays_its_own_invoice(env):
    order(env)
    invoice(env, "SINV-OLD", so="SO-OLD", outstanding=50000)  # the customer's older debt: never touched
    invoice(env, "SINV-1")
    m = env.matching.retarget_billed_order(register(env), so_match(env))
    assert (m.doctype, m.name, m.customer) == ("Sales Invoice", "SINV-1", "Cash Cust - Isinya")
    assert "SO-1 invoiced as SINV-1" in m.details


def test_unbilled_or_draft_order_is_left_alone(env):
    order(env, per_billed=40)
    invoice(env, "SINV-1")
    m = so_match(env)
    assert env.matching.retarget_billed_order(register(env), m) is m
    order(env, per_billed=0, docstatus=0)
    assert env.matching.retarget_billed_order(register(env), m) is m


def test_billed_order_with_paid_invoices_is_held_for_staff(env):
    order(env)
    invoice(env, "SINV-1", outstanding=0)
    invoice(env, "SINV-R", outstanding=-500, is_return=1)  # a credit note never counts
    m = env.matching.retarget_billed_order(register(env), so_match(env))
    assert m.hold and "invoices are paid" in m.details


def test_several_open_invoices_pick_the_exact_balance_else_ambiguous(env):
    order(env)
    invoice(env, "SINV-1", outstanding=20000)
    invoice(env, "SINV-2", outstanding=36000)
    m = env.matching.retarget_billed_order(register(env), so_match(env))
    assert m.name == "SINV-2"
    m = env.matching.retarget_billed_order(register(env, amount=5000), so_match(env))
    assert m.ambiguous and sorted(m.candidates) == ["SINV-1", "SINV-2"]


def test_other_matches_pass_through(env):
    order(env)
    invoice(env, "SINV-1")
    for m in (env.matching.Match(method="Reference", doctype="Sales Invoice", name="SINV-9"),
              so_match(env, hold=True), so_match(env, ambiguous=True, candidates=[])):
        assert env.matching.retarget_billed_order(register(env), m) is m


def test_prepaid_order_flow_books_invoiced_order_receipt_on_the_invoice(env):
    order(env)
    invoice(env, "SINV-1")
    reg = register(env)
    env.sales_order.book_on_invoice(env.frappe.get_doc(REGISTER, reg.name), env.frappe.get_doc("Sales Order", "SO-1"))
    assert env.stamped == [("MPC2B-1", "Sales Invoice", "SINV-1")]
    assert env.booked == ["MPC2B-1"]


def test_prepaid_order_flow_invoices_paid_books_customer_advance(env):
    order(env)
    invoice(env, "SINV-1", outstanding=0)
    reg = register(env)
    env.sales_order.book_on_invoice(env.frappe.get_doc(REGISTER, reg.name), env.frappe.get_doc("Sales Order", "SO-1"))
    row = env.frappe.db.table(REGISTER)["MPC2B-1"]
    assert row["matched_reference_doctype"] is None and row["match_method"].startswith("Customer advance")
    assert env.booked == ["MPC2B-1"] and env.stamped == []


def test_prepaid_order_flow_several_open_invoices_waits_for_staff(env):
    order(env)
    invoice(env, "SINV-1", outstanding=20000)
    invoice(env, "SINV-2", outstanding=20000)
    reg = register(env)
    env.sales_order.book_on_invoice(env.frappe.get_doc(REGISTER, reg.name), env.frappe.get_doc("Sales Order", "SO-1"))
    assert env.booked == [] and env.logged and env.logged[0][0][0] == "Ambiguous"


def test_order_settled_by_invoices(env):
    order(env)
    assert not env.common.order_settled_by_invoices("SO-1")  # billed, but no invoice found
    invoice(env, "SINV-1", outstanding=0.5)  # within tolerance
    assert env.common.order_settled_by_invoices("SO-1")
    invoice(env, "SINV-2", outstanding=100)
    assert not env.common.order_settled_by_invoices("SO-1")
    order(env, per_billed=50)
    env.frappe.db.table("Sales Invoice")["SINV-2"]["outstanding_amount"] = 0
    assert not env.common.order_settled_by_invoices("SO-1")  # part billed: the rest is still owed
