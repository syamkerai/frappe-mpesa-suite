"""Trident live-test fixes against the real database (ERPNext v15 and v16; handover F1, F2, F4, F5,
F7, F11 in docs/TRIDENT_LIVE_TEST_HANDOVER.md).

The shortcode and supplier of test_b2b_documents (600777, an approved paybill account) plus an
approved till account. Requests are written as their submit and Safaricom's ack leave them
(sending, and who may send, have their own tests), so nobody here submits or approves anything;
nothing reaches Safaricom (post() is replaced) and Results are handed over the way Safaricom posts
them. Each test is rolled back. Unit coverage: tests/unit/test_trident_fixes.py and
tests/unit/test_b2b_status.py.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import b2b, b2b_status, b2c_status
from mpsa_suite.money_out.test_b2b_documents import (
    BUYING,
    SETTINGS,
    SUITE_VALUES,
    SUPPLIER,
    _buying_price_list,
    _settings,
    _supplier,
)
from mpsa_suite.services import b2c_request_service

B2B, PE, JE, BT, PI = b2b.B2B_DOCTYPE, "Payment Entry", "Journal Entry", "Bank Transaction", "Purchase Invoice"
DISB, IR = "B2C Payment Disbursement", "Integration Request"
TILL = "835811"
TILL_BALANCE = "{{Amount={{CurrencyCode=KES, MinimumAmount={cents}, BasicAmount={amount:.2f}}}}}"


def _till_account(supplier: str) -> str:
    """An approved Till (Buy Goods) account of the supplier (written as approved)."""
    name = "_T-MSA-TRIDENT-TILL"
    if not frappe.db.exists("Supplier M-Pesa Account", name):
        doc = frappe.get_doc({
            "doctype": "Supplier M-Pesa Account", "supplier": supplier, "account_type": "Till (Buy Goods)",
            "shortcode": TILL, "expected_name": SUPPLIER, "is_default": 0, "docstatus": 1,
            "proof": "/private/files/_t_till_proof.pdf", "approved_by": "Administrator", "approved_on": now_datetime(),
            "active_from": add_days(now_datetime(), -1), "creation": now_datetime(), "modified": now_datetime(),
            "owner": "Administrator"})
        doc.name = name
        doc.db_insert()
    return name


def _project(name: str) -> str:
    existing = frappe.db.get_value("Project", {"project_name": name, "company": t.COMPANY}, "name")
    return existing or frappe.get_doc({"doctype": "Project", "project_name": name, "company": t.COMPANY}).insert().name


def _cost_center(name: str) -> str:
    existing = frappe.db.get_value("Cost Center", {"cost_center_name": name, "company": t.COMPANY}, "name")
    if existing:
        return existing
    parent = frappe.db.get_value("Cost Center", {"company": t.COMPANY, "is_group": 1}, "name", order_by="lft asc")
    return frappe.get_doc({"doctype": "Cost Center", "cost_center_name": name, "parent_cost_center": parent,
                           "company": t.COMPANY, "is_group": 0}).insert().name


class TestTridentFixes(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        cls.bank_account, cls.gl = _settings()
        cls.supplier, cls.account = _supplier()
        cls.till = _till_account(cls.supplier)
        _buying_price_list()
        frappe.db.set_value("Item", t.ITEM, "is_purchase_item", 1)
        cls.project = _project("_T Trident P1")
        cls.other_project = _project("_T Trident Other")
        cls.payout_center = _cost_center("_T Trident Payouts")
        cls.company_center = frappe.get_cached_value("Company", t.COMPANY, "cost_center")
        frappe.db.delete(B2B, {"supplier": cls.supplier})
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        self.tag = frappe.generate_hash(length=8)
        # when Safaricom says the payment finished (FinalisedTime is an int in its answers)
        self.finalised = int((now_datetime() - timedelta(minutes=1)).strftime("%Y%m%d%H%M%S"))
        self._no_commit = patch.object(frappe.db, "commit")
        self._no_commit.start()

    def tearDown(self):
        self._no_commit.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.local.mpesa_suite_bank_accounts = None

    # ------------------------------------------------------------ helpers
    def _receipt(self) -> str:
        return "U" + frappe.generate_hash(length=9).upper()

    def _invoice(self, amount, submit=True, **values):
        pi = frappe.get_doc({
            "doctype": PI, "supplier": self.supplier, "company": t.COMPANY, "posting_date": nowdate(),
            "due_date": nowdate(), "currency": "KES", "conversion_rate": 1, "buying_price_list": BUYING,
            "plc_conversion_rate": 1, "bill_no": f"TRIDENT-{self.tag}-{frappe.generate_hash(length=4)}",
            "items": [{"item_code": t.ITEM, "qty": 1, "rate": amount, "uom": "Nos", "conversion_factor": 1}],
            **values})
        pi.insert()
        if submit:
            pi.submit()
        return pi

    def _bank_payment(self, pi, amount) -> None:
        cash = frappe.db.get_value("Account", {"company": t.COMPANY, "account_type": "Cash", "is_group": 0}, "name")
        pe = frappe.new_doc(PE)
        pe.update({"payment_type": "Pay", "company": t.COMPANY, "party_type": "Supplier", "party": self.supplier,
                   "paid_from": cash, "paid_to": frappe.db.get_value(PI, pi, "credit_to"), "paid_amount": amount,
                   "received_amount": amount, "source_exchange_rate": 1, "target_exchange_rate": 1,
                   "reference_no": f"BANK-{self.tag}", "reference_date": nowdate(), "posting_date": nowdate()})
        pe.append("references", {"reference_doctype": PI, "reference_name": pi, "allocated_amount": amount})
        pe.insert()
        pe.submit()

    def _sent(self, amount, **values) -> str:
        """A request as its submit and Safaricom's ack leave it: submitted, Initiated, with the ack's IDs."""
        doc = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "amount": amount, "company": t.COMPANY,
                              "remarks": f"T {self.tag}", "supplier": self.supplier, **values})
        doc.insert()
        frappe.db.set_value(B2B, doc.name, {
            "docstatus": 1, "status": "Initiated", "conversation_id": f"AG_{frappe.generate_hash(length=12)}",
            "originator_conversation_id": f"OC_{frappe.generate_hash(length=12)}"}, update_modified=False)
        return doc.name

    def _result(self, name, params, book=True) -> str:
        """Safaricom's Paid Result for the request, then (``book``) the booking job it queues."""
        row = frappe.db.get_value(B2B, name, ["conversation_id", "originator_conversation_id"], as_dict=True)
        receipt = self._receipt()
        result = {"ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
                  "ConversationID": row.conversation_id, "OriginatorConversationID": row.originator_conversation_id,
                  "TransactionID": receipt,
                  "ResultParameters": {"ResultParameter": [{"Key": k, "Value": v} for k, v in params]}}
        with patch("frappe.enqueue") as enqueue:
            b2b.handle_result(result, name)
        self.queued = [c.args[0] for c in enqueue.call_args_list if c.args]
        if book:
            b2b.book_payment_entry(name)
        return receipt

    def _till_result(self, name, balance=1894.00, book=True) -> str:
        """What Trident's till payment got back: a balance and nothing else."""
        text = TILL_BALANCE.format(cents=int(balance * 100), amount=balance)
        return self._result(name, [("DebitAccountCurrentBalance", text), ("InitiatorAccountCurrentBalance", text),
                                   ("Amount", 50.0)], book=book)

    def _answer(self, name, receipt, ack, **values) -> dict:
        """Safaricom's Transaction Status answer for the payout's receipt, as it answered live."""
        row = frappe.db.get_value(B2B, name, ["conversation_id", "originator_conversation_id", "amount"], as_dict=True)
        params = {
            "DebitPartyName": "600777 - TEST CO", "CreditPartyName": f"{TILL} - {SUPPLIER.upper()} HQ",
            "OriginatorConversationID": row.originator_conversation_id, "InitiatedTime": self.finalised,
            "DebitAccountType": "MMF Account for Organization",
            "DebitPartyCharges": "Business Buy Goods Charge|KES|3.00", "ReasonType": "Business Buy Goods via API",
            "TransactionStatus": "Completed", "FinalisedTime": self.finalised, "Amount": flt(row.amount),
            "ConversationID": row.conversation_id, "ReceiptNo": receipt, **values}
        return {"ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
                "OriginatorConversationID": ack["OriginatorConversationID"], "ConversationID": ack["ConversationID"],
                "TransactionID": "UJR1000000",  # a placeholder: ReceiptNo is the receipt
                "ResultParameters": {"ResultParameter": [{"Key": k, "Value": v} for k, v in params.items()]}}

    def _ask(self, name):
        ack = {"ResponseCode": "0", "ResponseDescription": "Accept the service request successfully.",
               "ConversationID": f"AG_Q{frappe.generate_hash(length=12)}",
               "OriginatorConversationID": f"OC_Q{frappe.generate_hash(length=12)}"}
        with patch("mpsa_suite.money_out.b2c_status.post", return_value=(ack, None)) as post:
            query = b2b_status.auto_query(name)
        self.assertTrue(query)
        return query, ack, post.call_args.args[2]

    def _line(self, key):
        lines = frappe.get_all(BT, filters={"reference_number": key, "docstatus": ["<", 2]},
                               fields=["name", "withdrawal"], order_by="creation asc")
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    # ------------------------------------------------------------ F1: a callback user hidden from the project
    def test_booking_runs_as_administrator_for_an_integration_user_hidden_from_the_project(self):
        user = frappe.get_doc({"doctype": "User", "email": f"_t_integration_{self.tag}@example.com",
                               "first_name": "Integration", "send_welcome_email": 0,
                               "roles": [{"role": "Accounts User"}, {"role": "M-Pesa Manager"}]}).insert(
            ignore_permissions=True)
        frappe.get_doc({"doctype": "User Permission", "user": user.name, "allow": "Project",
                        "for_value": self.other_project, "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
        pi = self._invoice(3000, project=self.project)
        self.assertFalse(frappe.has_permission(PI, "read", doc=pi, user=user.name))
        name = self._sent(3000, purchase_invoice=pi.name, supplier_mpesa_account=self.account)
        since = now_datetime()
        frappe.set_user(user.name)  # what the Result's job runs as
        receipt = self._result(name, [("DebitPartyAffectedAccountBalance", "Working Account|KES|47000.00|47000.00|0.00|0.00"),
                                      ("TransCompletedTime", now_datetime().strftime("%Y%m%d%H%M%S")),
                                      ("DebitPartyCharges", "Business Pay Bill Charge|KES|3.00"),
                                      ("ReceiverPartyPublicName", f"888880 - {SUPPLIER.upper()}")])
        frappe.set_user("Administrator")
        (entry,) = frappe.get_all(PE, filters={"reference_no": receipt, "docstatus": 1}, fields=["name", "owner", "project"])
        self.assertEqual((entry.owner, entry.project), ("Administrator", self.project))  # F5: the document's project
        self.assertEqual(frappe.db.get_value(B2B, name, "entry_status"), "Entry Made")
        self.assertFalse(frappe.get_all("Error Log", filters={"method": ["like", "M-Pesa B2B booking failed%"],
                                                              "creation": [">=", since]}))

    # ------------------------------------------------------------ F2: a till's Result, and its status answer
    def test_a_tills_result_is_flagged_and_queried_and_the_answer_fills_it(self):
        name = self._sent(50, supplier_mpesa_account=self.till)
        self.assertEqual(frappe.db.get_value(B2B, name, "command_id"), "BusinessBuyGoods")
        receipt = self._till_result(name)
        self.assertIn("mpsa_suite.money_out.b2b_status.auto_query", self.queued)
        state = frappe.db.get_value(B2B, name, ["status", "completed_time_estimated", "debit_account_balance",
                                                "receiver_public_name", "trans_completed_time"], as_dict=True)
        self.assertEqual((state.status, state.completed_time_estimated, state.debit_account_balance),
                         ("Paid", 1, "Working Account|KES|1894.00|1894.00"))
        self.assertTrue(state.trans_completed_time and not state.receiver_public_name)
        # no charge line yet: the status query may still state the charge
        self.assertFalse(frappe.db.exists(BT, {"reference_number": f"CHG-{receipt}"}))

        query, ack, body = self._ask(name)
        self.assertEqual((body["TransactionID"], body["IdentifierType"]), (receipt, "4"))
        self.assertIn(f"b2c_status_result?ref={query}", body["ResultURL"])
        with patch("mpsa_suite.money_out.supplier_accounts._alert") as alert:
            b2c_status.handle_result(self._answer(name, receipt, ack), query)
        alert.assert_not_called()
        state = frappe.db.get_value(B2B, name, ["receiver_public_name", "debit_party_charges", "trans_completed_time",
                                                "completed_time_estimated", "receiver_name_mismatch"], as_dict=True)
        self.assertEqual((state.receiver_public_name, state.debit_party_charges, state.completed_time_estimated),
                         (f"{TILL} - {SUPPLIER.upper()} HQ", "Business Buy Goods Charge|KES|3.00", 0))
        self.assertEqual(state.trans_completed_time, datetime.strptime(str(self.finalised), "%Y%m%d%H%M%S"))
        self.assertFalse(state.receiver_name_mismatch)
        account = frappe.db.get_value("Supplier M-Pesa Account", self.till, ["last_receiver_name", "name_mismatch"],
                                      as_dict=True)
        self.assertEqual((account.last_receiver_name, account.name_mismatch), (f"{TILL} - {SUPPLIER.upper()} HQ", 0))
        q = frappe.db.get_value("Mpesa B2C Status Query", query, ["outcome", "applied", "receipt_no", "b2b_request"],
                                as_dict=True)
        self.assertEqual((q.outcome, q.applied, q.receipt_no, q.b2b_request), ("Paid", 1, receipt, name))
        # the charge line and its entry, as the hand step on Release C did
        self.assertEqual(flt(self._line(f"CHG-{receipt}").withdrawal), 3)
        self.assertTrue(frappe.db.exists(JE, {"cheque_no": f"CHG-{receipt}", "docstatus": ["<", 2]}))

    def test_a_till_answer_naming_someone_else_blocks_the_account(self):
        name = self._sent(50, supplier_mpesa_account=self.till)
        receipt = self._till_result(name)
        query, ack, _body = self._ask(name)
        with patch("mpsa_suite.money_out.supplier_accounts._alert") as alert:
            b2c_status.handle_result(self._answer(name, receipt, ack, CreditPartyName=f"{TILL} - SOMEBODY ELSE"), query)
        self.assertEqual(frappe.db.get_value(B2B, name, "receiver_name_mismatch"), 1)
        self.assertEqual(frappe.db.get_value("Supplier M-Pesa Account", self.till, "name_mismatch"), 1)
        self.assertEqual(alert.call_count, 1)

    def test_a_tills_charge_is_the_fall_in_the_working_balance_on_one_cost_center(self):
        frappe.db.set_value("Mpesa Settings", SETTINGS, "cost_center", self.payout_center)
        paybill = self._sent(50, supplier_mpesa_account=self.account)
        till = self._sent(50, supplier_mpesa_account=self.till)
        self._result(paybill, [("DebitPartyAffectedAccountBalance", "Working Account|KES|1947.00|1947.00|0.00|0.00"),
                               ("TransCompletedTime", (now_datetime() - timedelta(seconds=30)).strftime("%Y%m%d%H%M%S")),
                               ("DebitPartyCharges", "Business Pay Bill Charge|KES|3.00"),
                               ("ReceiverPartyPublicName", f"888880 - {SUPPLIER.upper()}")])
        receipt = self._till_result(till, balance=1894.00)
        self.assertEqual(flt(self._line(f"CHG-{receipt}").withdrawal), 3)  # 1,947 - 1,894 - 50
        (je,) = frappe.get_all(JE, filters={"cheque_no": f"CHG-{receipt}", "docstatus": ["<", 2]}, pluck="name")
        centers = {row.cost_center for row in frappe.get_doc(JE, je).accounts}
        self.assertEqual(centers, {self.payout_center})  # both rows follow the shortcode (F5)

    # ------------------------------------------------------------ F5: the payout's Payment Entry follows its document
    def test_b2b_entry_takes_the_invoices_cost_center_else_the_shortcodes(self):
        frappe.db.set_value("Mpesa Settings", SETTINGS, "cost_center", self.payout_center)
        params = [("DebitPartyAffectedAccountBalance", "Working Account|KES|47000.00|47000.00|0.00|0.00"),
                  ("TransCompletedTime", now_datetime().strftime("%Y%m%d%H%M%S")),
                  ("DebitPartyCharges", "Business Pay Bill Charge|KES|3.00"),
                  ("ReceiverPartyPublicName", f"888880 - {SUPPLIER.upper()}")]
        for header, expected in ((self.company_center, self.company_center), (None, self.payout_center)):
            pi = self._invoice(1000)
            frappe.db.set_value(PI, pi.name, "cost_center", header)  # whatever ERPNext filled in
            receipt = self._result(self._sent(1000, purchase_invoice=pi.name, supplier_mpesa_account=self.account),
                                   params)
            self.assertEqual(frappe.db.get_value(PE, {"reference_no": receipt, "docstatus": 1}, "cost_center"), expected)

    # ------------------------------------------------------------ F11: Mpesa Settings → Check Transaction Status
    def test_a_status_answer_for_a_till_receipt_is_the_payouts_and_fills_it(self):
        from mpsa_suite.mpsa_suite.api import m_pesa_api

        name = self._sent(50, supplier_mpesa_account=self.till)
        receipt = self._till_result(name)
        ack = {"ConversationID": f"AG_S{frappe.generate_hash(length=12)}", "ResponseCode": "0",
               "OriginatorConversationID": f"OC_S{frappe.generate_hash(length=12)}"}
        asked = frappe.get_doc({"doctype": IR, "is_remote_request": 1, "integration_request_service": "Mpesa Transaction Status",
                                "reference_doctype": "Mpesa C2B Payment Register", "status": "Completed",
                                "data": json.dumps({"mpesa_settings": SETTINGS, "transaction_id": receipt}),
                                "output": json.dumps(ack)}).insert(ignore_permissions=True)
        result = frappe.get_doc({
            "doctype": IR, "is_remote_request": 1, "integration_request_service": "Mpesa Transaction Status Result Callback",
            "reference_doctype": "Mpesa C2B Payment Register", "status": "Queued",
            "data": json.dumps({"Result": self._answer(name, receipt, ack)}),
            "url": f"https://test.example.com/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api."
                   f"handle_transaction_status_result?ref={asked.name}&t=token"}).insert(ignore_permissions=True)
        since = now_datetime()
        with patch("frappe.publish_realtime") as publish, patch("mpsa_suite.money_out.supplier_accounts._alert"):
            m_pesa_api.process_mpesa_integration_request(result.name)
        row = frappe.db.get_value(IR, result.name, ["status", "output"], as_dict=True)
        self.assertEqual(row.status, "Completed", row.output)
        self.assertEqual((json.loads(row.output)["kind"], json.loads(row.output)["payout"]), ("B2B", name))
        self.assertEqual(frappe.db.get_value(B2B, name, "debit_party_charges"), "Business Buy Goods Charge|KES|3.00")
        told = [c.kwargs for c in publish.call_args_list if c.kwargs.get("event") == "mpesa_transaction_status_update"]
        self.assertEqual([(m["user"], m["message"]["status"]) for m in told], [("Administrator", "success")])
        self.assertIn(f"Payout {name} Completed: KES 50.00", told[0]["message"]["message"])
        self.assertFalse(frappe.db.exists("Mpesa C2B Payment Register", {"transid": receipt}))
        self.assertFalse(frappe.get_all("Error Log", filters={"method": ["like", "M-Pesa Transaction Status%"],
                                                              "creation": [">=", since]}))

    # ------------------------------------------------------------ F4 and F7: B2C rows checked at save
    def test_a_b2c_row_whose_invoice_owes_nothing_is_refused_at_save(self):
        paid, part, draft = self._invoice(1000), self._invoice(1000), self._invoice(1000, submit=False)
        self._bank_payment(paid.name, 1000)
        self._bank_payment(part.name, 400)
        doc = frappe.new_doc(DISB)
        row = doc.append("references", {"reference_doctype": PI, "reference_name": paid.name, "allocated_amount": 100,
                                        "party_type": "Supplier", "party": self.supplier})
        self.assertRaisesRegex(frappe.ValidationError, f"Row #1: Purchase Invoice {paid.name} has nothing left to pay",
                               doc.set_outstanding_from_documents)
        row.reference_name = draft.name  # not submitted: owes nothing yet
        self.assertRaisesRegex(frappe.ValidationError, "has nothing left to pay", doc.set_outstanding_from_documents)
        row.reference_name = part.name
        doc.set_outstanding_from_documents()
        self.assertEqual(flt(row.outstanding_amount), 600)

        # an amended copy keeps a row whose payout was sent already: it is not sent again, so it saves
        request = frappe.get_doc({"doctype": "B2C Disbursement Request", "name": f"_T-B2CR-{self.tag}", "docstatus": 1,
                                  "status": "Paid", "amount": 100, "mpesa_settings": SETTINGS})
        request.db_insert()
        row.update({"reference_name": paid.name, "b2c_disbursement_request": request.name})
        doc.set_outstanding_from_documents()

    def test_the_transaction_limit_caps_every_b2c_row_at_save_and_at_send(self):
        frappe.db.set_value("Mpesa Settings", SETTINGS, "transaction_limit", 100)
        doc = frappe.new_doc(DISB)
        doc.mode_of_payment = f"Mpesa-{SETTINGS}"
        row = doc.append("references", {"reference_doctype": PI, "reference_name": "ANY", "allocated_amount": 150})
        self.assertRaisesRegex(frappe.ValidationError, "Row #1: Amount is above this shortcode's transaction limit",
                               doc.validate_payout_limits)
        row.allocated_amount = 100
        doc.validate_payout_limits()

        request = frappe._dict(name="B2C-T", amount=150, mpesa_settings=SETTINGS, phone_number="254708374149")
        self.assertRaisesRegex(frappe.ValidationError, "transaction limit of 100.00",
                               b2c_request_service.validate_b2c_amount_and_party, request)
        b2c_request_service.validate_b2c_amount_and_party(frappe._dict(request, amount=100))
