# Copyright (c) 2020, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt

import unittest
from json import dumps

import frappe
from mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings import (
    create_mode_of_payment,
    process_balance_info,
)


class TestMpesaSettings(unittest.TestCase):
    def setUp(self):
        # create payment gateway in setup
        create_mpesa_settings(payment_gateway_name="_Test")
        create_mpesa_settings(payment_gateway_name="_Account Balance")
        create_mpesa_settings(payment_gateway_name="Payment")

    def tearDown(self):
        # Only this suite's records: other suites keep their Mpesa Settings fixtures.
        for name in ("_Test", "_Account Balance", "Payment"):
            frappe.db.delete("Mpesa Settings", {"name": name})
        frappe.db.sql(
            "delete from `tabIntegration Request` where integration_request_service = 'Mpesa'"
        )

    def test_creation_of_payment_gateway(self):
        mode_of_payment = create_mode_of_payment("Mpesa-_Test", payment_type="Phone")
        self.assertTrue(
            frappe.db.exists(
                "Payment Gateway Account", {"payment_gateway": "Mpesa-_Test"}
            )
        )
        self.assertTrue(mode_of_payment.name)
        self.assertEqual(mode_of_payment.type, "Phone")

    def test_processing_of_account_balance(self):
        mpesa_doc = create_mpesa_settings(payment_gateway_name="_Account Balance")
        mpesa_doc.get_account_balance_info()

        callback_response = get_account_balance_callback_payload()
        process_balance_info(**callback_response)
        integration_request = frappe.get_doc(
            "Integration Request", "AG_20200927_00007cdb1f9fb6494315"
        )

        # test integration request creation and successful update of the status on receiving callback response
        self.assertTrue(integration_request)
        self.assertEqual(integration_request.status, "Completed")

        # test formatting of account balance received as string to json with appropriate currency symbol
        mpesa_doc.reload()
        self.assertEqual(
            mpesa_doc.account_balance,
            dumps(
                {
                    "Working Account": {
                        "current_balance": "Sh 481,000.00",
                        "available_balance": "Sh 481,000.00",
                        "reserved_balance": "Sh 0.00",
                        "uncleared_balance": "Sh 0.00",
                    }
                }
            ),
        )

        integration_request.delete()

    def test_register_pull_transaction_missing_nominated_number(self):
        from mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings import (
            register_pull_transaction,
        )

        # _Test settings has no pull_transaction_nominated_number
        with self.assertRaises(frappe.exceptions.ValidationError):
            register_pull_transaction("_Test")

    def test_register_pull_transaction_success(self):
        from unittest.mock import MagicMock, patch

        from mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings import (
            register_pull_transaction,
        )

        frappe.db.set_value(
            "Mpesa Settings",
            "_Test",
            "pull_transaction_nominated_number",
            "254712345678",
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {  # Daraja Pull register reply
            "ResponseRefID": "0000-0000-0000-0000",
            "ResponseStatus": "1000",
            "ResponseDescription": "Accept the service request successfully.",
        }
        mock_response.raise_for_status = MagicMock()

        with (
            patch(
                "mpsa_suite.mpsa_suite.api.m_pesa_api.get_token",
                return_value="test_token",
            ),
            patch("requests.post", return_value=mock_response),
        ):
            # From the form the URL comes from the request; tests have no request.
            result = register_pull_transaction("_Test", callback_url="https://example.com/api/method/x")

        self.assertEqual(result["status"], "success")
        self.assertIn("Accept", result["message"])

    def test_pull_transaction_on_success_creates_c2b_records(self):
        from mpsa_suite.mpsa_suite.api.mpesa_response_handler import (
            pull_transaction_on_success,
        )

        unique_txn_id = f"PULL{frappe.generate_hash()[:8].upper()}"
        response = {
            "ResponseCode": "1000",
            "ResponseMessage": "Success",
            "Response": [
                [
                    {
                        "transactionId": unique_txn_id,
                        "trxDate": "2026-06-01T12:00:00+03:00",
                        "msisdn": "254712345678",
                        "sender": "MPESA",
                        "transactiontype": "c2b-paybill-debi",
                        "billreference": "TEST-REF-001",
                        "amount": "250.00",
                        "organizationname": "Safaricom Daraja 978",
                    }
                ]
            ],
            "CurrentPage": 0,
            "PageSize": 10,
            "TotalPages": 1,
            "TotalRecords": 1,
        }

        pull_transaction_on_success(
            response=response,
            document_name="_Test",
            settings_name="_Test",
            integration_request=None,
        )

        exists = frappe.db.exists(
            "Mpesa C2B Payment Register", {"transid": unique_txn_id}
        )
        self.assertTrue(exists)
        frappe.db.delete("Mpesa C2B Payment Register", {"transid": unique_txn_id})

    def test_pull_transaction_on_success_skips_duplicates(self):
        from mpsa_suite.mpsa_suite.api.mpesa_response_handler import (
            pull_transaction_on_success,
        )

        unique_txn_id = f"DUP{frappe.generate_hash()[:8].upper()}"

        # Pre-insert so it already exists
        existing = frappe.get_doc(
            {
                "doctype": "Mpesa C2B Payment Register",
                "transid": unique_txn_id,
                "transamount": 100.0,
                "msisdn": "254700000001",
                "businessshortcode": "174379",
            }
        )
        existing.insert(ignore_permissions=True)

        response = {
            "ResponseCode": "1000",
            "Response": [
                [
                    {
                        "transactionId": unique_txn_id,
                        "trxDate": "2026-06-01T12:00:00+03:00",
                        "msisdn": "254712345678",
                        "sender": "MPESA",
                        "transactiontype": "c2b-paybill-debi",
                        "billreference": "",
                        "amount": "100.00",
                        "organizationname": "Safaricom Daraja 978",
                    }
                ]
            ],
        }

        # Should not raise; duplicate is silently skipped
        pull_transaction_on_success(
            response=response,
            document_name="_Test",
            settings_name="_Test",
            integration_request=None,
        )

        count = frappe.db.count(
            "Mpesa C2B Payment Register", {"transid": unique_txn_id}
        )
        self.assertEqual(count, 1)
        frappe.db.delete("Mpesa C2B Payment Register", {"transid": unique_txn_id})

    def _capture_pull_realtime(self, response):
        """Run pull_transaction_on_success and return the realtime message it published."""
        from unittest.mock import patch

        from mpsa_suite.mpsa_suite.api.mpesa_response_handler import (
            pull_transaction_on_success,
        )

        published = {}

        def fake_publish(*args, **kwargs):
            if kwargs.get("event") == "mpesa_pull_transaction_complete":
                published.update(kwargs.get("message") or {})

        with patch(
            "mpsa_suite.mpsa_suite.api.mpesa_response_handler.frappe.publish_realtime",
            side_effect=fake_publish,
        ):
            pull_transaction_on_success(
                response=response,
                document_name="_Test",
                settings_name="_Test",
                integration_request=None,
            )
        return published

    def test_pull_transaction_1001_is_not_reported_as_success(self):
        """1001 arrives as HTTP 200 and must not read as a successful import.

        Regression: it used to fall through to the import loop, find no
        "Response" key, and publish "0 record(s) imported" - making an
        unprovisioned shortcode indistinguishable from a quiet window.
        """
        published = self._capture_pull_realtime(
            {
                "ResponseCode": "1001",
                "ResponseMessage": "No records found or Organization Name not available",
            }
        )

        self.assertEqual(published.get("status"), "warning")
        self.assertEqual(published.get("response_code"), "1001")
        self.assertIn("1001", published.get("message", ""))
        self.assertNotIn("record(s) imported", published.get("message", ""))

        self.assertEqual(
            frappe.db.get_value("Mpesa Settings", "_Test", "last_pull_status"),
            "No Data",
        )
        self.assertEqual(
            frappe.db.get_value("Mpesa Settings", "_Test", "last_pull_response_code"),
            "1001",
        )

    def test_pull_transaction_other_error_code_is_reported_as_error(self):
        published = self._capture_pull_realtime(
            {"ResponseCode": "500.003.02", "ResponseMessage": "Internal server error"}
        )

        self.assertEqual(published.get("status"), "error")
        self.assertEqual(published.get("count"), 0)
        self.assertEqual(
            frappe.db.get_value("Mpesa Settings", "_Test", "last_pull_status"), "Error"
        )

    def test_pull_transaction_1000_with_empty_response_still_reports_zero(self):
        """A genuinely quiet window is still a success, just with nothing to import."""
        published = self._capture_pull_realtime(
            {"ResponseCode": "1000", "ResponseMessage": "Success", "Response": []}
        )

        self.assertIn("0 record(s) imported", published.get("message", ""))
        self.assertEqual(published.get("count"), 0)
        self.assertEqual(
            frappe.db.get_value("Mpesa Settings", "_Test", "last_pull_status"),
            "Success",
        )

def create_mpesa_settings(payment_gateway_name="Express"):
    if frappe.db.exists("Mpesa Settings", payment_gateway_name):
        return frappe.get_doc("Mpesa Settings", payment_gateway_name)

    doc = frappe.get_doc(
        dict(  # nosec
            doctype="Mpesa Settings",
            sandbox=1,
            payment_gateway_name=payment_gateway_name,
            consumer_key="5sMu9LVI1oS3oBGPJfh3JyvLHwZOdTKn",
            consumer_secret="VI1oS3oBGPJfh3JyvLHw",
            online_passkey="LVI1oS3oBGPJfh3JyvLHwZOd",
            paybill_type="Pay Bill",
            business_shortcode="174379",
            till_number="174379",
        )
    )
    if doc.meta.has_field("company"):
        # The Payment Gateway Account and Mode of Payment are made for this company.
        doc.company = (
            "Wind Power LLC"
            if frappe.db.exists("Company", "Wind Power LLC")
            else frappe.db.get_single_value("Global Defaults", "default_company")
        )

    doc.insert(ignore_permissions=True)
    return doc


def get_test_account_balance_response():
    """Response received after calling the account balance API."""
    return {
        "ResultType": 0,
        "ResultCode": 0,
        "ResultDesc": "The service request has been accepted successfully.",
        "OriginatorConversationID": "10816-694520-2",
        "ConversationID": "AG_20200927_00007cdb1f9fb6494315",
        "TransactionID": "LGR0000000",
        "ResultParameters": {
            "ResultParameter": [
                {"Key": "ReceiptNo", "Value": "LGR919G2AV"},
                {"Key": "Conversation ID", "Value": "AG_20170727_00004492b1b6d0078fbe"},
                {"Key": "FinalisedTime", "Value": 20170727101415},
                {"Key": "Amount", "Value": 10},
                {"Key": "TransactionStatus", "Value": "Completed"},
                {"Key": "ReasonType", "Value": "Salary Payment via API"},
                {"Key": "TransactionReason"},
                {"Key": "DebitPartyCharges", "Value": "Fee For B2C Payment|KES|33.00"},
                {"Key": "DebitAccountType", "Value": "Utility Account"},
                {"Key": "InitiatedTime", "Value": 20170727101415},
                {"Key": "Originator Conversation ID", "Value": "19455-773836-1"},
                {"Key": "CreditPartyName", "Value": "254708374149 - John Doe"},
                {"Key": "DebitPartyName", "Value": "600134 - Safaricom157"},
            ]
        },
        "ReferenceData": {"ReferenceItem": {"Key": "Occasion", "Value": "aaaa"}},
    }


def get_payment_request_response_payload(Amount=500):
    """Response received after successfully calling the stk push process request API."""

    CheckoutRequestID = frappe.utils.random_string(10)

    return {
        "MerchantRequestID": "8071-27184008-1",
        "CheckoutRequestID": CheckoutRequestID,
        "ResultCode": 0,
        "ResultDesc": "The service request is processed successfully.",
        "CallbackMetadata": {
            "Item": [
                {"Name": "Amount", "Value": Amount},
                {"Name": "MpesaReceiptNumber", "Value": "LGR7OWQX0R"},
                {"Name": "TransactionDate", "Value": 20201006113336},
                {"Name": "PhoneNumber", "Value": 254723575670},
            ]
        },
    }


def get_account_balance_callback_payload():
    """Response received from the server as callback after calling the account balance API."""
    return {
        "Result": {
            "ResultType": 0,
            "ResultCode": 0,
            "ResultDesc": "The service request is processed successfully.",
            "OriginatorConversationID": "16470-170099139-1",
            "ConversationID": "AG_20200927_00007cdb1f9fb6494315",
            "TransactionID": "OIR0000000",
            "ResultParameters": {
                "ResultParameter": [
                    {
                        "Key": "AccountBalance",
                        "Value": "Working Account|KES|481000.00|481000.00|0.00|0.00",
                    },
                    {"Key": "BOCompletedTime", "Value": 20200927234123},
                ]
            },
            "ReferenceData": {
                "ReferenceItem": {
                    "Key": "QueueTimeoutURL",
                    "Value": "https://internalsandbox.safaricom.co.ke/mpesa/abresults/v1/submit",
                }
            },
        }
    }
