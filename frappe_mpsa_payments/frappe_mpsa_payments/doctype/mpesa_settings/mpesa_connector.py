import urllib.parse

import requests
from frappe.utils import get_url


class MpesaConnector:
    def __init__(
        self,
        env="sandbox",
        app_key=None,
        app_secret=None,
        sandbox_url="https://sandbox.safaricom.co.ke",
        live_url="https://api.safaricom.co.ke",
    ):
        """Setup configuration for Mpesa connector and generate new access token."""
        self.env = env
        self.app_key = app_key
        self.app_secret = app_secret
        if env == "sandbox":
            self.base_url = sandbox_url
        else:
            self.base_url = live_url
        self.authentication_token = None

    def _ensure_authenticated(self):
        if not self.authentication_token:
            self.authenticate()

    def authenticate(self):
        """
        This method is used to fetch the access token required by Mpesa.

        Returns:
                                        access_token (str): This token is to be used with the Bearer header for further API calls to Mpesa.
        """
        from ....daraja.client import fetch_token

        self.authentication_token = fetch_token(self.app_key, self.app_secret, self.base_url)
        return self.authentication_token

    def get_balance(
        self,
        initiator=None,
        security_credential=None,
        party_a=None,
        identifier_type=None,
        remarks=None,
        queue_timeout_url=None,
        result_url=None,
    ):
        """
        This method uses Mpesa's Account Balance API to to enquire the balance on a M-Pesa BuyGoods (Till Number).

        Args:
                                        initiator (str): Username used to authenticate the transaction.
                                        security_credential (str): Generate from developer portal.
                                        command_id (str): AccountBalance.
                                        party_a (int): Till number being queried.
                                        identifier_type (int): Type of organization receiving the transaction. (MSISDN/Till Number/Organization short code)
                                        remarks (str): Comments that are sent along with the transaction(maximum 100 characters).
                                        queue_timeout_url (str): The url that handles information of timed out transactions.
                                        result_url (str): The url that receives results from M-Pesa api call.

        Returns:
                                        OriginatorConverstionID (str): The unique request ID for tracking a transaction.
                                        ConversationID (str): The unique request ID returned by mpesa for each request made
                                        ResponseDescription (str): Response Description message
        """

        self._ensure_authenticated()

        payload = {
            "Initiator": initiator,
            "SecurityCredential": security_credential,
            "CommandID": "AccountBalance",
            "PartyA": party_a,
            "IdentifierType": identifier_type,
            "Remarks": remarks,
            "QueueTimeOutURL": queue_timeout_url,
            "ResultURL": result_url,
        }
        headers = {
            "Authorization": f"Bearer {self.authentication_token}",
            "Content-Type": "application/json",
        }
        saf_url = "{}{}".format(self.base_url, "/mpesa/accountbalance/v1/query")
        r = requests.post(saf_url, headers=headers, json=payload, timeout=(10, 30))
        return r.json()

    def transaction_status(
        self,
        initiator=None,
        security_credential=None,
        transaction_id=None,
        party_a=None,
        identifier_type=None,
        remarks=None,
        occasion=None,
        queue_timeout_url=None,
        result_url=None,
    ):
        """
        This method uses Mpesa's Transaction Status API to query the status of a transaction.

        Args:
                                        initiator (str): Username used to authenticate the transaction.
                                        security_credential (str): Generate from developer portal.
                                        transaction_id (str): Unique identifier for the transaction being queried.
                                        party_a (int): Organization's short code (MSISDN/Till Number/Paybill).
                                        identifier_type (int): Type of organization receiving the transaction.
                                                                                                                        - 1: MSISDN
                                                                                                                        - 2: Till Number
                                                                                                                        - 3: Organization Short Code
                                        remarks (str): Comments sent along with the transaction (maximum 100 characters).
                                        occasion (str): Optional parameter to pass information about the transaction.
                                        queue_timeout_url (str): URL for handling timeouts.
                                        result_url (str): URL to receive the response.

        Returns:
                                        OriginatorConversationID (str): Unique ID for tracking the request.
                                        ConversationID (str): Unique request ID returned by Mpesa.
                                        ResponseDescription (str): Descriptive message of the response.
        """

        self._ensure_authenticated()

        payload = {
            "Initiator": initiator,
            "SecurityCredential": security_credential,
            "CommandID": "TransactionStatusQuery",
            "TransactionID": transaction_id,
            "PartyA": party_a,
            "IdentifierType": identifier_type,
            "Occasion": occasion,
            "Remarks": remarks,
            "QueueTimeOutURL": queue_timeout_url,
            "ResultURL": result_url,
        }
        headers = {
            "Authorization": f"Bearer {self.authentication_token}",
            "Content-Type": "application/json",
        }
        saf_url = "{}{}".format(self.base_url, "/mpesa/transactionstatus/v1/query")
        r = requests.post(saf_url, headers=headers, json=payload, timeout=(10, 30))
        return r.json()

    def get_payment_url(self, **kwargs):
        phone = kwargs.get("phone") or kwargs.get("phone_number") or ""

        query_params = {
            "phone_number": phone,
            "payment_gateway": kwargs.get("payment_gateway"),
            "reference_type": kwargs.get("reference_doctype"),
            "reference_id": kwargs.get("reference_docname"),
            "amount": kwargs.get("amount"),
            "redirect_to": kwargs.get("redirect_to"),
        }

        encoded_params = urllib.parse.urlencode(query_params)

        return get_url(f"./mpesa/stkpush?{encoded_params}")
