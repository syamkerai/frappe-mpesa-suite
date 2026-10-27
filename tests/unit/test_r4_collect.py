"""Round 4 (WP6): Request M-Pesa payment on a submitted Sales Order or Sales Invoice
(money_in.collect): who may, on what, for how much, how often, and that it goes through ERPNext's
Payment Request (make_payment_request, then submit, which prompts). Bench coverage:
mpsa_suite/money_in/test_collect.py."""

import datetime
import importlib
import sys
import types

import fake_frappe
import pytest

SO, SI, PR = "Sales Order", "Sales Invoice", "Payment Request"
EXPRESS = "Mpesa Express Request"
COMPANY = "_T Collect Co"
NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)


class FakeCache:
    def __init__(self):
        self.counts = {}

    def get_value(self, key):
        return self.counts.get(key)

    def make_key(self, key):
        return "site|" + key

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, seconds):
        pass


class FakePR:
    """What make_payment_request(return_doc=1, submit_doc=0) hands back: an unsaved request."""

    def __init__(self, env, args, saved=False):
        self.env, self.args, self.saved = env, args, saved
        self.flags = fake_frappe._dict()
        self.values = {"grand_total": 999999, "phone_number": args.get("phone_number"), "payment_channel": "Email"}
        self.name = None
        self.calls = []

    def update(self, values):
        self.values.update(values)

    def is_new(self):
        return not self.saved

    def insert(self):
        self.calls.append(("insert", dict(self.flags)))
        self.name = "ACC-PRQ-1"

    def save(self):
        self.calls.append(("save", dict(self.flags)))
        self.name = self.name or "ACC-PRQ-1"

    def submit(self):
        self.calls.append(("submit", dict(self.flags)))


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.user_roles = {"Sales User"}
        frappe.session.user = "cashier@example.com"
        frappe.cache = FakeCache()
        frappe.permission_calls, frappe.denied = [], set()

        def has_permission(doctype, ptype="read", doc=None, throw=False, **kw):
            frappe.permission_calls.append((doctype, ptype, doc))
            if (doctype, ptype) in frappe.denied:
                if throw:
                    raise frappe.PermissionError(f"no {ptype} on {doctype}")
                return False
            return True

        frappe.has_permission = has_permission
        meta_fields = {PR: {"outstanding_amount"}, "Payment Gateway Account": {"company"}, "Mpesa Settings": {"company"}}
        frappe.get_meta = lambda doctype, *a, **kw: types.SimpleNamespace(
            has_field=lambda f: f in meta_fields.get(doctype, set()))
        frappe.utils.now_datetime = lambda: NOW
        frappe.suite_values = {}
        frappe.get_cached_doc = lambda doctype, *a, **kw: fake_frappe._dict(frappe.suite_values)

        # ERPNext's Payment Request functions
        made = []
        erp = types.ModuleType("erpnext.accounts.doctype.payment_request.payment_request")

        def get_amount(ref_doc, payment_account=None):
            if ref_doc.doctype == SO:
                return (ref_doc.rounded_total or ref_doc.grand_total) - (ref_doc.advance_paid or 0)
            return ref_doc.outstanding_amount

        def make_payment_request(**args):
            pr = FakePR(types.SimpleNamespace(), args)
            made.append(pr)
            return pr

        erp.get_amount, erp.make_payment_request = get_amount, make_payment_request
        for name in ("erpnext", "erpnext.accounts", "erpnext.accounts.doctype", "erpnext.accounts.doctype.payment_request"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, erp.__name__, erp)

        db = frappe.db
        db.table("Customer")["CUST-1"] = {"name": "CUST-1", "mobile_no": "0712345678"}
        db.table(SO)["SO-1"] = {"name": "SO-1", "docstatus": 1, "status": "To Deliver and Bill", "currency": "KES",
                                "company": COMPANY, "customer": "CUST-1", "customer_name": "Wanjiku", "grand_total": 5000,
                                "rounded_total": 5000, "advance_paid": 1000, "per_billed": 0}
        db.table(SI)["SINV-1"] = {"name": "SINV-1", "docstatus": 1, "currency": "KES", "company": COMPANY,
                                  "customer": "CUST-1", "outstanding_amount": 3000, "is_pos": 0, "is_return": 0,
                                  "contact_mobile": "+254 722 000 111"}
        _shortcode(db, "TILL", COMPANY, limit=150000)
        collect = importlib.import_module("mpsa_suite.money_in.collect")
        superseded = []
        monkeypatch.setattr(collect, "supersede_unpaid", lambda doc: superseded.append(doc.reference_name))
        yield types.SimpleNamespace(frappe=frappe, db=db, collect=collect, made=made, superseded=superseded)
    finally:
        fake_frappe.uninstall(saved)


def _shortcode(db, name, company, *, stk=1, limit=0, default=1):
    db.table("Mpesa Settings")[name] = {"name": name, "company": company, "business_shortcode": "174379",
                                        "till_number": None, "transaction_limit": limit, "use_stk": stk}
    db.table("Payment Gateway")[f"Mpesa-{name}"] = {"name": f"Mpesa-{name}", "gateway_settings": "Mpesa Settings",
                                                   "gateway_controller": name}
    db.table("Payment Gateway Account")[f"Mpesa-{name}-KES"] = {
        "name": f"Mpesa-{name}-KES", "payment_gateway": f"Mpesa-{name}", "payment_account": f"Mpesa-{name} - TC",
        "is_default": default, "company": company}


def _request(env, doctype=SI, name="SINV-1", phone="0712 345 678", amount=1000, gateway=None):
    return env.collect.request_payment(doctype, name, phone, amount, gateway)


# ---------------------------------------------------------------- who, on what
def test_only_sales_orders_and_invoices(env):
    with pytest.raises(env.frappe.ValidationError, match="Sales Order or a Sales Invoice"):
        _request(env, doctype="Purchase Invoice", name="PINV-1")
    with pytest.raises(env.frappe.ValidationError, match="Sales Order or a Sales Invoice"):
        env.collect.collect_options("Quotation", "QTN-1")
    assert env.made == []


def test_collect_roles_only(env):
    env.frappe.user_roles = {"Purchase User", "HR User"}
    with pytest.raises(env.frappe.PermissionError):
        _request(env)
    with pytest.raises(env.frappe.PermissionError):
        env.collect.collect_options(SI, "SINV-1")
    assert env.made == []


@pytest.mark.parametrize("denied", [(SI, "read"), (PR, "create")])
def test_read_on_the_document_and_create_on_payment_request(env, denied):
    env.frappe.denied = {denied}
    with pytest.raises(env.frappe.PermissionError):
        _request(env)
    assert env.made == []


def test_the_checks_name_the_document(env):
    _request(env)
    assert (SI, "read", "SINV-1") in env.frappe.permission_calls
    assert (PR, "create", None) in env.frappe.permission_calls


@pytest.mark.parametrize("doctype, name, values, message", [
    (SI, "SINV-1", {"docstatus": 0}, "Submit SINV-1 first"),
    (SI, "SINV-1", {"currency": "USD"}, "M-Pesa collects KES"),
    (SI, "SINV-1", {"is_return": 1}, "is a return"),
    (SI, "SINV-1", {"is_pos": 1}, "is a POS invoice"),
    (SI, "SINV-1", {"outstanding_amount": 0}, "nothing left to pay"),
    (SO, "SO-1", {"status": "Closed"}, "is Closed"),
    (SO, "SO-1", {"status": "On Hold"}, "is On Hold"),
    (SO, "SO-1", {"per_billed": 100}, "fully invoiced"),
    (SO, "SO-1", {"advance_paid": 5000}, "nothing left to pay"),
])
def test_what_cannot_be_collected_from_its_form(env, doctype, name, values, message):
    env.db.table(doctype)[name].update(values)
    with pytest.raises(env.frappe.ValidationError, match=message):
        _request(env, doctype=doctype, name=name, amount=10)
    assert env.collect.collect_options(doctype, name)["problem"].find(message.split()[0]) >= 0
    assert env.made == []


# ---------------------------------------------------------------- how much
def test_at_most_what_is_owed_and_at_least_one_shilling(env):
    with pytest.raises(env.frappe.ValidationError, match="KES 3,000.00 left to ask for"):
        _request(env, amount=3000.01)
    with pytest.raises(env.frappe.ValidationError, match="at least KES 1"):
        _request(env, amount=0.5)
    assert env.made == []
    assert _request(env, amount=3000)["amount"] == 3000


def test_an_order_owes_its_total_less_advances(env):
    assert env.collect.collect_options(SO, "SO-1")["owed"] == 4000
    with pytest.raises(env.frappe.ValidationError, match="KES 4,000.00 left"):
        _request(env, doctype=SO, name="SO-1", amount=4001)
    _request(env, doctype=SO, name="SO-1", amount=4000)


def test_at_most_the_shortcodes_transaction_limit(env):
    env.db.table(SI)["SINV-1"]["outstanding_amount"] = 400000
    with pytest.raises(env.frappe.ValidationError, match="above the M-Pesa Transaction Limit of KES 150,000.00"):
        _request(env, amount=150001)
    _request(env, amount=150000)
    # a blank limit is Safaricom's 250,000
    env.db.table("Mpesa Settings")["TILL"]["transaction_limit"] = 0
    with pytest.raises(env.frappe.ValidationError, match="Limit of KES 250,000.00"):
        _request(env, amount=250001)
    assert [g["limit"] for g in env.collect.collect_options(SI, "SINV-1")["gateways"]] == [250000]


def _open_request(env, name, status="Requested", outstanding=1000, gateway="Mpesa-TILL"):
    env.db.table(PR)[name] = {"name": name, "reference_doctype": SI, "reference_name": "SINV-1", "docstatus": 1,
                              "status": status, "grand_total": outstanding, "outstanding_amount": outstanding,
                              "payment_gateway": gateway, "payment_gateway_account": gateway + "-KES"}


def test_open_payment_requests_hold_what_they_ask_for(env):
    _open_request(env, "PR-OLD", outstanding=2500)
    _open_request(env, "PR-BANK", outstanding=500, gateway="Stripe")
    options = env.collect.collect_options(SI, "SINV-1")
    assert (options["owed"], options["held_by"]) == (0, ["PR-OLD", "PR-BANK"])
    with pytest.raises(env.frappe.ValidationError, match="PR-OLD, PR-BANK already ask for everything"):
        _request(env, amount=100)
    assert env.superseded == [] and env.made == []


def test_unpaid_mpesa_requests_a_new_prompt_supersedes_do_not_hold(env):
    env.frappe.suite_values = {"supersede_unpaid_prompts": 1}
    _open_request(env, "PR-OLD", outstanding=2500)
    _open_request(env, "PR-PART", status="Partially Paid", outstanding=500)
    _open_request(env, "PR-BANK", outstanding=200, gateway="Stripe")
    options = env.collect.collect_options(SI, "SINV-1")
    assert (options["owed"], options["held_by"]) == (2300, ["PR-PART", "PR-BANK"])
    _request(env, amount=2300)
    assert env.superseded == ["SINV-1"]  # before ERPNext reckons the new request


# ---------------------------------------------------------------- the phone, the shortcode
@pytest.mark.parametrize("phone", ["", "12345", "0612345678", "2.54712E+11"])
def test_a_kenyan_mobile_number(env, phone):
    with pytest.raises(env.frappe.ValidationError, match="Kenyan mobile number"):
        _request(env, phone=phone)


def test_the_default_phone_is_the_documents_contact_then_the_customers(env):
    assert env.collect.collect_options(SI, "SINV-1")["phone"] == "0722000111"
    assert env.collect.collect_options(SO, "SO-1")["phone"] == "0712345678"


def test_only_shortcodes_of_the_company_that_send_prompts(env):
    _shortcode(env.db, "OTHER", "_T Another Co", default=0)
    _shortcode(env.db, "PAYBILL", COMPANY, stk=0, default=0)
    assert [g["name"] for g in env.collect.collect_options(SI, "SINV-1")["gateways"]] == ["Mpesa-TILL-KES"]
    with pytest.raises(env.frappe.ValidationError, match="cannot prompt for"):
        _request(env, gateway="Mpesa-OTHER-KES")
    with pytest.raises(env.frappe.ValidationError, match="cannot prompt for"):
        _request(env, gateway="Mpesa-PAYBILL-KES")
    env.db.table("Mpesa Settings")["TILL"]["use_stk"] = 0
    with pytest.raises(env.frappe.ValidationError, match="Send STK Prompts"):
        _request(env)


# ---------------------------------------------------------------- how often
def test_at_most_five_prompts_a_minute_per_user(env):
    for _ in range(5):
        _request(env, amount=1)
    with pytest.raises(env.frappe.ValidationError, match="Too many M-Pesa prompts"):
        _request(env, amount=1)
    assert len(env.made) == 5


def _prompt(env, name, reference_doctype, reference_name, seconds_ago, status="In Progress"):
    env.db.table(EXPRESS)[name] = {"name": name, "reference_doctype": reference_doctype, "reference_name": reference_name,
                                   "status": status, "docstatus": 1,
                                   "creation": NOW - datetime.timedelta(seconds=seconds_ago)}


@pytest.mark.parametrize("reference", [(PR, "PR-OLD"), (SI, "SINV-1")])
def test_no_new_prompt_while_the_last_may_be_on_the_phone(env, reference):
    _open_request(env, "PR-OLD", status="Requested", outstanding=100)
    _prompt(env, "MPESAEXP-1", *reference, seconds_ago=30)
    with pytest.raises(env.frappe.ValidationError, match="A prompt was just sent for SINV-1 \\(MPESAEXP-1\\)"):
        _request(env, amount=100)
    env.db.table(EXPRESS)["MPESAEXP-1"]["creation"] = NOW - datetime.timedelta(seconds=91)
    _request(env, amount=100)
    _prompt(env, "MPESAEXP-2", *reference, seconds_ago=5, status="Failed")
    _request(env, amount=100)


def test_a_draft_payment_request_is_dealt_with_first(env):
    env.db.table(PR)["PR-DRAFT"] = {"name": "PR-DRAFT", "reference_doctype": SI, "reference_name": "SINV-1",
                                    "docstatus": 0, "payment_gateway": "Mpesa-TILL", "payment_gateway_account": ""}
    with pytest.raises(env.frappe.ValidationError, match="PR-DRAFT is a draft for SINV-1"):
        _request(env)
    env.frappe.suite_values = {"supersede_unpaid_prompts": 1}  # an M-Pesa draft the prompt removes
    _request(env)
    env.db.table(PR)["PR-DRAFT"]["payment_gateway"] = "Stripe"
    with pytest.raises(env.frappe.ValidationError, match="PR-DRAFT is a draft"):
        _request(env)


# ---------------------------------------------------------------- through ERPNext's Payment Request
def test_the_prompt_is_a_payment_request_submitted(env):
    env.db.table(EXPRESS)["MPESAEXP-9"] = {"name": "MPESAEXP-9", "reference_doctype": PR, "reference_name": "ACC-PRQ-1",
                                           "status": "In Progress", "docstatus": 1, "creation": NOW}
    out = _request(env, phone="+254 712 345 678", amount=1250.5)
    (pr,) = env.made
    assert pr.args == {"dt": SI, "dn": "SINV-1", "payment_gateway_account": "Mpesa-TILL-KES",
                       "phone_number": "254712345678", "mute_email": 1, "submit_doc": 0, "return_doc": 1}
    assert pr.values == {"grand_total": 1250.5, "phone_number": "254712345678", "payment_channel": "Phone"}
    assert [c for c, _ in pr.calls] == ["insert", "submit"]
    assert all(flags.get("ignore_permissions") and flags.get("mute_email") for _, flags in pr.calls)
    assert out == {"payment_request": "ACC-PRQ-1", "express_request": "MPESAEXP-9", "amount": 1250.5,
                   "phone": "0712345678"}


def test_a_request_erpnext_saved_as_a_draft_is_saved_then_submitted(env, monkeypatch):
    erp = sys.modules["erpnext.accounts.doctype.payment_request.payment_request"]
    monkeypatch.setattr(erp, "make_payment_request", lambda **args: env.made.append(FakePR(None, args, saved=True))
                        or env.made[-1])
    _request(env)
    assert [c for c, _ in env.made[0].calls] == ["save", "submit"]


def test_the_endpoints_are_in_the_permission_matrix():
    import mpsa_suite.permission_matrix as matrix

    for name in ("collect_options", "request_payment"):
        who, evidence = matrix.ENDPOINTS[f"mpsa_suite.money_in.collect.{name}"]
        assert "COLLECT_ROLES" in who and set(evidence) == {"roles:COLLECT_ROLES", "perm"}
