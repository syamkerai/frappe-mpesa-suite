"""Bench tests: callback results reach the right request through the real request parsing and
the real database (findings C04, C26, C18, C03).

C04: Frappe builds a JSON POST's arguments from the body only, so ``?ir=`` on the ResultURL
never reached c2b_verification_result. C26: a ConversationID of "%" in an unauthenticated body
was a LIKE wildcard. C18: Transaction Status ResultCode 0 with a Declined receipt was
registered. C03: a prompt sent again on a request whose first prompt settled as Failed had
its callback dropped.
"""

import json
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.mpsa_suite.api import m_pesa_api
from mpsa_suite.money_in import c2b_verify, stk_verify
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_in.common import EXPRESS, REGISTER

C2B_RESULT_METHOD = c2b_verify.RESULT_METHOD


def unique_id(prefix: str) -> str:
    return f"{prefix}_{frappe.generate_hash(length=16)}"


def status_result(transid, amount, conversation_id, credit=t.SHORTCODE, status="Completed"):
    return {
        "ResultType": 0,
        "ResultCode": 0,
        "ResultDesc": "The service request is processed successfully.",
        "ConversationID": conversation_id,
        "OriginatorConversationID": f"OC-{conversation_id}",
        "ResultParameters": {
            "ResultParameter": [
                {"Key": "DebitPartyName", "Value": "254708374149 - TEST PAYER"},
                {"Key": "CreditPartyName", "Value": f"{credit} - Test Co"},
                {"Key": "TransactionStatus", "Value": status},
                {"Key": "Amount", "Value": f"{amount:.2f}"},
                {"Key": "ReceiptNo", "Value": transid},
            ]
        },
    }


class TestCallbackFixes(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(verify_c2b_confirmations=1, verify_stk_callbacks=1)
        frappe.db.set_value(
            "Mpesa Settings", t.SETTINGS, {"initiator_name": "testapi", "security_credential": "dummy"}
        )

    @classmethod
    def tearDownClass(cls):
        t.enable_money_in()  # verification off again for the other suites
        super().tearDownClass()

    def setUp(self):
        frappe.set_user("Administrator")

    # ------------------------------------------------------------ helpers
    def post_json(self, method: str, query: str, body: dict):
        """POST ``body`` as application/json the way Safaricom does, through Frappe's own
        make_form_dict and frappe.call (what handler.execute_cmd runs)."""
        from frappe.app import make_form_dict
        from werkzeug.test import EnvironBuilder
        from werkzeug.wrappers import Request

        request = Request(
            EnvironBuilder(path=f"/api/method/{method}", method="POST", query_string=query, json=body).get_environ()
        )
        had_request = hasattr(frappe.local, "request")
        saved_request = getattr(frappe.local, "request", None)
        saved_form = frappe.local.form_dict
        frappe.local.request = request
        try:
            make_form_dict(request)
            arguments = dict(frappe.local.form_dict)
            with patch("frappe.enqueue") as enqueue:
                answer = frappe.call(method, **frappe.local.form_dict)
            return arguments, answer, enqueue
        finally:
            frappe.local.form_dict = saved_form
            if had_request:
                frappe.local.request = saved_request
            else:
                del frappe.local.request

    def held(self, amount, ack_lost=False):
        payload = t.confirmation_payload(amount)
        ack = {"ResponseCode": "0", "ConversationID": unique_id("AG"), "OriginatorConversationID": unique_id("OC")}
        answer = (None, "unknown") if ack_lost else (ack, None)  # what post gives when the ack never came
        with patch("mpsa_suite.money_out.common.post", return_value=answer):
            ir = c2b_verify.hold(payload)
        self.assertTrue(ir)
        return payload, ir, ack

    def status_query(self, receipt):
        """A completed 'Fetch transaction' query, as trigger_transaction_status leaves it."""
        conversation_id = unique_id("AG")
        doc = frappe.get_doc(
            {
                "doctype": "Integration Request",
                "integration_request_service": "Mpesa Transaction Status",
                "reference_doctype": REGISTER,
                "status": "Completed",
                "data": json.dumps({"mpesa_settings": t.SETTINGS, "transaction_id": receipt}),
                "output": json.dumps(
                    {"OriginatorConversationID": f"OC-{conversation_id}", "ConversationID": conversation_id,
                     "ResponseCode": "0"}
                ),
            }
        ).insert(ignore_permissions=True)
        return doc.name, conversation_id

    def status_callback(self, result) -> str:
        """Store a Transaction Status result as handle_transaction_status_result does, then process it."""
        doc = frappe.get_doc(
            {
                "doctype": "Integration Request",
                "integration_request_service": "Mpesa Transaction Status Result Callback",
                "reference_doctype": REGISTER,
                "status": "Queued",
                "data": json.dumps({"Result": result}),
            }
        ).insert(ignore_permissions=True)
        m_pesa_api.process_mpesa_integration_request(doc.name)
        return doc.name

    # ------------------------------------------------------------ C04
    def test_c2b_verification_result_posted_as_json_reaches_the_held_confirmation(self):
        # the query's ack was lost: no stored ID can find the held confirmation, only ?ir= can
        payload, ir, _ack = self.held(300, ack_lost=True)
        self.assertFalse(json.loads(frappe.db.get_value("Integration Request", ir, "output")).get("conversation_id"))
        result = status_result(payload["transid"], 300, unique_id("AG"))
        _arguments, answer, enqueue = self.post_json(C2B_RESULT_METHOD, "t=token", {"Result": result})
        self.assertEqual(answer["ResultCode"], 1)
        enqueue.assert_not_called()

        arguments, answer, enqueue = self.post_json(C2B_RESULT_METHOD, f"ir={ir}&t=token", {"Result": result})
        self.assertNotIn("ir", arguments, "Frappe drops the query string of a JSON POST")
        self.assertEqual(answer, {"ResultCode": 0, "ResultDesc": "Accepted"})
        queued = enqueue.call_args.kwargs
        self.assertEqual(queued["ir"], ir)
        self.assertEqual(c2b_verify.apply_result(queued["ir"], queued["result"]), "Verified")
        self.assertTrue(frappe.db.exists(REGISTER, {"transid": payload["transid"]}))

    def test_c2b_verification_result_without_ir_is_matched_by_conversation_id(self):
        payload, ir, ack = self.held(300)
        self.held(300)  # another held confirmation, newer
        result = status_result(payload["transid"], 300, ack["ConversationID"])
        _arguments, answer, enqueue = self.post_json(C2B_RESULT_METHOD, "t=token", {"Result": result})
        self.assertEqual(answer["ResultCode"], 0)
        self.assertEqual(enqueue.call_args.kwargs["ir"], ir)

    def test_c2b_verification_result_with_wildcard_id_matches_nothing(self):
        self.held(300)
        forged = {"ResultCode": 0, "ConversationID": "%", "OriginatorConversationID": "%"}
        _arguments, answer, enqueue = self.post_json(C2B_RESULT_METHOD, "t=token", {"Result": forged})
        self.assertEqual(answer["ResultCode"], 1)
        enqueue.assert_not_called()

    # ------------------------------------------------------------ C26 / C18: Transaction Status
    def test_forged_status_result_with_wildcard_conversation_id_is_not_registered(self):
        receipt = "Q" + frappe.generate_hash(length=9).upper()
        _query, conversation_id = self.status_query(receipt)
        for forged_id in ("%", conversation_id[:-2]):  # wildcard, and a LIKE-matching prefix
            name = self.status_callback(status_result(receipt, 95000, forged_id))
            self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Failed")
            self.assertFalse(frappe.db.exists(REGISTER, {"transid": receipt}))
        # the answer to the query that was really sent still passes the check
        params = {"TransactionStatus": "Completed"}
        self.assertIsNone(
            m_pesa_api._unsolicited_status_result({"ConversationID": conversation_id}, params, receipt, t.SHORTCODE)
        )

    def test_declined_receipt_from_transaction_status_is_not_registered(self):
        receipt = "Q" + frappe.generate_hash(length=9).upper()
        _query, conversation_id = self.status_query(receipt)
        name = self.status_callback(status_result(receipt, 500, conversation_id, status="Declined"))
        self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Failed")
        self.assertIn("not Completed", frappe.db.get_value("Integration Request", name, "output"))
        self.assertFalse(frappe.db.exists(REGISTER, {"transid": receipt}))

    # ------------------------------------------------------------ C26: Account Balance
    def test_forged_balance_result_leaves_held_confirmations_alone(self):
        _payload, ir, _ack = self.held(300)
        before = frappe.db.get_value("Integration Request", ir, ["status", "output"], as_dict=True)
        for forged_id in ("attempts", "%"):
            m_pesa_api.account_balance_result(Result={"ResultCode": 0, "ConversationID": forged_id})
            after = frappe.db.get_value("Integration Request", ir, ["status", "output"], as_dict=True)
            self.assertEqual(after, before)
        # held confirmations still take Safaricom's real answer
        self.assertEqual(frappe.db.get_value("Integration Request", ir, "status"), "Queued")

    def test_balance_result_is_recorded_only_for_our_balance_query(self):
        conversation_id = unique_id("AG")
        ack = {"OriginatorConversationID": f"OC-{conversation_id}", "ConversationID": conversation_id, "ResponseCode": "0"}
        query = frappe.get_doc(
            {
                "doctype": "Integration Request",
                "integration_request_service": "Mpesa",
                "request_description": "Mpesa Balance Query",
                "reference_doctype": "Mpesa Settings",
                "reference_docname": t.SETTINGS,
                "status": "Completed",
                "output": str(ack),  # what the connector logs
            }
        ).insert(ignore_permissions=True)
        result = {"ResultCode": 0, "ConversationID": conversation_id, "ResultParameters": {"ResultParameter": [
            {"Key": "BOCompletedTime", "Value": 20261020120000}]}}
        m_pesa_api.account_balance_result(Result=result)  # no AccountBalance: nothing changes
        self.assertEqual(frappe.db.get_value("Integration Request", query.name, "output"), str(ack))

        balance = "Working Account|KES|481000.00|481000.00|0.00|0.00"
        result["ResultParameters"]["ResultParameter"].append({"Key": "AccountBalance", "Value": balance})
        m_pesa_api.account_balance_result(Result=result)
        self.assertEqual(frappe.db.get_value("Mpesa Settings", t.SETTINGS, "account_balance"), balance)
        self.assertIn(conversation_id, frappe.db.get_value("Integration Request", query.name, "output"))

    # ------------------------------------------------------------ C03: prompt sent again
    def _prompt(self, amount=100):
        doc = frappe.get_doc(
            {
                "doctype": EXPRESS,
                "settings": t.SETTINGS,
                "phone_number": "254708374149",
                "currency": "KES",
                "base_amount": amount,
                "status": "In Progress",
                "checkout_request_id": unique_id("ws_CO"),
            }
        ).insert(ignore_permissions=True)
        self.assertTrue(doc.payment_gateway, "the test Mpesa Settings needs its Payment Gateway")
        return doc

    def _settle_failed(self, req):
        cancelled = {"MerchantRequestID": "1", "CheckoutRequestID": req.checkout_request_id,
                     "ResultCode": 1032, "ResultDesc": "Request cancelled by user"}
        frappe.db.set_value(EXPRESS, req.name, {"callback_payload": json.dumps(cancelled), "verification_status": "Pending"})
        with patch("mpsa_suite.money_in.stk_poll.stk_query", return_value={"ResultCode": "1032"}):
            self.assertEqual(stk_verify.settle(req.name), "Failed")

    def _prompt_again(self, req):
        new_id = unique_id("ws_CO")

        def accepted(**kw):
            ack = {"MerchantRequestID": "2", "CheckoutRequestID": new_id, "ResponseCode": "0",
                   "ResponseDescription": "Success. Request accepted for processing", "CustomerMessage": "ok"}
            kw["success_callback"](response=ack, payload=kw["payload"], document_name=kw["document_name"],
                                   doctype=kw["doctype"], settings_name=kw["settings_name"])
            return ack

        with patch.object(m_pesa_api, "process_request", side_effect=accepted) as process:
            frappe.get_doc(EXPRESS, req.name).initiate_request()
        process.assert_called_once()
        return new_id

    def _paid_callback(self, checkout_request_id, amount=100):
        receipt = "S" + frappe.generate_hash(length=9).upper()
        cb = {"MerchantRequestID": "2", "CheckoutRequestID": checkout_request_id, "ResultCode": 0, "ResultDesc": "ok",
              "CallbackMetadata": {"Item": [
                  {"Name": "Amount", "Value": amount},
                  {"Name": "MpesaReceiptNumber", "Value": receipt},
                  {"Name": "TransactionDate", "Value": 20261020120000},
                  {"Name": "PhoneNumber", "Value": 254708374149},
              ]}}
        with patch("mpsa_suite.money_in.stk_verify.queue") as queue:
            m_pesa_api.stk_push_callback(Body={"stkCallback": cb})
        return receipt, queue

    def test_prompt_sent_again_after_a_failed_one_is_settled_and_booked(self):
        req = self._prompt()
        self._settle_failed(req)
        new_id = self._prompt_again(req)
        row = frappe.db.get_value(
            EXPRESS, req.name, ["checkout_request_id", "status", "verification_status", "callback_payload"], as_dict=True
        )
        self.assertEqual((row.checkout_request_id, row.status), (new_id, "In Progress"))
        self.assertFalse(row.verification_status or row.callback_payload)

        receipt, queue = self._paid_callback(new_id)
        queue.assert_called_once_with(req.name)
        with patch("mpsa_suite.money_in.stk_poll.stk_query", return_value={"ResultCode": "0"}), \
                patch("mpsa_suite.money_in.stk_verify.book") as book:
            self.assertEqual(stk_verify.settle(req.name), "Verified")
        book.assert_called_once_with(req.name)
        row = frappe.db.get_value(EXPRESS, req.name, ["status", "transaction_id"], as_dict=True)
        self.assertEqual((row.status, row.transaction_id), ("Completed", receipt))

    def test_callback_for_a_prompt_sent_again_before_the_reset_is_still_taken(self):
        # a request prompted again before this fix: Failed / Verified with the old prompt's callback
        req = self._prompt()
        self._settle_failed(req)
        new_id = unique_id("ws_CO")
        frappe.db.set_value(EXPRESS, req.name, "checkout_request_id", new_id)
        _receipt, queue = self._paid_callback(new_id)
        queue.assert_called_once_with(req.name)
        self.assertEqual(frappe.db.get_value(EXPRESS, req.name, "verification_status"), "Pending")

        # ...while Safaricom re-sending the callback it already settled is still ignored
        req2 = self._prompt()
        self._settle_failed(req2)
        with patch("mpsa_suite.money_in.stk_verify.queue") as queue:
            m_pesa_api.stk_push_callback(
                Body={"stkCallback": json.loads(frappe.db.get_value(EXPRESS, req2.name, "callback_payload"))}
            )
        queue.assert_not_called()

    def test_prompt_accepted_after_the_earlier_one_was_paid_keeps_the_payment(self):
        # the guard passes; while Safaricom answers, the earlier prompt's callback is settled as paid
        req = self._prompt()
        old_id = req.checkout_request_id
        five_minutes_ago = add_to_date(now_datetime(), minutes=-5)
        frappe.db.set_value(EXPRESS, req.name, "timestamp", five_minutes_ago)
        receipt = "S" + frappe.generate_hash(length=9).upper()

        def accepted(**kw):
            frappe.db.set_value(EXPRESS, req.name, {"status": "Completed", "verification_status": "Verified",
                                                    "transaction_id": receipt, "is_reconciled": 1})
            ack = {"MerchantRequestID": "2", "CheckoutRequestID": unique_id("ws_CO"), "ResponseCode": "0"}
            kw["success_callback"](response=ack, payload=kw["payload"], document_name=kw["document_name"],
                                   doctype=kw["doctype"], settings_name=kw["settings_name"])
            return ack

        with patch.object(m_pesa_api, "process_request", side_effect=accepted), \
                patch.object(frappe, "log_error") as log_error:
            frappe.get_doc(EXPRESS, req.name).initiate_request()
        row = frappe.db.get_value(
            EXPRESS, req.name, ["status", "verification_status", "transaction_id", "checkout_request_id"], as_dict=True
        )
        self.assertEqual((row.status, row.verification_status, row.transaction_id), ("Completed", "Verified", receipt))
        self.assertEqual(row.checkout_request_id, old_id)
        self.assertTrue(any("not tracked" in str(c.kwargs.get("title")) for c in log_error.call_args_list))

    def test_prompt_refused_while_the_last_one_is_still_on_the_phone(self):
        req = self._prompt()
        frappe.db.set_value(EXPRESS, req.name, "timestamp", now_datetime())
        with patch.object(m_pesa_api, "process_request") as process, self.assertRaises(frappe.ValidationError):
            frappe.get_doc(EXPRESS, req.name).initiate_request()
        process.assert_not_called()

    def test_prompt_refused_while_the_last_callback_is_undecided_or_paid(self):
        req = self._prompt()
        for values in ({"verification_status": "Pending"}, {"verification_status": "Mismatch"},
                       {"verification_status": "Unverified"}, {"verification_status": "Verified", "status": "Completed"}):
            frappe.db.set_value(EXPRESS, req.name, {"status": "Failed", **values})
            with patch.object(m_pesa_api, "process_request") as process, self.assertRaises(frappe.ValidationError):
                frappe.get_doc(EXPRESS, req.name).initiate_request()
            process.assert_not_called()
