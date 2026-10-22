from unittest.mock import Mock, patch

from frappe.tests.utils import FrappeTestCase

from .m_pesa_api import (
    confirmation,
    get_token,
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
        # Validation always accepts, in Daraja's documented shape: ResultCode is the string "0".
        result = validation()
        self.assertEqual(result["ResultCode"], "0")
        self.assertEqual(result["ResultDesc"], "Accepted")
