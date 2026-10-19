"""Bench tests: callbacks are confirmed with Safaricom before anything is booked (audit items 1, 4, 5)."""

import json
from unittest.mock import patch

import frappe

from frappe_mpsa_payments.compat import IntegrationTestCase
from frappe_mpsa_payments.money_in import c2b_verify, stk_verify
from frappe_mpsa_payments.money_in import testing as t
from frappe_mpsa_payments.money_in.common import EXPRESS, REGISTER, named_lock

ACK = {"ResponseCode": "0", "ConversationID": "AG_TEST_1", "OriginatorConversationID": "OC_TEST_1"}


def status_result(transid, amount, credit=t.SHORTCODE, status="Completed"):
    return {
        "ResultType": 0,
        "ResultCode": 0,
        "ResultDesc": "The service request is processed successfully.",
        "ConversationID": ACK["ConversationID"],
        "OriginatorConversationID": ACK["OriginatorConversationID"],
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


class TestCallbackVerification(IntegrationTestCase):
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

    # ------------------------------------------------------------ C2B
    def _held(self, amount):
        from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import delayed_insert_c2b

        payload = t.confirmation_payload(amount)
        with patch("frappe_mpsa_payments.money_out.common.post", return_value=(ACK, None)) as post:
            delayed_insert_c2b(payload)
        self.assertTrue(post.called, "a Transaction Status query should be sent")
        ir = frappe.db.get_value(
            "Integration Request", {"request_id": payload["transid"], "integration_request_service": c2b_verify.SERVICE}
        )
        self.assertTrue(ir)
        self.assertFalse(frappe.db.exists(REGISTER, {"transid": payload["transid"]}), "nothing booked before verification")
        return payload, ir

    def test_c2b_confirmation_booked_only_after_transaction_status(self):
        payload, ir = self._held(300)
        self.assertEqual(c2b_verify.apply_result(ir, status_result(payload["transid"], 300)), "Verified")
        self.assertTrue(frappe.db.exists(REGISTER, {"transid": payload["transid"]}))
        self.assertEqual(frappe.db.get_value("Integration Request", ir, "status"), "Completed")

    def test_forged_c2b_amount_is_never_registered(self):
        payload, ir = self._held(300)
        self.assertEqual(c2b_verify.apply_result(ir, status_result(payload["transid"], 30)), "Mismatch")
        self.assertFalse(frappe.db.exists(REGISTER, {"transid": payload["transid"]}))
        self.assertEqual(frappe.db.get_value("Integration Request", ir, "status"), "Failed")

    def test_result_for_another_query_is_ignored(self):
        payload, ir = self._held(300)
        forged = status_result(payload["transid"], 300)
        forged["ConversationID"] = forged["OriginatorConversationID"] = "AG_SOMEONE_ELSE"
        self.assertEqual(c2b_verify.apply_result(ir, forged), "ignored")
        self.assertFalse(frappe.db.exists(REGISTER, {"transid": payload["transid"]}))

    def test_unknown_shortcode_confirmation_is_dropped(self):
        from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import delayed_insert_c2b

        payload = {**t.confirmation_payload(300), "businessshortcode": "999999"}
        with patch("frappe_mpsa_payments.money_out.common.post") as post:
            delayed_insert_c2b(payload)
        self.assertFalse(post.called)
        self.assertFalse(frappe.db.exists(REGISTER, {"transid": payload["transid"]}))

    # ------------------------------------------------------------ STK
    def _prompt(self, amount=100):
        doc = frappe.get_doc(
            {
                "doctype": EXPRESS,
                "settings": t.SETTINGS,
                "phone_number": "254708374149",
                "currency": "KES",
                "base_amount": amount,
                "status": "In Progress",
                "checkout_request_id": "ws_CO_" + frappe.generate_hash(length=12),
            }
        ).insert(ignore_permissions=True)
        return doc

    def _callback(self, req, paid=True, amount=100):
        from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import stk_push_callback

        receipt = "S" + frappe.generate_hash(length=9).upper()
        cb = {"MerchantRequestID": "1", "CheckoutRequestID": req.checkout_request_id,
              "ResultCode": 0 if paid else 1032, "ResultDesc": "ok" if paid else "Request cancelled by user"}
        if paid:
            cb["CallbackMetadata"] = {"Item": [
                {"Name": "Amount", "Value": amount},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "TransactionDate", "Value": 20261018120000},
                {"Name": "PhoneNumber", "Value": 254708374149},
            ]}
        with patch("frappe_mpsa_payments.money_in.stk_verify.queue") as queue:
            stk_push_callback(Body={"stkCallback": cb})
        queue.assert_called_once_with(req.name)
        return receipt

    def test_stk_callback_records_nothing_until_settled(self):
        req = self._prompt()
        self._callback(req)
        row = frappe.db.get_value(EXPRESS, req.name, ["status", "transaction_id", "verification_status"], as_dict=True)
        self.assertEqual(row.status, "In Progress")
        self.assertFalse(row.transaction_id)
        self.assertEqual(row.verification_status, "Pending")

    def test_forged_stk_callback_is_not_booked(self):
        req = self._prompt()
        self._callback(req)
        with patch("frappe_mpsa_payments.money_in.stk_poll.stk_query", return_value={"ResultCode": "1032"}):
            self.assertEqual(stk_verify.settle(req.name), "Mismatch")
        row = frappe.db.get_value(EXPRESS, req.name, ["status", "transaction_id", "verification_status"], as_dict=True)
        self.assertEqual(row.verification_status, "Mismatch")
        self.assertNotEqual(row.status, "Completed")
        self.assertFalse(row.transaction_id)

    def test_confirmed_stk_callback_is_completed_with_receipt(self):
        req = self._prompt()
        receipt = self._callback(req)
        with patch("frappe_mpsa_payments.money_in.stk_poll.stk_query", return_value={"ResultCode": "0"}), \
                patch("frappe_mpsa_payments.money_in.stk_verify.book") as book:
            self.assertEqual(stk_verify.settle(req.name), "Verified")
        book.assert_called_once_with(req.name)
        row = frappe.db.get_value(EXPRESS, req.name, ["status", "transaction_id", "verification_status"], as_dict=True)
        self.assertEqual((row.status, row.transaction_id, row.verification_status), ("Completed", receipt, "Verified"))

    def test_stk_amount_mismatch_is_not_booked(self):
        req = self._prompt(100)
        self._callback(req, amount=1)
        with patch("frappe_mpsa_payments.money_in.stk_poll.stk_query", return_value={"ResultCode": "0"}):
            self.assertEqual(stk_verify.settle(req.name), "Mismatch")

    def test_cancelled_prompt_marked_failed(self):
        req = self._prompt()
        self._callback(req, paid=False)
        with patch("frappe_mpsa_payments.money_in.stk_poll.stk_query", return_value={"ResultCode": "1032"}):
            self.assertEqual(stk_verify.settle(req.name), "Failed")
        self.assertEqual(frappe.db.get_value(EXPRESS, req.name, "status"), "Failed")

    def test_resent_callback_after_settlement_is_ignored(self):
        req = self._prompt()
        self._callback(req)
        with patch("frappe_mpsa_payments.money_in.stk_poll.stk_query", return_value={"ResultCode": "0"}), \
                patch("frappe_mpsa_payments.money_in.stk_verify.book"):
            stk_verify.settle(req.name)
        from frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api import stk_push_callback

        with patch("frappe_mpsa_payments.money_in.stk_verify.queue") as queue:
            stk_push_callback(Body={"stkCallback": json.loads(frappe.db.get_value(EXPRESS, req.name, "callback_payload"))})
        queue.assert_not_called()

    # ------------------------------------------------------------ locks
    def test_named_lock_held_until_commit(self):
        if frappe.db.db_type != "mariadb":
            return
        import hashlib

        key = "test:" + frappe.generate_hash(length=8)
        lock = "mpesa:" + hashlib.sha1(key.encode()).hexdigest()
        with named_lock(key):
            with named_lock(key):  # re-entrant in one transaction
                pass
        self.assertEqual(frappe.db.sql("SELECT IS_FREE_LOCK(%s)", (lock,))[0][0], 0, "still held after the block")
        frappe.db.rollback()
        self.assertEqual(frappe.db.sql("SELECT IS_FREE_LOCK(%s)", (lock,))[0][0], 1, "released by rollback")
