from unittest.mock import Mock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from .m_pesa_api import (
    confirmation,
    get_mpesa_draft_c2b_payments,
    get_mpesa_mode_of_payment,
    get_token,
    submit_mpesa_payment,
    validation,
)


class TestMPesaAPI(FrappeTestCase):
    @patch("requests.get")
    def test_get_token(self, mock_get):
        mock_response = Mock()
        mock_response.json.return_value = {"access_token": "dummy_token"}
        mock_get.return_value = mock_response

        app_key = "dummy_key"
        app_secret = "dummy_secret"
        base_url = "https://example.com"

        token = get_token(app_key, app_secret, base_url)

        self.assertEqual(token, "dummy_token")

    def test_confirmation(self):
        # Test accepted case
        args = {
            "TransactionType": "Payment",
            "TransID": "123456",
            "TransTime": "2024-05-01T12:00:00",
            "TransAmount": 100.0,
            "BusinessShortCode": "123456",
            "BillRefNumber": "BILL001",
            "InvoiceNumber": "INV001",
            "OrgAccountBalance": 500.0,
            "ThirdPartyTransID": "789012",
            "MSISDN": "1234567890",
            "FirstName": "John",
            "MiddleName": "Doe",
            "LastName": "Smith",
        }
        with patch("frappe.enqueue") as enqueue:
            result = confirmation(**args)
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(result["ResultDesc"], "Accepted")
        c2b_data = enqueue.call_args.kwargs["c2b_data"]
        self.assertEqual((c2b_data["transid"], c2b_data["transamount"]), ("123456", 100.0))

        # A confirmation is always acknowledged (Safaricom only retries otherwise); a bad
        # amount is caught later, when the receipt is verified or booked.
        args["TransAmount"] = "invalid_amount"
        with patch("frappe.enqueue") as enqueue:
            result = confirmation(**args)
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(enqueue.call_args.kwargs["c2b_data"]["transamount"], 0)

    def test_validation(self):
        # Test validation always returns accepted
        result = validation()
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(result["ResultDesc"], "Accepted")

    @patch("frappe.get_all")
    def test_get_mpesa_mode_of_payment(self, mock_get_all):
        mock_get_all.return_value = [frappe._dict(mode_of_payment="Cash"), frappe._dict(mode_of_payment="Cash")]

        company = "Test Company"

        modes_of_payment = get_mpesa_mode_of_payment(company)

        self.assertEqual(modes_of_payment, ["Cash"])

    @patch("frappe.get_list")
    def test_get_mpesa_draft_payments(self, mock_get_all):
        mock_get_all.return_value = [{"name": "MP001", "amount": 100.0}]

        company = "Test Company"
        mode_of_payment = "Cash"

        payments = get_mpesa_draft_c2b_payments(company, mode_of_payment=mode_of_payment)

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]["name"], "MP001")
        self.assertEqual(payments[0]["amount"], 100.0)

    @patch("frappe.get_doc")
    @patch("frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.process_mpesa_payment")
    def test_submit_mpesa_payment(self, mock_process, mock_get_doc):
        mock_process.return_value = Mock(payment_entry="PE001")
        mock_get_doc.return_value = "Payment Entry PE001"

        payment_entry = submit_mpesa_payment("MP001", "Test Customer")

        mock_process.assert_called_once_with("MP001", "Test Customer", submit_payment=True)
        mock_get_doc.assert_called_once_with("Payment Entry", "PE001")
        self.assertEqual(payment_entry, "Payment Entry PE001")
