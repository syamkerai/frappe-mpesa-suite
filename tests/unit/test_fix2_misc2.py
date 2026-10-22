"""Round 2 "misc2" fixes, on an in-memory frappe.

C08: the after_migrate index hook removes (never recreates) the search_index Property Setter on
a text field, keeps the index and the setters on indexable fields. C2B verification: a result
whose IDs are lists or dicts is ignored instead of crashing the job. C09: a prepaid order whose
release fails with an unexpected error (not ERPNext's refusal) keeps its traceback in the Error
Log, once per error, and its Match Log row does not blame the credit limit. That includes the
ValidationError subclasses a buggy hook raises (DoesNotExistError ...); a missing permission, a
mandatory field or a workflow refusal is a refusal: no Error Log, and a hint that fits it.
"""

import importlib
import json
import sys
import traceback
import types

import pytest

import fake_frappe

PS = "Property Setter"
SUITE = "M-Pesa Suite Settings"
IR = "Integration Request"
HELD = "Mpesa C2B Verification"
SETTINGS = "TESTC2B"
SHORTCODE = "174379"
FIELDTYPES = {
    ("Bank Transaction", "reference_number"): "Small Text",
    ("Bank Transaction", "transaction_id"): "Data",
    ("Payment Entry", "reference_no"): "Data",
    ("Integration Request", "request_id"): "Data",
}


@pytest.fixture
def frappe(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        yield frappe
    finally:
        fake_frappe.uninstall(saved)


# ---------------------------------------------------------------- C08 lookup indexes
@pytest.fixture
def install_env(frappe, monkeypatch):
    calls = fake_frappe._dict(made=[], indexed=[], cleared=[])

    def get_meta(doctype):
        fields = {f: t for (d, f), t in FIELDTYPES.items() if d == doctype}
        field = lambda name: fake_frappe._dict(fieldname=name, fieldtype=fields[name]) if name in fields else None
        return types.SimpleNamespace(get_field=field, has_field=lambda name: name in fields)

    def make_property_setter(doctype, fieldname, prop, value, property_type, validate_fields_for_doctype=True):
        calls.made.append((doctype, fieldname))
        name = f"{doctype}-{fieldname}-{prop}"
        frappe.db.table(PS)[name] = {"name": name, "doc_type": doctype, "field_name": fieldname,
                                     "property": prop, "value": value}

    def delete(doctype, filters):
        table = frappe.db.table(doctype)
        for row in frappe.db._rows(doctype, filters):
            table.pop(row["name"])

    frappe.get_meta = get_meta
    frappe.clear_cache = lambda doctype=None: calls.cleared.append(doctype)
    frappe.db.delete = delete
    frappe.db.add_index = lambda doctype, fields, index_name=None: calls.indexed.append((doctype, index_name))
    for doctype in {d for d, _ in FIELDTYPES}:
        frappe.db.table("DocType")[doctype] = {"name": doctype}
    module = types.ModuleType("frappe.custom.doctype.property_setter.property_setter")
    module.make_property_setter = make_property_setter
    for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.property_setter"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    calls.install = importlib.import_module("mpsa_suite.install")
    return calls


def setter(frappe, doctype, fieldname):
    return frappe.db.exists(PS, {"doc_type": doctype, "field_name": fieldname, "property": "search_index"})


def test_index_hook_removes_the_text_field_setter_left_by_an_earlier_release(frappe, install_env):
    # the patch already ran (or is logged as done): the setter is back, as on test15/test16
    name = "Bank Transaction-reference_number-search_index"
    frappe.db.table(PS)[name] = {"name": name, "doc_type": "Bank Transaction", "field_name": "reference_number",
                                 "property": "search_index", "value": 1}

    install_env.install.ensure_lookup_indexes()

    assert not setter(frappe, "Bank Transaction", "reference_number")
    assert install_env.cleared == ["Bank Transaction"]  # meta rebuilt without search_index
    assert ("Bank Transaction", "reference_number") not in install_env.made
    # the index is still built for every field; the Data fields keep (get) their setters
    assert sorted(install_env.indexed) == sorted((d, f) for d, f in FIELDTYPES)
    for doctype, fieldname in FIELDTYPES:
        if FIELDTYPES[(doctype, fieldname)] == "Data":
            assert setter(frappe, doctype, fieldname)
    assert frappe.errors == []


def test_index_hook_never_creates_a_text_field_setter_and_is_idempotent(frappe, install_env):
    install_env.install.ensure_lookup_indexes()  # fresh install: after_install runs it after the patches
    install_env.install.ensure_lookup_indexes()  # and every migrate after that

    assert not setter(frappe, "Bank Transaction", "reference_number")
    assert install_env.made == [d_f for d_f, t in FIELDTYPES.items() if t == "Data"]  # each once
    assert install_env.cleared == []  # nothing removed, no cache churn on each migrate
    assert len(install_env.indexed) == 2 * len(FIELDTYPES)


def test_index_hook_failure_on_one_field_does_not_stop_the_others(frappe, install_env):
    def add_index(doctype, fields, index_name=None):
        if doctype == "Bank Transaction":
            raise RuntimeError("lock wait timeout")
        install_env.indexed.append((doctype, index_name))

    frappe.db.add_index = add_index
    install_env.install.ensure_lookup_indexes()
    assert install_env.indexed == [("Payment Entry", "reference_no"), ("Integration Request", "request_id")]
    assert len(frappe.errors) == 2  # logged, the migrate carries on


# ---------------------------------------------------------------- C2B verification result
@pytest.fixture
def c2b(frappe, monkeypatch):
    stub = types.ModuleType("mpsa_suite.utils.encoding_initiator_password")
    stub.generate_security_credential = lambda *a, **kw: ""
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    frappe.db.table("Mpesa Settings")[SETTINGS] = {"name": SETTINGS, "business_shortcode": SHORTCODE,
                                                   "till_number": ""}
    state = {"attempts": 1, "settings": SETTINGS, "conversation_id": "AG_20261021_c2b0001",
             "originator_conversation_id": "OC-20261021-0001"}
    frappe.db.table(IR)["IR-HELD"] = {
        "name": "IR-HELD", "integration_request_service": HELD, "status": "Queued", "request_id": "RKL51ZDR4F",
        "data": json.dumps({"transid": "RKL51ZDR4F", "businessshortcode": SHORTCODE, "transamount": 300}),
        "output": json.dumps(state),
    }
    return importlib.import_module("mpsa_suite.money_in.c2b_verify")


@pytest.mark.parametrize(
    "forged",
    [
        {"ConversationID": ["AG_20261021_c2b0001"], "OriginatorConversationID": None},
        {"ConversationID": {"id": "AG_20261021_c2b0001"}, "OriginatorConversationID": ["OC-20261021-0001"]},
        {"ConversationID": "%", "OriginatorConversationID": {"$ne": ""}},
    ],
)
def test_forged_unhashable_ids_are_ignored_not_raised(frappe, c2b, forged):
    result = {"ResultCode": 0, "ResultParameters": {"ResultParameter": [{"Key": "ReceiptNo", "Value": "RKL51ZDR4F"}]},
              **forged}
    before = dict(frappe.db.table(IR)["IR-HELD"])
    assert c2b.apply_result("IR-HELD", result) == "ignored"
    assert frappe.db.table(IR)["IR-HELD"] == before  # nothing recorded, still held


def test_result_that_is_not_ours_is_ignored_before_its_body_is_parsed(frappe, c2b):
    result = {"ResultCode": 0, "ConversationID": "AG_20261021_other", "ResultParameters": ["not a dict"]}
    assert c2b.apply_result("IR-HELD", result) == "ignored"
    assert frappe.db.table(IR)["IR-HELD"]["status"] == "Queued"


def test_one_real_id_beside_a_forged_one_still_answers_the_query(frappe, c2b):
    result = {"ResultCode": 1, "ResultDesc": "busy", "ConversationID": ["junk"],
              "OriginatorConversationID": " OC-20261021-0001 "}
    assert c2b.apply_result("IR-HELD", result) == "Pending"
    assert json.loads(frappe.db.table(IR)["IR-HELD"]["output"])["outcome"] == "Pending"


# ---------------------------------------------------------------- C09 release faults
@pytest.fixture
def orders(frappe, monkeypatch):
    frappe.utils.strip_html = lambda text: text.replace("<br>", " ")
    frappe.get_roles = lambda user=None: []
    frappe.get_traceback = lambda *a, **kw: traceback.format_exc()
    frappe.db.table(SUITE)[SUITE] = {"name": SUITE, "enable_sales_order_payments": 1, "amount_tolerance": 1,
                                     "sales_order_approver": "approver@example.com"}
    frappe.db.table("Accounts Settings")["Accounts Settings"] = {"credit_controller": "Credit Controller"}
    frappe.get_cached_doc = lambda doctype, *a, **kw: frappe.get_doc(doctype, doctype)
    frappe.db.rollback = lambda save_point=None: None
    frappe.local.message_log = []

    def log_error(title=None, message=None, reference_doctype=None, reference_name=None, **kw):
        name = f"ERR-{len(frappe.db.table('Error Log')) + 1}"
        frappe.db.table("Error Log")[name] = {"name": name, "method": title, "error": message,
                                              "reference_doctype": reference_doctype, "reference_name": reference_name}

    frappe.log_error = log_error
    workflow = types.ModuleType("frappe.model.workflow")
    workflow.get_workflow_name = lambda doctype: None
    workflow.get_transitions = lambda doc: []
    workflow.apply_workflow = lambda doc, action: None
    for name in ("WorkflowStateError", "WorkflowTransitionError", "WorkflowPermissionError"):
        setattr(workflow, name, type(name, (frappe.ValidationError,), {}))  # as in frappe.model.workflow
    monkeypatch.setitem(sys.modules, "frappe.model.workflow", workflow)
    frappe.MandatoryError = type("MandatoryError", (frappe.ValidationError,), {})
    mod = importlib.import_module("mpsa_suite.money_in.sales_order")
    logged = []
    monkeypatch.setattr(mod, "log_match", lambda *a, **kw: logged.append(kw["details"]))
    frappe.db.table("Sales Order")["SO-1"] = {"name": "SO-1", "docstatus": 0, "status": "Draft",
                                              "customer": "CUST-1", "grand_total": 1000.0}
    return fake_frappe._dict(sales_order=mod, logged=logged, workflow=workflow)


def fail_with(frappe, exc):
    def submit():
        raise exc

    frappe.db.table("Sales Order")["SO-1"]["submit"] = submit


def test_release_fault_keeps_its_traceback_and_does_not_blame_the_credit_limit(frappe, orders):
    fail_with(frappe, KeyError("custom_field_x"))
    orders.sales_order.release_order("SO-1")  # contained: the receipt match stays

    [error] = frappe.db.table("Error Log").values()
    assert error["method"] == "M-Pesa paid order release failed for SO-1"
    assert error["reference_doctype"] == "Sales Order" and error["reference_name"] == "SO-1"
    assert "Traceback" in error["error"] and "KeyError: 'custom_field_x'" in error["error"]
    [details] = orders.logged
    assert orders.sales_order.REFUSED in details  # the backoff still holds the retries
    assert "KeyError: 'custom_field_x'" in details and "Error Log" in details
    assert "Credit Controller" not in details and "credit limit" not in details.lower()


def test_release_fault_is_logged_once_per_error(frappe, orders):
    fail_with(frappe, KeyError("custom_field_x"))
    orders.sales_order.release_order("SO-1")
    orders.sales_order.release_order("SO-1")  # retried after the backoff: same fault
    assert len(frappe.db.table("Error Log")) == 1
    assert orders.logged[0] == orders.logged[1]  # same row, only marked as tried again

    fail_with(frappe, AttributeError("'NoneType' object has no attribute 'rate'"))
    orders.sales_order.release_order("SO-1")
    assert len(frappe.db.table("Error Log")) == 2  # a different fault is recorded


def test_refusal_by_erpnext_writes_no_error_log_and_keeps_the_hint(frappe, orders):
    fail_with(frappe, frappe.ValidationError("Credit limit has been crossed for customer CUST-1 (2000/100)"))
    orders.sales_order.release_order("SO-1")
    assert frappe.db.table("Error Log") == {}
    [details] = orders.logged
    assert "Credit limit has been crossed" in details
    assert "give approver@example.com the 'Credit Controller' role" in details
    assert "ValidationError" not in details


# frappe.exceptions on v15 and v16: these subclass ValidationError, and a buggy hook raises them
class DoesNotExistError(fake_frappe.ValidationError):
    pass


class LinkValidationError(fake_frappe.ValidationError):
    pass


@pytest.mark.parametrize(
    "exc,text",
    [
        (DoesNotExistError("Item Price None not found"), "DoesNotExistError: Item Price None not found"),
        (LinkValidationError("Could not find Row #1: Warehouse: Stores - X"), "LinkValidationError: Could not find"),
    ],
)
def test_validation_error_subclass_from_a_hook_keeps_its_traceback(frappe, orders, exc, text):
    fail_with(frappe, exc)
    orders.sales_order.release_order("SO-1")

    [error] = frappe.db.table("Error Log").values()
    assert error["method"] == "M-Pesa paid order release failed for SO-1" and error["reference_name"] == "SO-1"
    assert "Traceback" in error["error"] and text in error["error"]
    [details] = orders.logged
    assert orders.sales_order.REFUSED in details and text in details
    assert " The traceback is in the Error Log." in details
    assert "Credit Controller" not in details and "credit limit" not in details.lower()
    assert "not a refusal" not in details  # it may be an app's own refusal: say only where to look

    orders.sales_order.release_order("SO-1")  # retried after the backoff: same error, no second log
    assert len(frappe.db.table("Error Log")) == 1


def test_permission_refusal_writes_no_error_log_and_names_the_permission(frappe, orders):
    frappe.flags.error_message = "set by the request before the release"

    def submit():  # Document.raise_no_permission_to: the text goes to the flags, the error is bare
        frappe.flags.error_message = "You need the 'submit' permission on Sales Order SO-1 to perform this action."
        raise frappe.PermissionError

    frappe.db.table("Sales Order")["SO-1"]["submit"] = submit
    orders.sales_order.release_order("SO-1")

    assert frappe.db.table("Error Log") == {}
    [details] = orders.logged
    assert orders.sales_order.REFUSED in details
    assert "approver@example.com could not release it: You need the 'submit' permission on Sales Order SO-1" in details
    assert "Check that approver@example.com has a role that may submit Sales Orders" in details
    assert "not a refusal" not in details and "PermissionError" not in details
    assert "Credit Controller" not in details
    assert frappe.flags.error_message == "set by the request before the release"  # restored, not leaked


def test_permission_error_without_text_does_not_borrow_an_earlier_message(frappe, orders):
    frappe.flags.error_message = "You need the 'write' permission on Sales Order SO-OLD to perform this action."
    fail_with(frappe, frappe.PermissionError())
    orders.sales_order.release_order("SO-1")

    [details] = orders.logged
    assert "SO-OLD" not in details
    assert "Check that approver@example.com has a role that may submit Sales Orders" in details
    assert frappe.db.table("Error Log") == {}
    assert "SO-OLD" in frappe.flags.error_message  # the caller's flag is left as it was


def test_successful_release_leaves_the_flags_alone(frappe, orders):
    frappe.flags.error_message = "earlier"
    frappe.db.table("Sales Order")["SO-1"]["submit"] = lambda: None
    orders.sales_order.release_order("SO-1")
    assert frappe.flags.error_message == "earlier" and frappe.db.table("Error Log") == {}


@pytest.mark.parametrize(
    "kind,text,hint",
    [
        ("MandatoryError", "[Sales Order, SO-1]: delivery_date", "Fill in the missing fields on the order."),
        ("WorkflowStateError", "Workflow State not set", "Check the workflow actions and that approver@example.com"),
        ("WorkflowPermissionError", "Not allowed to change to Approved", "Check the workflow actions"),
    ],
)
def test_mandatory_and_workflow_refusals_get_their_own_hint_and_no_error_log(frappe, orders, kind, text, hint):
    exc = getattr(frappe, kind, None) or getattr(orders.workflow, kind)
    fail_with(frappe, exc(text))
    orders.sales_order.release_order("SO-1")

    assert frappe.db.table("Error Log") == {}
    [details] = orders.logged
    assert f"could not release it: {text}" in details and hint in details
    assert kind not in details and "Error Log" not in details
    assert "Credit Controller" not in details and "credit limit" not in details.lower()
