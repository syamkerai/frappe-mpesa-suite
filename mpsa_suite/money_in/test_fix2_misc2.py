"""Round 2 "misc2" fixes on a real database.

C08: the after_migrate index hook on its own (the cleanup patch already logged) removes the
Bank Transaction Reference Number search_index setter and keeps the index. C2B verification: a
result whose IDs are lists or dicts is ignored, and a real ID beside one still counts. C09: a
prepaid order whose release fails with an unexpected error keeps its traceback in the Error Log
(once per error) and stays a draft with its receipt matched; a DoesNotExistError from a hook
counts as such an error, while an approver without permission is a refusal that names the
permission, with no Error Log.
"""

import json
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import c2b_verify, matching, sales_order
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_in.common import REGISTER

MATCH_LOG = "Mpesa Match Log"
IR = "Integration Request"
SETTER = {"doc_type": "Bank Transaction", "field_name": "reference_number", "property": "search_index"}


def _status_result(transid: str, amount: float, conversation_id, originator_id) -> dict:
    return {
        "ResultType": 0,
        "ResultCode": 0,
        "ResultDesc": "The service request is processed successfully.",
        "ConversationID": conversation_id,
        "OriginatorConversationID": originator_id,
        "ResultParameters": {
            "ResultParameter": [
                {"Key": "DebitPartyName", "Value": "254708374149 - TEST PAYER"},
                {"Key": "CreditPartyName", "Value": f"{t.SHORTCODE} - Test Co"},
                {"Key": "TransactionStatus", "Value": "Completed"},
                {"Key": "Amount", "Value": f"{amount:.2f}"},
                {"Key": "ReceiptNo", "Value": transid},
            ]
        },
    }


class TestFix2Misc2(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in()
        cls.customer = t.customer("_Test M-Pesa Customer")

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None

    # ------------------------------------------------------------ C08 index hook
    def test_index_hook_alone_removes_the_text_field_setter(self):
        from frappe.core.doctype.doctype.doctype import validate_fields_for_doctype
        from frappe.custom.doctype.property_setter.property_setter import make_property_setter

        from mpsa_suite import install

        for doctype, fieldname in install.LOOKUP_INDEXES:
            # add_index commits before building a missing index; that would commit test data
            if frappe.db.exists("DocType", doctype) and not frappe.db.has_index(f"tab{doctype}", fieldname):
                self.skipTest(f"{doctype}.{fieldname} has no index on this site")
        self.assertEqual(frappe.get_meta("Bank Transaction").get_field("reference_number").fieldtype, "Small Text")

        # the one-shot patch is already logged (test15/test16 were in this state): only the hook runs
        if not frappe.db.exists("Property Setter", SETTER):
            make_property_setter("Bank Transaction", "reference_number", "search_index", 1, "Check",
                                 validate_fields_for_doctype=False)
        frappe.clear_cache(doctype="Bank Transaction")
        self.assertRaises(frappe.ValidationError, validate_fields_for_doctype, "Bank Transaction")

        install.ensure_lookup_indexes()
        install.ensure_lookup_indexes()  # every later migrate: nothing to do, nothing recreated

        self.assertFalse(frappe.db.exists("Property Setter", SETTER))
        validate_fields_for_doctype("Bank Transaction")  # Custom Field / Customize Form saves work
        self.assertFalse(frappe.get_meta("Bank Transaction").get_field("reference_number").search_index)
        self.assertTrue(frappe.db.has_index("tabBank Transaction", "reference_number"), "index kept")
        for doctype, fieldname in install.LOOKUP_INDEXES:
            if fieldname != "reference_number" and frappe.db.exists("DocType", doctype):
                self.assertTrue(frappe.db.exists("Property Setter", dict(SETTER, doc_type=doctype, field_name=fieldname)))

    # ------------------------------------------------------------ C2B verification IDs
    def _held(self, amount: float) -> tuple:
        payload = t.confirmation_payload(amount)
        ir = frappe.get_doc(
            {
                "doctype": IR,
                "integration_request_service": c2b_verify.SERVICE,
                "is_remote_request": 1,
                "request_id": payload["transid"],
                "status": c2b_verify.QUEUED,
                "data": json.dumps(payload),
                "output": json.dumps({"attempts": 1, "settings": t.SETTINGS, "conversation_id": "AG_20261021_misc2",
                                      "originator_conversation_id": "OC-20261021-misc2"}),
            }
        ).insert(ignore_permissions=True)
        return payload, ir.name

    def test_forged_list_or_dict_ids_are_ignored(self):
        payload, ir = self._held(300)
        for cid, oid in ((["AG_20261021_misc2"], None), ({"id": "AG_20261021_misc2"}, ["OC-20261021-misc2"])):
            result = _status_result(payload["transid"], 300, cid, oid)
            self.assertEqual(c2b_verify.apply_result(ir, result), "ignored")
        self.assertEqual(frappe.db.get_value(IR, ir, "status"), c2b_verify.QUEUED)
        self.assertFalse(frappe.db.exists(REGISTER, {"transid": payload["transid"]}))

        # Safaricom's real answer, even beside a junk ID, still confirms the receipt
        result = _status_result(payload["transid"], 300, ["junk"], "OC-20261021-misc2")
        self.assertEqual(c2b_verify.apply_result(ir, result), "Verified")
        self.assertTrue(frappe.db.exists(REGISTER, {"transid": payload["transid"]}))

    # ------------------------------------------------------------ C09 release fault
    def test_release_fault_keeps_its_traceback_once(self):
        if frappe.db.exists("Workflow", {"document_type": "Sales Order", "is_active": 1}):
            self.skipTest("site has an active Sales Order workflow")
        so = t.sales_order(self.customer, 1000)
        title = f"M-Pesa paid order release failed for {so.name}"
        logs = frappe.db.count("Error Log", {"method": title})
        run = frappe.generate_hash(length=6)  # Error Logs may outlive the rollback; names are reused
        controller = type(frappe.get_doc("Sales Order", so.name))
        with patch.object(controller, "on_submit", side_effect=KeyError(f"custom_{run}"), create=True):
            name = t.receipt(1000, billref=so.name)
            matching.match_register(name)  # must not raise

        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 0)
        reg = frappe.db.get_value(REGISTER, name, ["docstatus", "matched_reference_name"], as_dict=True)
        self.assertEqual((reg.docstatus, reg.matched_reference_name), (0, so.name), "match kept for the retry")
        self.assertEqual(frappe.db.count("Error Log", {"method": title}), logs + 1)
        error = frappe.get_all("Error Log", filters={"method": title}, fields=["error", "reference_name"],
                               order_by="creation desc", limit=1)[0]
        self.assertIn(f"KeyError: 'custom_{run}'", error.error)
        self.assertIn("Traceback", error.error)
        self.assertEqual(error.reference_name, so.name)
        [log] = frappe.get_all(MATCH_LOG, filters={"reference_name": so.name, "result": "Error"},
                               fields=["name", "details"])
        self.assertIn(f"KeyError: 'custom_{run}'", log.details)
        self.assertIn(sales_order.REFUSED, log.details)
        self.assertNotIn("Credit Controller", log.details)

        # past the backoff the same fault comes back: tried again, but no second Error Log
        aged = add_to_date(now_datetime(), minutes=-10)
        frappe.db.set_value(MATCH_LOG, log.name, {"creation": aged, "modified": aged}, update_modified=False)
        with patch.object(controller, "on_submit", side_effect=KeyError(f"custom_{run}"), create=True):
            sales_order.process_pending_orders()
        self.assertEqual(frappe.db.count("Error Log", {"method": title}), logs + 1)
        self.assertEqual(len(frappe.get_all(MATCH_LOG, filters={"reference_name": so.name, "result": "Error"})), 1)
        self.assertGreater(frappe.db.get_value(MATCH_LOG, log.name, "modified"), aged)
        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 0)

    def test_does_not_exist_error_from_a_hook_keeps_its_traceback(self):
        if frappe.db.exists("Workflow", {"document_type": "Sales Order", "is_active": 1}):
            self.skipTest("site has an active Sales Order workflow")
        so = t.sales_order(self.customer, 1000)
        title = f"M-Pesa paid order release failed for {so.name}"
        logs = frappe.db.count("Error Log", {"method": title})
        run = frappe.generate_hash(length=6)
        controller = type(frappe.get_doc("Sales Order", so.name))
        bug = frappe.DoesNotExistError(f"Item Price {run} not found")  # a ValidationError subclass
        with patch.object(controller, "on_submit", side_effect=bug, create=True):
            name = t.receipt(1000, billref=so.name)
            matching.match_register(name)  # must not raise

        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 0)
        self.assertEqual(frappe.db.get_value(REGISTER, name, "matched_reference_name"), so.name)
        self.assertEqual(frappe.db.count("Error Log", {"method": title}), logs + 1)
        error = frappe.get_all("Error Log", filters={"method": title}, fields=["error"],
                               order_by="creation desc", limit=1)[0]
        self.assertIn(f"Item Price {run} not found", error.error)
        self.assertIn("Traceback", error.error)
        [log] = frappe.get_all(MATCH_LOG, filters={"reference_name": so.name, "result": "Error"}, fields=["details"])
        self.assertIn(f"DoesNotExistError: Item Price {run} not found", log.details)
        self.assertIn("The traceback is in the Error Log.", log.details)
        self.assertIn(sales_order.REFUSED, log.details)
        self.assertNotIn("Credit Controller", log.details)

    def test_release_without_permission_is_a_refusal_that_names_it(self):
        if frappe.db.exists("Workflow", {"document_type": "Sales Order", "is_active": 1}):
            self.skipTest("site has an active Sales Order workflow")
        so = t.sales_order(self.customer, 1000)
        title = f"M-Pesa paid order release failed for {so.name}"
        logs = frappe.db.count("Error Log", {"method": title})
        flag = frappe.flags.get("error_message")
        controller = type(frappe.get_doc("Sales Order", so.name))
        # the approver's roles do not allow it: Document.check_permission raises a bare PermissionError
        with patch.object(controller, "has_permission", lambda self, *a, **kw: False):
            name = t.receipt(1000, billref=so.name)
            matching.match_register(name)  # must not raise

        self.assertEqual(frappe.db.get_value("Sales Order", so.name, "docstatus"), 0)
        self.assertEqual(frappe.db.get_value(REGISTER, name, "matched_reference_name"), so.name)
        self.assertEqual(frappe.db.count("Error Log", {"method": title}), logs)
        [log] = frappe.get_all(MATCH_LOG, filters={"reference_name": so.name, "result": "Error"}, fields=["details"])
        self.assertRegex(log.details, rf"You need the '\w+' permission on Sales Order {so.name}")
        self.assertIn("has a role that may submit Sales Orders", log.details)
        self.assertIn(sales_order.REFUSED, log.details)
        self.assertNotIn("PermissionError", log.details)
        self.assertNotIn("not a refusal", log.details)
        self.assertEqual(frappe.flags.get("error_message"), flag)  # not left for the caller's response
