"""B2B from Purchase Invoices and Orders, pay first and make the entry later, against the real
database (ERPNext v15 and v16).

A shortcode of its own (600777, its own Bank Account and M-Pesa Charges Account) and a supplier
with an approved Supplier M-Pesa Account. Nothing reaches Safaricom: post() is replaced, and
Results are handed to handle_result the way Safaricom posts them; the booking job it enqueues is
run in the test. Each test is rolled back: commits made by the code under test (the Result's
Integration Request log, the sweep) are replaced for the whole test, so nothing one test does is
seen by the next. Requests are made with the test company: ERPNext's v15 test setup makes
another company the site default. Requests are prepared as Administrator and sent by a named
M-Pesa Payer with Send via M-Pesa (t.send_as), as the form does: Administrator may not send, nor
cancel one that may have been paid (a named M-Pesa Manager does). The B2C payouts' Send via
M-Pesa (a direct B2C Payment Disbursement and its jobs, a direct B2C Disbursement Request) is
here too: the shortcode is also a B2C one.
Unit coverage is in tests/unit/test_b2b_owed.py and tests/unit/test_b2b_entries.py.
"""

from unittest.mock import Mock, patch

import frappe
from frappe.utils import add_days, add_to_date, flt, getdate, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import b2b, b2b_entries, owed, send_confirm

SETTINGS, SHORTCODE = "TESTB2BDOCS", "600777"
B2B, PE, JE, BT = b2b.B2B_DOCTYPE, "Payment Entry", "Journal Entry", "Bank Transaction"
PI, PO = "Purchase Invoice", "Purchase Order"
DISB, DISB_ROW = "B2C Payment Disbursement", "B2C Payment Disbursement Reference"
B2C_REQ, B2C_PHONE = "B2C Disbursement Request", "254708374149"
SUPPLIER = "_T B2B Docs Supplier"
BUYING = "_Test M-Pesa KES Buying"
CHARGES = "_Test M-Pesa Charges"
EXPENSE = "_T B2B Docs Rent"
TERMS = "_T B2B Docs 50-50"
SUITE_VALUES = dict(book_payout_charges=1, reconcile_payout_lines=1, payout_lines_from=None, allow_one_off_b2b=0,
                    entry_reminder_days=3)


def _account(name: str, root_type: str, report_type: str) -> str:
    existing = frappe.db.get_value("Account", {"account_name": name, "company": t.COMPANY}, "name")
    if existing:
        return existing
    parent = frappe.db.get_value("Account", {"company": t.COMPANY, "root_type": root_type, "is_group": 1}, "name",
                                 order_by="lft desc")
    return frappe.get_doc({"doctype": "Account", "account_name": name, "company": t.COMPANY, "parent_account": parent,
                           "root_type": root_type, "report_type": report_type, "is_group": 0}).insert().name


def _settings() -> tuple[str, str]:
    """Mpesa Settings TESTB2BDOCS (B2B) on its own Bank Account; (bank account, GL)."""
    if not frappe.db.exists("Mpesa Settings", SETTINGS):
        frappe.get_doc({
            "doctype": "Mpesa Settings", "payment_gateway_name": SETTINGS, "use_c2b": 0, "use_stk": 0, "use_b2c": 1,
            "use_b2b": 1, "paybill_type": "Pay Bill", "online_passkey": "passkey", "sandbox": 1, "company": t.COMPANY,
            "consumer_key": "key", "consumer_secret": "secret", "business_shortcode": SHORTCODE,
            "transaction_limit": 250000,
        }).insert()
    gl = frappe.db.get_value("Mode of Payment Account", {"parent": f"Mpesa-{SETTINGS}", "company": t.COMPANY},
                             "default_account")
    bank_account = frappe.db.get_value("Bank Account", {"account": gl, "is_company_account": 1}, "name")
    if not bank_account:
        bank_account = frappe.get_doc({"doctype": "Bank Account", "account_name": f"Mpesa B2B {SHORTCODE}",
                                       "bank": "Mpesa", "account": gl, "company": t.COMPANY, "is_company_account": 1,
                                       "bank_account_no": SHORTCODE}).insert().name
    charges = _account(CHARGES, "Expense", "Profit and Loss")
    # an initiator so the request can be "sent" (post() is replaced in the tests)
    frappe.db.set_value("Mpesa Settings", SETTINGS, {
        "bank_account": bank_account, "charges_account": charges, "submit_b2c_accounting_entries": 1,
        "initiator_name": "testapi", "security_credential": "dummy"})
    return bank_account, gl


def _supplier() -> tuple[str, str]:
    """The supplier and its approved, usable M-Pesa account (written as approved: maker-checker
    approval has its own tests)."""
    name = frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER}, "name")
    if not name:
        group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
        name = frappe.get_doc({"doctype": "Supplier", "supplier_name": SUPPLIER, "supplier_group": group}).insert().name
    account = frappe.db.get_value("Supplier M-Pesa Account", {"supplier": name, "docstatus": 1}, "name")
    if not account:
        doc = frappe.get_doc({
            "doctype": "Supplier M-Pesa Account", "supplier": name, "account_type": "Paybill", "shortcode": "888880",
            "account_number": "TB2B-1", "expected_name": SUPPLIER, "is_default": 1, "docstatus": 1,
            "proof": "/private/files/_t_b2b_proof.pdf", "approved_by": "Administrator", "approved_on": now_datetime(),
            "active_from": add_days(now_datetime(), -1), "creation": now_datetime(), "modified": now_datetime(),
            "owner": "Administrator"})
        doc.name = "_T-MSA-B2BDOCS"
        doc.db_insert()
        account = doc.name
    return name, account


def _buying_price_list() -> None:
    """KES, so invoices and orders never ask for an exchange rate (none exists offline)."""
    if not frappe.db.exists("Price List", BUYING):
        frappe.get_doc({"doctype": "Price List", "price_list_name": BUYING, "currency": "KES", "buying": 1,
                        "enabled": 1}).insert()


def _terms() -> str:
    """A template that splits the invoice 50/50 and allocates payments by term."""
    for term, days in (("_T B2B Now", 0), ("_T B2B Later", 30)):
        if not frappe.db.exists("Payment Term", term):
            frappe.get_doc({"doctype": "Payment Term", "payment_term_name": term, "invoice_portion": 50,
                            "due_date_based_on": "Day(s) after invoice date", "credit_days": days}).insert()
    if not frappe.db.exists("Payment Terms Template", TERMS):
        frappe.get_doc({
            "doctype": "Payment Terms Template", "template_name": TERMS, "allocate_payment_based_on_payment_terms": 1,
            "terms": [
                {"payment_term": "_T B2B Now", "invoice_portion": 50, "due_date_based_on": "Day(s) after invoice date",
                 "credit_days": 0},
                {"payment_term": "_T B2B Later", "invoice_portion": 50,
                 "due_date_based_on": "Day(s) after invoice date", "credit_days": 30},
            ],
        }).insert()
    return TERMS


class TestB2BDocuments(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        cls.bank_account, cls.gl = _settings()
        cls.supplier, cls.account = _supplier()
        _buying_price_list()
        _terms()
        frappe.db.set_value("Item", t.ITEM, "is_purchase_item", 1)
        cls.expense = _account(EXPENSE, "Expense", "Profit and Loss")
        cls.cost_center = frappe.get_cached_value("Company", t.COMPANY, "cost_center")
        cls.payer = t.payer()  # sends the requests (Send via M-Pesa)
        cls.manager = t.manager()  # cancels one that may have been paid, having checked the statement
        # moves a paid payout to another document, or books one paid after its request was cancelled: a named
        # M-Pesa Manager (never the shared Administrator login) who may make Payment Entries
        cls.booker = t.named_user("_t-b2b-booker@example.com", "M-Pesa Manager", "Accounts Manager")
        # prepare requests: an Accounts User (reads invoices, not orders), an Expense Approver (reads neither)
        cls.clerk = t.named_user("_t-b2b-clerk@example.com", "Accounts User")
        cls.approver = t.named_user("_t-b2b-approver@example.com", "Expense Approver")
        # Requests an interrupted run committed would show as earlier payouts to the test supplier
        # (it exists only for these tests).
        frappe.db.delete(B2B, {"supplier": cls.supplier})
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        self.tag = frappe.generate_hash(length=8)
        self._no_commit = patch.object(frappe.db, "commit")
        self._no_commit.start()

    def tearDown(self):
        self._no_commit.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.local.mpesa_suite_bank_accounts = None
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")  # a switch a test changed

    # ------------------------------------------------------------ helpers
    def _receipt(self) -> str:
        return "T" + frappe.generate_hash(length=9).upper()

    def _items(self, amount, **row):
        return [{"item_code": t.ITEM, "qty": 1, "rate": amount, "uom": "Nos", "conversion_factor": 1, **row}]

    def _invoice(self, amount, submit=True, **values):
        if not values.get("payment_terms_template"):
            values.setdefault("due_date", nowdate())  # a template sets its own due dates
        pi = frappe.get_doc({
            "doctype": PI, "supplier": self.supplier, "company": t.COMPANY, "posting_date": nowdate(),
            "currency": "KES", "conversion_rate": 1, "buying_price_list": BUYING, "plc_conversion_rate": 1,
            "bill_no": f"B2B-{self.tag}-{frappe.generate_hash(length=4)}", "items": self._items(amount), **values})
        pi.insert()
        if submit:
            pi.submit()
        return pi

    def _order(self, amount):
        schedule = add_days(nowdate(), 3)
        po = frappe.get_doc({
            "doctype": PO, "supplier": self.supplier, "company": t.COMPANY, "currency": "KES", "conversion_rate": 1,
            "buying_price_list": BUYING, "plc_conversion_rate": 1, "transaction_date": nowdate(),
            "schedule_date": schedule, "items": self._items(amount, schedule_date=schedule)})
        po.insert()
        po.submit()
        return po

    def _request(self, amount, send=True, **values):
        doc = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "amount": amount, "company": t.COMPANY,
                              "remarks": f"T {self.tag}", **values})
        doc.insert()
        if send:
            self._send(doc)
        return frappe.get_doc(B2B, doc.name)

    def _send(self, doc, answer=None):
        ack = {"ResponseCode": "0", "ResponseDescription": "Accept the service request successfully.",
               "ConversationID": f"AG_{frappe.generate_hash(length=12)}",
               "OriginatorConversationID": f"OC_{frappe.generate_hash(length=12)}"}
        with patch("mpsa_suite.money_out.b2b.post", return_value=answer or (ack, None)) as post:
            t.send_as(self.payer, doc)
        return post

    def _pay(self, name, charge="Business Pay Bill Charge|KES|2.00", book=True) -> str:
        """Safaricom's Paid Result for the request, then the booking job it enqueues (``book``)."""
        row = frappe.db.get_value(B2B, name, ["conversation_id", "originator_conversation_id"], as_dict=True)
        receipt = self._receipt()
        result = {
            "ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
            "ConversationID": row.conversation_id or f"AG_{receipt}",
            "OriginatorConversationID": row.originator_conversation_id or f"OC_{receipt}", "TransactionID": receipt,
            "ResultParameters": {"ResultParameter": [
                {"Key": "DebitAccountBalance", "Value": "Working Account|KES|50000.00|50000.00|0.00|0.00"},
                {"Key": "TransCompletedTime", "Value": now_datetime().strftime("%Y%m%d%H%M%S")},
                {"Key": "DebitPartyCharges", "Value": charge},
                {"Key": "ReceiverPartyPublicName", "Value": f"888880 - {SUPPLIER.upper()}"},
            ]},
        }
        with patch("frappe.enqueue") as enqueue:
            b2b.handle_result(result, name)
        jobs = [c for c in enqueue.call_args_list if c.args and c.args[0].endswith("b2b.book_payment_entry")]
        self.assertEqual(len(jobs), 1)
        if book:
            b2b.book_payment_entry(name)
        return receipt

    def _entries(self, receipt) -> list:
        return frappe.get_all(PE, filters={"reference_no": receipt, "docstatus": ["<", 2]},
                              fields=["name", "docstatus", "paid_from", "paid_amount", "unallocated_amount",
                                      "posting_date"], order_by="creation asc")

    def _journals(self, receipt) -> list:
        return frappe.get_all(JE, filters={"cheque_no": receipt, "docstatus": ["<", 2]}, fields=["name", "docstatus"],
                              order_by="creation asc")

    def _refs(self, pe) -> list:
        return [(r.reference_doctype, r.reference_name, flt(r.allocated_amount), r.payment_term or None)
                for r in frappe.get_doc(PE, pe).references]

    def _state(self, name):
        return frappe.db.get_value(B2B, name, ["entry_status", "entry_note", "payment_entry", "journal_entry",
                                               "entry_alerted_on", "purchase_invoice", "purchase_order"], as_dict=True)

    def _line(self, key):
        lines = frappe.get_all(BT, filters={"reference_number": key, "docstatus": ["<", 2]},
                               fields=["name", "status", "withdrawal"], order_by="creation asc")
        self.assertEqual(len(lines), 1, lines)
        return lines[0]

    def _outstanding(self, pi) -> float:
        return flt(frappe.db.get_value(PI, pi, "outstanding_amount"))

    def _alerts(self, alert, text) -> list:
        """What a patched supplier_accounts._alert was told that mentions ``text``."""
        return [c.args[0] for c in alert.call_args_list if text in c.args[0]]

    def _bank_payment(self, pi, amount) -> str:
        """The invoice paid another way (a bank Payment Entry) while M-Pesa was paying it."""
        cash = frappe.db.get_value("Account", {"company": t.COMPANY, "account_type": "Cash", "is_group": 0}, "name")
        pe = frappe.new_doc(PE)
        pe.update({"payment_type": "Pay", "company": t.COMPANY, "party_type": "Supplier", "party": self.supplier,
                   "paid_from": cash, "paid_to": frappe.db.get_value(PI, pi, "credit_to"), "paid_amount": amount,
                   "received_amount": amount, "source_exchange_rate": 1, "target_exchange_rate": 1,
                   "reference_no": f"BANK-{self.tag}", "reference_date": nowdate(), "posting_date": nowdate()})
        pe.append("references", {"reference_doctype": PI, "reference_name": pi, "allocated_amount": amount})
        pe.insert()
        pe.submit()
        return pe.name

    def _b2c_row(self, dt, dn, amount, status="Initiated") -> str:
        """An open B2C payout for the document, as the disbursement's submit and jobs leave it."""
        parent = f"_T-B2CPD-{self.tag}"
        frappe.get_doc({"doctype": DISB, "name": parent, "docstatus": 1, "status": "Initiated",
                        "payment_type": "Mpesa Disbursement", "party_type": "Supplier",
                        "transaction_to_pay_against": dt, "posting_date": nowdate()}).db_insert()
        frappe.get_doc({"doctype": DISB_ROW, "name": f"{parent}-R1", "parent": parent, "parenttype": DISB,
                        "parentfield": "references", "idx": 1, "docstatus": 1, "reference_doctype": dt,
                        "reference_name": dn, "party_type": "Supplier", "party": self.supplier,
                        "partyb": "254712345678", "allocated_amount": amount, "payment_status": status}).db_insert()
        return parent

    # ------------------------------------------------------------ a Purchase Invoice, booked automatically
    def test_invoice_paid_by_mpesa_is_booked_on_it_and_reconciled(self):
        pi = self._invoice(10000)
        req = self._request(4000, purchase_invoice=pi.name)
        self.assertEqual((req.supplier, req.supplier_mpesa_account, req.status, req.company),
                         (self.supplier, self.account, "Initiated", t.COMPANY))
        self.assertEqual(req.entry_mode, "Book automatically")
        receipt = self._pay(req.name)

        (pe,) = self._entries(receipt)
        self.assertEqual((pe.docstatus, pe.paid_from, flt(pe.paid_amount), getdate(pe.posting_date)),
                         (1, self.gl, 4000, getdate(nowdate())))
        self.assertEqual(self._refs(pe.name), [(PI, pi.name, 4000, None)])
        state = self._state(req.name)
        self.assertEqual((state.entry_status, state.payment_entry), ("Entry Made", pe.name))
        self.assertEqual(self._outstanding(pi.name), 6000)
        self.assertEqual(self._line(receipt).status, "Reconciled")
        self.assertEqual(self._line(f"CHG-{receipt}").status, "Reconciled")
        self.assertEqual(owed.left_to_pay(PI, pi.name).left, 6000)

        b2b.book_payment_entry(req.name)  # a re-run (re-sent Result, the sweep) books nothing
        self.assertEqual(len(self._entries(receipt)), 1)

    def test_cash_mode_of_payment_on_the_invoice_does_not_change_the_account(self):
        if not frappe.db.exists("Mode of Payment", "Cash"):
            self.skipTest("no Cash Mode of Payment on this site")
        pi = self._invoice(5000, mode_of_payment="Cash")
        receipt = self._pay(self._request(5000, purchase_invoice=pi.name).name)
        (pe,) = self._entries(receipt)
        self.assertEqual(pe.paid_from, self.gl)  # ERPNext's get_payment_entry would use Cash's account (F5)

    def test_term_based_invoice_is_paid_term_by_term(self):
        pi = self._invoice(10000, payment_terms_template=TERMS)
        receipt = self._pay(self._request(7000, purchase_invoice=pi.name).name)
        (pe,) = self._entries(receipt)
        self.assertEqual(self._refs(pe.name), [(PI, pi.name, 5000, "_T B2B Now"), (PI, pi.name, 2000, "_T B2B Later")])
        self.assertEqual(self._outstanding(pi.name), 3000)

    def test_invoice_paid_by_bank_before_the_result_is_an_advance_that_needs_checking(self):
        pi = self._invoice(4000)
        req = self._request(4000, purchase_invoice=pi.name)
        self._bank_payment(pi.name, 4000)
        frappe.db.set_value(B2B, req.name, "entry_alerted_on", now_datetime())  # an earlier alert went out
        with patch("mpsa_suite.money_out.supplier_accounts._alert") as alert:
            receipt = self._pay(req.name)
        (pe,) = self._entries(receipt)
        self.assertEqual((self._refs(pe.name), flt(pe.unallocated_amount)), ([], 4000))
        state = self._state(req.name)
        self.assertEqual(state.entry_status, "Needs Checking")
        self.assertIn("could no longer take this payment (nothing is left to pay on it)", state.entry_note)
        # a possible double payment is always alerted, whatever was alerted before
        self.assertEqual(len(self._alerts(alert, "Check that the supplier was not paid twice.")), 1)

    def test_invoice_paid_in_part_by_bank_before_the_result_needs_checking(self):
        pi = self._invoice(10000)
        req = self._request(10000, purchase_invoice=pi.name)
        self._bank_payment(pi.name, 4000)  # staff paid part of it by bank meanwhile
        with patch("mpsa_suite.money_out.supplier_accounts._alert") as alert:
            receipt = self._pay(req.name)
        (pe,) = self._entries(receipt)
        self.assertEqual((self._refs(pe.name), flt(pe.unallocated_amount)), ([(PI, pi.name, 6000, None)], 4000))
        state = self._state(req.name)
        self.assertEqual(state.entry_status, "Needs Checking")
        self.assertIn("could take only KES 6,000 of this payment, so KES 4,000 is an unallocated", state.entry_note)
        self.assertEqual(len(self._alerts(alert, "Check that the supplier was not paid twice.")), 1)

        b2b_entries.refresh_entries()  # the sweep keeps it, and the note that says why
        self.assertEqual(self._state(req.name).entry_status, "Needs Checking")
        self.assertEqual(self._state(req.name).entry_note, state.entry_note)

    def test_invoice_put_on_hold_after_sending_is_not_allocated(self):
        pi = self._invoice(4000)
        req = self._request(4000, purchase_invoice=pi.name)
        frappe.db.set_value(PI, pi.name, "on_hold", 1)
        receipt = self._pay(req.name)
        (pe,) = self._entries(receipt)
        self.assertEqual(self._refs(pe.name), [])
        self.assertIn("(it is on hold)", self._state(req.name).entry_note)

    def test_the_request_takes_the_invoices_company(self):
        other = frappe.db.get_value("Company", {"name": ["!=", t.COMPANY]}, "name", order_by="creation asc")
        if not other:
            self.skipTest("only one company on this site")
        pi = self._invoice(4000)
        # what Frappe fills from the user's default company on a new request
        req = self._request(4000, send=False, purchase_invoice=pi.name, company=other)
        self.assertEqual(req.company, t.COMPANY)

    def test_supplier_put_on_hold_stops_the_submit_before_any_http_call(self):
        pi = self._invoice(4000)
        req = self._request(4000, send=False, purchase_invoice=pi.name)
        frappe.db.set_value("Supplier", self.supplier, {"on_hold": 1, "hold_type": "Payments", "release_date": None})
        with patch("mpsa_suite.money_out.b2b.post") as post:
            self.assertRaisesRegex(frappe.ValidationError, "is on hold for payments", t.send_as, self.payer, req)
        post.assert_not_called()

    def test_booking_runs_as_administrator_for_a_restricted_callback_user(self):
        user = frappe.get_doc({"doctype": "User", "email": f"_t_b2b_{self.tag}@example.com", "first_name": "B2B",
                               "send_welcome_email": 0, "roles": [{"role": "M-Pesa Viewer"}]}).insert(
            ignore_permissions=True)
        pi = self._invoice(3000)
        req = self._request(3000, purchase_invoice=pi.name)
        frappe.set_user(user.name)  # what the Result's job runs as (Integration User)
        receipt = self._pay(req.name)
        frappe.set_user("Administrator")
        (pe,) = self._entries(receipt)
        self.assertEqual(pe.docstatus, 1)
        self.assertEqual(frappe.db.get_value(PE, pe.name, "owner"), "Administrator")
        self.assertEqual(self._state(req.name).entry_status, "Entry Made")

    # ------------------------------------------------------------ a Purchase Order: an advance
    def test_order_advance_is_picked_up_by_its_invoice(self):
        from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_invoice

        po = self._order(10000)
        req = self._request(4000, purchase_order=po.name)
        receipt = self._pay(req.name)
        (pe,) = self._entries(receipt)
        self.assertEqual(self._refs(pe.name), [(PO, po.name, 4000, None)])
        self.assertEqual(flt(frappe.db.get_value(PO, po.name, "advance_paid")), 4000)
        self.assertEqual(self._state(req.name).entry_status, "Entry Made")
        self.assertEqual(owed.left_to_pay(PO, po.name).left, 6000)

        if not frappe.db.has_column("Payment Entry Reference", "advance_voucher_no"):
            self.skipTest("this ERPNext keeps the order row on the Payment Entry")
        pi = make_purchase_invoice(po.name)
        pi.bill_no = f"B2B-{self.tag}"
        pi.set_advances()
        for adv in pi.advances:
            adv.allocated_amount = adv.advance_amount
        pi.insert()
        pi.submit()
        self.assertEqual(self._outstanding(pi.name), 6000)
        self.assertEqual(b2b_entries.refresh_entry_now(req.name)["entry_status"], "Entry Made")
        result = owed.left_to_pay(PI, pi.name)
        self.assertEqual((result.left, result.parts), (6000, []))  # the advance is on the invoice now

    def test_invoice_takes_off_an_order_advance_not_yet_moved_onto_it(self):
        from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_invoice

        po = self._order(10000)
        self._pay(self._request(4000, purchase_order=po.name).name)
        pi = make_purchase_invoice(po.name)
        pi.bill_no = f"B2B-{self.tag}"
        pi.allocate_advances_automatically = 0
        pi.set("advances", [])
        pi.insert()
        pi.submit()
        result = owed.left_to_pay(PI, pi.name)
        self.assertEqual((result.owed, result.left), (10000, 6000))
        self.assertEqual([p.kind for p in result.parts], ["advance"])
        self.assertRaisesRegex(frappe.ValidationError, "advance still on", self._request, 7000, send=False,
                               purchase_invoice=pi.name)
        self.assertTrue(self._request(6000, send=False, purchase_invoice=pi.name).name)

    def _invoice_from_order_without_advances(self, po):
        from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_invoice

        pi = make_purchase_invoice(po)
        pi.bill_no = f"B2B-{self.tag}-{frappe.generate_hash(length=4)}"
        pi.allocate_advances_automatically = 0
        pi.set("advances", [])
        pi.insert()
        pi.submit()
        return pi

    def test_b2c_disbursement_takes_off_a_b2b_advance_on_the_invoices_order(self):
        po = self._order(10000)
        self._pay(self._request(10000, purchase_order=po.name).name)  # booked on the order
        pi = self._invoice_from_order_without_advances(po.name)
        self.assertEqual(self._outstanding(pi.name), 10000)  # Get Advances not pressed
        disbursement = frappe.get_doc({
            "doctype": DISB, "name": f"_T-B2CPD-ADV-{self.tag}", "payment_type": "Mpesa Disbursement",
            "references": [{"reference_doctype": PI, "reference_name": pi.name, "allocated_amount": 10000,
                            "party_type": "Supplier", "party": self.supplier}]})
        self.assertRaisesRegex(frappe.ValidationError, f"less advance still on {po.name}",
                               disbursement.validate_no_double_payout)

    def test_b2c_disbursement_refuses_an_order_that_has_invoices(self):
        po = self._order(10000)
        pi = self._invoice_from_order_without_advances(po.name)
        self._pay(self._request(10000, purchase_invoice=pi.name).name)  # B2B paid the invoice
        self.assertEqual(flt(frappe.db.get_value(PO, po.name, "advance_paid")), 0)  # the order does not show it
        disbursement = frappe.get_doc({
            "doctype": DISB, "name": f"_T-B2CPD-PO-{self.tag}", "payment_type": "Mpesa Disbursement",
            "references": [{"reference_doctype": PO, "reference_name": po.name, "allocated_amount": 10000,
                            "party_type": "Supplier", "party": self.supplier}]})
        self.assertRaisesRegex(frappe.ValidationError, f"{po.name} already has invoices",
                               disbursement.validate_no_double_payout)

    def test_make_entry_moves_an_order_payout_onto_the_invoice_made_from_it(self):
        po = self._order(10000)
        req = self._request(10000, purchase_order=po.name, entry_mode="Make the entry later")
        receipt = self._pay(req.name)
        pi = self._invoice_from_order_without_advances(po.name)
        # the payout must not count against itself as an advance still on the order. Moving it off the order
        # is a named manager's (the shared Administrator login is refused)
        frappe.db.set_single_value("M-Pesa Suite Settings", "refuse_administrator_payouts", 1)
        self.assertRaisesRegex(frappe.PermissionError, "Use your own login", b2b_entries.make_entry, req.name, PI,
                               purchase_invoice=pi.name)
        self._make_entry_as_booker(req.name, PI, purchase_invoice=pi.name)
        (pe,) = self._entries(receipt)
        self.assertEqual(self._refs(pe.name), [(PI, pi.name, 10000, None)])
        state = self._state(req.name)
        self.assertEqual((state.entry_status, state.purchase_invoice, state.purchase_order),
                         ("Entry Made", pi.name, None))
        self.assertEqual(self._outstanding(pi.name), 0)

    # ------------------------------------------------------------ pay first, make the entry later
    def _paid_later(self, **values):
        req = self._request(4000, supplier=self.supplier, entry_mode="Make the entry later", **values)
        receipt = self._pay(req.name)
        self.assertEqual((self._entries(receipt), self._journals(receipt)), ([], []))
        self.assertEqual(self._state(req.name).entry_status, "Entry Not Made")
        self.assertEqual(self._line(receipt).status, "Unreconciled")  # its line waits for the entry
        self.assertEqual(self._line(f"CHG-{receipt}").status, "Reconciled")  # the charge is booked anyway
        self.assertEqual(len(self._journals(f"CHG-{receipt}")), 1)
        return req, receipt

    def _made(self, req, receipt, entry_doctype):
        entries = self._entries(receipt) + self._journals(receipt)
        self.assertEqual(len(entries), 1, entries)  # the receipt is on exactly one entry
        self.assertEqual(self._state(req.name).entry_status, "Entry Made")
        self.assertEqual(self._line(receipt).status, "Reconciled")
        self.assertRaisesRegex(frappe.ValidationError, "is already on", b2b_entries.make_entry, req.name,
                               "Supplier Advance")
        return entries[0].name

    def test_make_entry_against_an_invoice(self):
        req, receipt = self._paid_later()
        pi = self._invoice(10000)
        b2b_entries.make_entry(req.name, PI, purchase_invoice=pi.name)
        pe = self._made(req, receipt, PE)
        self.assertEqual(self._refs(pe), [(PI, pi.name, 4000, None)])
        self.assertEqual(self._state(req.name).purchase_invoice, pi.name)

    def test_make_entry_against_an_order(self):
        req, receipt = self._paid_later()
        po = self._order(10000)
        b2b_entries.make_entry(req.name, PO, purchase_order=po.name)
        pe = self._made(req, receipt, PE)
        self.assertEqual(self._refs(pe), [(PO, po.name, 4000, None)])

    def test_make_entry_as_a_supplier_advance(self):
        req, receipt = self._paid_later()
        b2b_entries.make_entry(req.name, "Supplier Advance")
        pe = self._made(req, receipt, PE)
        self.assertEqual(flt(frappe.db.get_value(PE, pe, "unallocated_amount")), 4000)

    def test_make_entry_as_a_journal_entry_to_an_expense(self):
        req, receipt = self._paid_later()
        b2b_entries.make_entry(req.name, "Journal Entry", account=self.expense, cost_center=self.cost_center,
                               remark="Office rent")
        je = self._made(req, receipt, JE)
        doc = frappe.get_doc(JE, je)
        self.assertEqual((doc.docstatus, doc.voucher_type, doc.cheque_no, getdate(doc.cheque_date)),
                         (1, "Bank Entry", receipt, getdate(nowdate())))
        rows = {(a.account, flt(a.debit_in_account_currency), flt(a.credit_in_account_currency)) for a in doc.accounts}
        self.assertEqual(rows, {(self.expense, 4000, 0), (self.gl, 0, 4000)})
        if frappe.get_meta(JE).has_field("custom_remark"):  # v16
            self.assertEqual(doc.custom_remark, 1)
            self.assertIn("Office rent", doc.remark)

    def test_draft_entry_keeps_the_invoice_blocked_until_submitted(self):
        frappe.db.set_value("Mpesa Settings", SETTINGS, "submit_b2c_accounting_entries", 0)
        try:
            self._draft_entry_keeps_the_invoice_blocked()
        finally:  # the rollback undoes it too; never left for the next test
            frappe.db.set_value("Mpesa Settings", SETTINGS, "submit_b2c_accounting_entries", 1)

    def _draft_entry_keeps_the_invoice_blocked(self):
        pi = self._invoice(10000)
        req = self._request(4000, purchase_invoice=pi.name)
        receipt = self._pay(req.name)
        (pe,) = self._entries(receipt)
        self.assertEqual((pe.docstatus, self._state(req.name).entry_status), (0, "Draft Entry"))
        self.assertEqual(self._outstanding(pi.name), 10000)
        self.assertEqual(owed.left_to_pay(PI, pi.name).left, 6000)  # the draft does not free the money (R8)
        self.assertRaisesRegex(frappe.ValidationError, "more than is left", self._request, 7000, send=False,
                               purchase_invoice=pi.name)

        frappe.delete_doc(PE, pe.name)  # staff throw the draft away
        self.assertEqual(b2b_entries.refresh_entry_now(req.name)["entry_status"], "Entry Not Made")
        b2b_entries.make_entry(req.name, PI, purchase_invoice=pi.name)
        (made,) = self._entries(receipt)
        self.assertEqual((made.docstatus, self._outstanding(pi.name)), (1, 6000))
        self.assertEqual(self._line(receipt).status, "Reconciled")

    def test_a_cancelled_entry_puts_the_payout_back_on_the_invoice(self):
        pi = self._invoice(10000)
        req = self._request(4000, purchase_invoice=pi.name)
        receipt = self._pay(req.name)
        (pe,) = self._entries(receipt)
        frappe.get_doc(PE, pe.name).cancel()  # the request links it: still allowed
        self.assertEqual(owed.left_to_pay(PI, pi.name).left, 6000)  # counted again
        self.assertEqual(b2b_entries.refresh_entry_now(req.name)["entry_status"], "Entry Not Made")
        self.assertIsNone(self._state(req.name).payment_entry)
        self.assertEqual(self._line(receipt).status, "Unreconciled")
        self.assertEqual(frappe.db.get_value(B2B, req.name, "entry_mode"), "Make the entry later")

    def test_a_cancelled_automatic_entry_is_not_booked_again_by_the_sweep(self):
        pi = self._invoice(10000)
        req = self._request(4000, purchase_invoice=pi.name)
        receipt = self._pay(req.name)
        (pe,) = self._entries(receipt)
        frappe.get_doc(PE, pe.name).cancel()  # it went to the wrong invoice, say
        b2b_entries.refresh_entries()
        self.assertEqual(self._entries(receipt), [])  # left to Make Entry
        state = self._state(req.name)
        self.assertEqual((state.entry_status, state.payment_entry), ("Entry Not Made", None))
        self.assertEqual(frappe.db.get_value(B2B, req.name, "entry_mode"), "Make the entry later")

    def test_desk_cancel_of_an_entry_does_not_offer_to_cancel_the_request(self):
        from frappe.desk.form.linked_with import get_submitted_linked_docs

        pi = self._invoice(10000)
        req = self._request(4000, purchase_invoice=pi.name)
        (pe,) = self._entries(self._pay(req.name))
        linked = get_submitted_linked_docs(PE, pe.name)["docs"]
        self.assertNotIn(req.name, [d["name"] for d in linked])  # hooks.auto_cancel_exempted_doctypes

    # ------------------------------------------------------------ guards
    def test_a_second_entry_for_the_receipt_is_refused(self):
        pi = self._invoice(10000)
        receipt = self._pay(self._request(4000, purchase_invoice=pi.name).name)
        pe = frappe.new_doc(PE)
        pe.update({"payment_type": "Pay", "company": t.COMPANY, "party_type": "Supplier", "party": self.supplier,
                   "paid_from": self.gl, "paid_amount": 4000, "received_amount": 4000, "source_exchange_rate": 1,
                   "target_exchange_rate": 1, "reference_no": receipt, "reference_date": nowdate(),
                   "posting_date": nowdate()})
        pe.setup_party_account_field()
        pe.set_missing_values()
        self.assertRaisesRegex(frappe.ValidationError, "already recorded on Payment Entry", pe.insert)

    def test_typed_paybill_inserted_already_submitted_is_refused(self):
        # frappe.client.insert with docstatus 1 runs before_submit, not the draft-only validate (F1):
        # it is no deliberate step (Send via M-Pesa), and inside one the typed paybill is refused too
        values = {"doctype": B2B, "mpesa_settings": SETTINGS, "command_id": "BusinessPayBill",
                  "receiver_shortcode": "555555", "account_reference": "X1", "amount": 100, "remarks": "typed",
                  "docstatus": 1}
        frappe.set_user(self.payer)
        with patch("mpsa_suite.money_out.b2b.post") as post:
            self.assertRaisesRegex(frappe.ValidationError, "Send via M-Pesa", frappe.get_doc(values).insert)
            with send_confirm.deliberate("server code"):
                # its maker sends it: refused by maker-checker, and with that off (one-person team)...
                self.assertRaisesRegex(frappe.ValidationError, "another person must send it",
                                       frappe.get_doc(values).insert)
                frappe.db.set_single_value("M-Pesa Suite Settings", "schedule_maker_checker", 0)
                frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")
                try:  # ...by the destination check (the test's rollback puts the switch back)
                    self.assertRaisesRegex(frappe.ValidationError, "approved M-Pesa account",
                                           frappe.get_doc(values).insert)
                finally:
                    frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")
        post.assert_not_called()

    def test_only_send_via_mpesa_sends_a_request(self):
        """The form's Submit, the list's bulk Submit, frappe.client.submit and run_doc_method all end in
        doc.submit(), which before_submit refuses; Send via M-Pesa's confirm is the one way."""
        from frappe.desk.doctype.bulk_update.bulk_update import submit_cancel_or_update_docs

        pi = self._invoice(10000)
        req = self._request(4000, send=False, purchase_invoice=pi.name)
        frappe.set_user(self.payer)
        with patch("mpsa_suite.money_out.b2b.post") as post:
            self.assertRaisesRegex(frappe.ValidationError, "Send via M-Pesa", frappe.get_doc(B2B, req.name).submit)
            # Actions > Submit on the list: each refused document is reported back, nothing sent (its
            # rollback is the test's own here)
            with patch.object(frappe.db, "rollback"):
                self.assertEqual(submit_cancel_or_update_docs(B2B, [req.name], "submit"), [req.name])
        post.assert_not_called()
        self.assertEqual(frappe.db.get_value(B2B, req.name, "docstatus"), 0)
        frappe.set_user("Administrator")
        self._send(req)
        self.assertEqual(frappe.db.get_value(B2B, req.name, ["docstatus", "status"]), (1, "Initiated"))

    def test_the_shared_administrator_login_cannot_send(self):
        pi = self._invoice(10000)
        req = self._request(4000, send=False, purchase_invoice=pi.name)
        frappe.db.set_single_value("M-Pesa Suite Settings", "refuse_administrator_payouts", 1)
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")
        try:
            self.assertRaisesRegex(frappe.PermissionError, "Use your own login", send_confirm.send_for_test, req)
            with send_confirm.deliberate("server code", req):
                self.assertRaisesRegex(frappe.PermissionError, "Use your own login", req.submit)
        finally:
            frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")
        self.assertEqual(frappe.db.get_value(B2B, req.name, "docstatus"), 0)

    def test_the_payer_who_prepared_a_request_cannot_send_it(self):
        pi = self._invoice(10000)
        frappe.set_user(self.payer)  # maker-checker: who prepared it is shown why, and gets no token
        try:
            req = self._request(4000, send=False, purchase_invoice=pi.name)
            found = send_confirm.prepare_send(B2B, req.name)
        finally:
            frappe.set_user("Administrator")
        self.assertIsNone(found["token"])
        self.assertIn("another person must send it", found["problems"][0])
        self.assertRaisesRegex(frappe.ValidationError, "another person must send it", t.send_as, self.payer, req)

    def test_earlier_payout_warning_blocks_submit_until_ticked(self):
        self._paid_later()  # money the supplier got with no invoice
        pi = self._invoice(10000)
        req = self._request(3000, send=False, purchase_invoice=pi.name)
        with patch("mpsa_suite.money_out.b2b.post") as post:
            self.assertRaisesRegex(frappe.ValidationError, "Check that none of them paid this invoice", t.send_as,
                                   self.payer, req)
        post.assert_not_called()
        req = frappe.get_doc(B2B, req.name)
        req.checked_earlier_payouts = 1
        req.save()
        self._send(req)
        self.assertEqual(frappe.db.get_value(B2B, req.name, "status"), "Initiated")

    def test_a_save_after_submit_keeps_what_the_server_wrote(self):
        pi = self._invoice(10000)
        req = self._request(4000, purchase_invoice=pi.name)
        doc = frappe.get_doc(B2B, req.name)
        doc.status = "Failed"  # it would stop counting against the invoice
        doc.transaction_id = "TFORGED001"
        doc.entry_status = "Entry Made"
        doc.save()
        row = frappe.db.get_value(B2B, req.name, ["status", "transaction_id", "entry_status"], as_dict=True)
        # an empty Select reads back as ""
        self.assertEqual((row.status, row.transaction_id, row.entry_status or None), ("Initiated", None, None))
        self.assertEqual(owed.left_to_pay(PI, pi.name).left, 6000)

    def _make_entry_as_booker(self, *args, **kwargs):
        frappe.set_user(self.booker)
        try:
            return b2b_entries.make_entry(*args, **kwargs)
        finally:
            frappe.set_user("Administrator")

    def _cancel_as_manager(self, name):
        """An M-Pesa Manager cancels the request, having checked the statement."""
        frappe.set_user(self.manager)
        try:
            frappe.get_doc(B2B, name).cancel()
        finally:
            frappe.set_user("Administrator")

    def test_a_cancelled_request_that_may_have_paid_cannot_be_deleted(self):
        pi = self._invoice(10000)
        req = self._request(4000, send=False, purchase_invoice=pi.name)
        self._send(req, answer=(None, "unknown"))  # no answer from M-Pesa
        frappe.db.set_single_value("M-Pesa Suite Settings", "refuse_administrator_payouts", 1)
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")
        # not by the shared Administrator login (decision 1): it holds every role but checked nothing
        self.assertRaisesRegex(frappe.PermissionError, "Use your own login", frappe.get_doc(B2B, req.name).cancel)
        self.assertEqual(frappe.db.get_value(B2B, req.name, "docstatus"), 1)
        self._cancel_as_manager(req.name)  # an admin checked the statement
        self.assertRaisesRegex(frappe.ValidationError, "is the record of that payment", frappe.delete_doc, B2B,
                               req.name)
        self.assertTrue(frappe.db.exists(B2B, req.name))
        draft = self._request(1000, send=False, purchase_invoice=pi.name)
        frappe.delete_doc(B2B, draft.name)  # never sent: may go
        self.assertFalse(frappe.db.exists(B2B, draft.name))

    def test_a_manager_can_cancel_an_initiated_request_whose_result_never_came(self):
        pi = self._invoice(10000)
        req = self._request(4000, purchase_invoice=pi.name)
        self._cancel_as_manager(req.name)  # after checking the statement
        self.assertEqual(owed.left_to_pay(PI, pi.name).left, 10000)
        with patch("mpsa_suite.money_out.supplier_accounts._alert") as alert:
            self._pay(req.name)  # the Result comes after all: recorded, never booked
        self.assertEqual(self._state(req.name).entry_status, "Needs Checking")
        self.assertEqual(len(self._alerts(alert, "M-Pesa paid this after the request was cancelled")), 1)
        self.assertEqual(owed.left_to_pay(PI, pi.name).left, 6000)

    # ------------------------------------------------------------ Send via M-Pesa on the B2C payouts
    def _b2c_disbursement(self, pi: str, amount):
        """A direct B2C Payment Disbursement to the supplier's phone for ``pi``: a saved draft, made
        as Pay Selected makes one (money_out.schedule._pay_b2c_group), prepared by Administrator."""
        doc = frappe.get_doc({
            "doctype": DISB, "naming_series": "MPESA-B2C-.YYYY.-", "company": t.COMPANY, "posting_date": nowdate(),
            "payment_type": "Mpesa Disbursement", "party_type": "Supplier", "transaction_to_pay_against": PI,
            "mode_of_payment": f"Mpesa-{SETTINGS}", "paid_from": self.gl, "paid_from_account_currency": "KES",
            "paid_to": frappe.db.get_value(PI, pi, "credit_to"), "paid_to_account_currency": "KES",
            "paid_amount": amount, "source_exchange_rate": 1, "target_exchange_rate": 1,
            "references": [{"reference_doctype": PI, "reference_name": pi, "party_type": "Supplier",
                            "party": self.supplier, "allocated_amount": amount, "partyb": B2C_PHONE,
                            "currency": "KES", "exchange_rate": 1}]})
        doc.insert()
        return doc

    def test_send_via_mpesa_sends_a_b2c_disbursement_and_its_jobs_make_the_payouts(self):
        """The real rows in the summary, a plain submit refused, then the submit inside the deliberate
        step: its job (create_b2c_request, run as the payer, as the queue runs it) makes and submits
        the B2C request inside its own, and that request is sent (the send is replaced)."""
        from mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request import (
            b2c_disbursement_request as req_module,
        )
        from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement import (
            b2c_payment_disbursement as disb,
        )
        from mpsa_suite.utils.phone import mask

        pi = self._invoice(5000)
        doc = self._b2c_disbursement(pi.name, 1500)
        frappe.set_user(self.payer)
        try:
            summary = send_confirm.prepare_send(DISB, doc.name)
            self.assertRaisesRegex(frappe.ValidationError, "Send via M-Pesa", frappe.get_doc(DISB, doc.name).submit)
        finally:
            frappe.set_user("Administrator")
        self.assertTrue(summary["token"])
        self.assertEqual(summary["problems"], [])
        self.assertEqual([(r["receiver"], r["destination"], r["amount"], r["document"]) for r in summary["rows"]],
                         [(SUPPLIER, f"M-Pesa phone {mask(B2C_PHONE)}", 1500.0, f"{PI} {pi.name}")])
        self.assertEqual(summary["total"], 1500.0)

        with patch("frappe.enqueue") as enqueue:
            t.send_as(self.payer, doc)
        self.assertEqual(frappe.db.get_value(DISB, doc.name, ["docstatus", "status"]), (1, "Initiated"))
        jobs = [c for c in enqueue.call_args_list if c.args and str(c.args[0]).endswith("create_b2c_request")]
        self.assertEqual(len(jobs), 1)
        sent = Mock()
        frappe.set_user(self.payer)  # a queued job runs as who queued it
        try:
            with patch.object(req_module, "make_b2c_payment_request", sent):
                for job in jobs:
                    disb.create_b2c_request(job.kwargs["ref"], job.kwargs["settings"], job.kwargs["b2c_disbursement"])
        finally:
            frappe.set_user("Administrator")
        request = frappe.db.get_value(DISB_ROW, {"parent": doc.name}, "b2c_disbursement_request")
        self.assertTrue(request)
        row = frappe.db.get_value(B2C_REQ, request, ["docstatus", "amount", "phone_number", "owner"], as_dict=True)
        self.assertEqual((row.docstatus, flt(row.amount), row.phone_number, row.owner),
                         (1, 1500, B2C_PHONE, self.payer))
        sent.assert_called_once_with(request)

    def test_send_via_mpesa_sends_a_direct_b2c_request(self):
        from mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request import (
            b2c_disbursement_request as req_module,
        )
        from mpsa_suite.utils.phone import mask

        pi = self._invoice(5000)
        doc = self._b2c_disbursement(pi.name, 1500)
        req = frappe.get_doc({"doctype": B2C_REQ, "amount": 1500, "currency": "KES", "mpesa_settings": SETTINGS,
                              "reference_doctype": PI, "reference_name": pi.name, "party_type": "Supplier",
                              "party": self.supplier, "phone_number": B2C_PHONE, "b2c_payment_disbursement": doc.name,
                              "b2c_payment_disbursement_reference": doc.references[0].name})
        req.insert()
        sent = Mock()
        with patch.object(req_module, "make_b2c_payment_request", sent):
            frappe.set_user(self.payer)
            try:
                summary = send_confirm.prepare_send(B2C_REQ, req.name)
            finally:
                frappe.set_user("Administrator")
            self.assertTrue(summary["token"])
            self.assertEqual([(r["receiver"], r["destination"], r["amount"]) for r in summary["rows"]],
                             [(SUPPLIER, f"M-Pesa phone {mask(B2C_PHONE)}", 1500.0)])
            sent.assert_not_called()
            t.send_as(self.payer, req)
        self.assertEqual(frappe.db.get_value(B2C_REQ, req.name, "docstatus"), 1)
        sent.assert_called_once_with(req.name)

    def _supplier_phone(self) -> str:
        """An approved Phone (B2C) Supplier M-Pesa Account of the supplier for B2C_PHONE (written as approved)."""
        doc = frappe.get_doc({
            "doctype": "Supplier M-Pesa Account", "supplier": self.supplier, "account_type": "Phone (B2C)",
            "phone": B2C_PHONE, "expected_name": SUPPLIER, "is_default": 0, "docstatus": 1,
            "proof": "/private/files/_t_b2b_proof.pdf", "approved_by": "Administrator", "approved_on": now_datetime(),
            "active_from": add_days(now_datetime(), -1), "creation": now_datetime(), "modified": now_datetime(),
            "owner": "Administrator"})
        doc.name = f"_T-MSA-B2BDOCS-P-{self.tag}"
        doc.db_insert()
        return doc.name

    def test_a_supplier_with_an_approved_phone_is_paid_only_to_it_by_hand_and_on_retry(self):
        # verification K15: a B2C request made by hand to another number is refused; a failed payout is not sent
        # again once the approved account its row named is cancelled (a stolen SIM), though it was the only one
        from mpsa_suite.money_out import employee_documents

        account = self._supplier_phone()
        pi = self._invoice(5000)
        doc = self._b2c_disbursement(pi.name, 1500)
        self.assertEqual(doc.references[0].payee_account, account)
        values = {"doctype": B2C_REQ, "amount": 1500, "currency": "KES", "mpesa_settings": SETTINGS,
                  "reference_doctype": PI, "reference_name": pi.name, "party_type": "Supplier", "party": self.supplier,
                  "b2c_payment_disbursement": doc.name, "b2c_payment_disbursement_reference": doc.references[0].name}
        self.assertRaisesRegex(frappe.ValidationError, "254733999888 is not a phone approved for",
                               frappe.get_doc(dict(values, phone_number="254733999888")).insert)
        # leaving its party out does not skip the supplier's rule: a request pays exactly its row
        self.assertRaisesRegex(frappe.ValidationError, "party type, party, phone number is not that of",
                               frappe.get_doc(dict(values, party_type=None, party=None,
                                                   phone_number="254733999888")).insert)
        request = frappe.get_doc(dict(values, phone_number=B2C_PHONE)).insert()
        frappe.db.set_value(B2C_REQ, request.name, {"docstatus": 1, "status": "Failed"})
        employee_documents.check_b2c_retry(frappe.get_doc(B2C_REQ, request.name))  # may be sent again
        frappe.db.set_value("Supplier M-Pesa Account", account, "docstatus", 2)
        self.assertRaisesRegex(frappe.ValidationError, f"{account} is not approved \\(cancelled\\)",
                               employee_documents.check_b2c_retry, frappe.get_doc(B2C_REQ, request.name))

    def test_a_draft_names_an_invoice_or_order_only_for_who_may_read_it(self):
        # verification K9: an Expense Approver (create on the request, no read on invoices) does not learn an
        # invoice's supplier or what is left to pay; an Accounts User names an order by its invoice read (ERPNext
        # gives Purchase Order read only to the Purchase and Stock roles); a payer saving a draft whose invoice is
        # unchanged needs no read on it, one changing it does
        pi, other = self._invoice(5000), self._invoice(3000)
        po = self._order(4000)
        values = {"doctype": B2B, "mpesa_settings": SETTINGS, "amount": 1000, "company": t.COMPANY,
                  "remarks": f"T {self.tag}"}
        frappe.set_user(self.approver)
        try:
            draft = frappe.get_doc(dict(values, purchase_invoice=pi.name))
            frappe.flags.error_message = None
            self.assertRaises(frappe.PermissionError, draft.insert)
            self.assertIn(f"No permission for Purchase Invoice {pi.name}", str(frappe.flags.error_message))
            self.assertFalse(draft.get("supplier"))
        finally:
            frappe.set_user("Administrator")
        frappe.set_user(self.clerk)
        try:
            self.assertFalse(frappe.has_permission(PO, "read"))
            request = frappe.get_doc(dict(values, purchase_order=po.name)).insert()
            self.assertEqual(request.supplier, self.supplier)
        finally:
            frappe.set_user("Administrator")
        request = self._request(1000, send=False, purchase_invoice=pi.name)  # prepared by Administrator
        frappe.set_user(self.payer)
        try:
            self.assertFalse(frappe.has_permission(PI, "read"))
            doc = frappe.get_doc(B2B, request.name)
            doc.remarks = f"Checked {self.tag}"
            doc.save()
            doc.purchase_invoice = other.name
            frappe.flags.error_message = None
            self.assertRaises(frappe.PermissionError, doc.save)
            self.assertIn(f"No permission for Purchase Invoice {other.name}", str(frappe.flags.error_message))
        finally:
            frappe.set_user("Administrator")
        self.assertEqual(frappe.db.get_value(B2B, request.name, "purchase_invoice"), pi.name)

    # ------------------------------------------------------------ B2B and B2C see each other
    def test_open_b2c_payout_counts_against_a_b2b_request(self):
        pi = self._invoice(10000)
        parent = self._b2c_row(PI, pi.name, 3000)
        self.assertRaisesRegex(frappe.ValidationError, f"B2C {parent}", self._request, 8000, send=False,
                               purchase_invoice=pi.name)
        self._request(7000, purchase_invoice=pi.name)

    def test_initiated_b2b_refuses_a_b2c_disbursement(self):
        pi = self._invoice(10000)
        req = self._request(2000, purchase_invoice=pi.name)
        disbursement = frappe.get_doc({
            "doctype": DISB, "name": f"_T-B2CPD-NEW-{self.tag}", "payment_type": "Mpesa Disbursement",
            "references": [{"reference_doctype": PI, "reference_name": pi.name, "allocated_amount": 1000,
                            "party_type": "Supplier", "party": self.supplier}]})
        self.assertRaisesRegex(frappe.ValidationError, f"awaiting accounting: {req.name}",
                               disbursement.validate_no_double_payout)

    # ------------------------------------------------------------ checked again at send
    def _submitted_by_server_code(self, amount=1000, **values) -> str:
        """A request submitted with flags.ignore_validate: only the send-time checks stand."""
        name = f"_T-MB2B-{self.tag}"
        frappe.get_doc({
            "doctype": B2B, "name": name, "docstatus": 1, "status": "Not Initiated", "mpesa_settings": SETTINGS,
            "command_id": "BusinessPayBill", "receiver_shortcode": "888880", "account_reference": "TB2B-1",
            "amount": amount, "supplier": self.supplier, "supplier_mpesa_account": self.account,
            "company": t.COMPANY, "remarks": "server code", "entry_mode": "Book automatically", **values,
        }).db_insert()
        return name

    def test_send_refuses_an_invoice_put_on_hold(self):
        pi = self._invoice(5000)
        frappe.db.set_value(PI, pi.name, "on_hold", 1)
        name = self._submitted_by_server_code(purchase_invoice=pi.name)
        with patch("mpsa_suite.money_out.b2b.post") as post:
            self.assertRaisesRegex(frappe.ValidationError, "is on hold", b2b.send_b2b, name)
        post.assert_not_called()

    def test_send_refuses_more_than_is_left_now(self):
        pi = self._invoice(5000)
        self._b2c_row(PI, pi.name, 4500)
        name = self._submitted_by_server_code(1000, purchase_invoice=pi.name)
        with patch("mpsa_suite.money_out.b2b.post") as post:
            self.assertRaisesRegex(frappe.ValidationError, "more than is left", b2b.send_b2b, name)
        post.assert_not_called()

    def test_send_refuses_a_changed_destination(self):
        name = self._submitted_by_server_code(receiver_shortcode="999999")
        with patch("mpsa_suite.money_out.b2b.post") as post:
            self.assertRaisesRegex(frappe.ValidationError, "does not match Supplier M-Pesa Account", b2b.send_b2b,
                                   name)
        post.assert_not_called()

    # ------------------------------------------------------------ paid after it was cancelled
    def test_payment_after_the_request_was_cancelled_is_recorded_never_booked(self):
        pi = self._invoice(10000)
        req = self._request(4000, send=False, purchase_invoice=pi.name)
        self._send(req, answer=(None, "unknown"))  # no answer from M-Pesa
        req = frappe.get_doc(B2B, req.name)
        self.assertEqual(req.status, "Unknown")
        # A named M-Pesa Manager checked the statement and found nothing (yet). The shared
        # Administrator login may not cancel a request that may have paid (decision 1).
        self._cancel_as_manager(req.name)
        req = frappe.get_doc(B2B, req.name)
        self.assertEqual(req.docstatus, 2)
        self.assertEqual(owed.left_to_pay(PI, pi.name).left, 10000)

        with patch("mpsa_suite.money_out.supplier_accounts._alert") as alert:
            receipt = self._pay(req.name)  # then the Result arrives: the money left
        row = frappe.db.get_value(B2B, req.name, ["docstatus", "status", "transaction_id"], as_dict=True)
        self.assertEqual((row.docstatus, row.status, row.transaction_id), (2, "Paid", receipt))
        self.assertEqual(self._entries(receipt), [])
        state = self._state(req.name)
        self.assertEqual(state.entry_status, "Needs Checking")
        self.assertEqual(len(self._alerts(alert, "M-Pesa paid this after the request was cancelled")), 1)
        self.assertEqual(self._line(receipt).status, "Unreconciled")  # its line and charge are still made
        self.assertEqual(owed.left_to_pay(PI, pi.name).left, 6000)  # and it counts against the invoice

        self._make_entry_as_booker(req.name, PI, purchase_invoice=pi.name)  # a named manager books it
        self.assertEqual(self._state(req.name).entry_status, "Entry Made")
        self.assertEqual(self._outstanding(pi.name), 6000)

    # ------------------------------------------------------------ the 15-minute sweep
    def test_failed_booking_is_tried_again_by_the_sweep(self):
        pi = self._invoice(10000)
        req = self._request(4000, purchase_invoice=pi.name)
        with patch("mpsa_suite.money_out.b2b_entries.build_payment_entry",
                   side_effect=frappe.ValidationError("Accounting Period is closed")):
            receipt = self._pay(req.name)
        state = self._state(req.name)
        self.assertEqual(state.entry_status, "Entry Not Made")
        self.assertTrue(state.entry_note.startswith("Could not book automatically: Accounting Period is closed."))
        self.assertEqual(self._entries(receipt), [])

        b2b_entries.refresh_entries()
        (pe,) = self._entries(receipt)
        self.assertEqual((pe.docstatus, self._state(req.name).entry_status), (1, "Entry Made"))
        self.assertEqual(self._line(receipt).status, "Reconciled")

    def test_a_lost_booking_job_is_booked_by_the_sweep(self):
        pi = self._invoice(10000)
        req = self._request(4000, purchase_invoice=pi.name)
        receipt = self._pay(req.name, book=False)  # the job never ran (a lock timeout, Redis down)
        state = self._state(req.name)
        self.assertEqual((state.entry_status, state.entry_note), ("Entry Not Made", "Booking the entry…"))
        self.assertTrue(frappe.db.get_value(B2B, req.name, "entry_attempted_on"))  # set by the Result

        b2b_entries.refresh_entries()
        (pe,) = self._entries(receipt)
        self.assertEqual((pe.docstatus, self._state(req.name).entry_status), (1, "Entry Made"))
        self.assertEqual(self._refs(pe.name), [(PI, pi.name, 4000, None)])

    def _paid_row(self, receipt, **values) -> str:
        name = f"_T-MB2B-{frappe.generate_hash(length=8)}"
        frappe.get_doc({
            "doctype": B2B, "name": name, "docstatus": 1, "status": "Paid", "mpesa_settings": SETTINGS,
            "command_id": "BusinessPayBill", "receiver_shortcode": "888880", "account_reference": "TB2B-1",
            "amount": 500, "supplier": self.supplier, "company": t.COMPANY, "transaction_id": receipt,
            "trans_completed_time": now_datetime(), "result_code": "0", "remarks": "old",
            "creation": now_datetime(), **values}).db_insert()
        return name

    def test_sweep_never_books_old_rows(self):
        legacy = self._paid_row(self._receipt())  # paid before this release: never tried
        old = self._paid_row(self._receipt(), entry_status="Entry Not Made", entry_attempted_on=now_datetime(),
                             creation=add_to_date(now_datetime(), days=-40))  # outside the 30 days
        b2b_entries.refresh_entries()
        for name in (legacy, old):
            receipt = frappe.db.get_value(B2B, name, "transaction_id")
            self.assertEqual(self._entries(receipt), [])
            self.assertEqual(frappe.db.get_value(B2B, name, "entry_status"), "Entry Not Made")
        self.assertIsNone(frappe.db.get_value(B2B, legacy, "entry_attempted_on"))

    def test_sweep_links_a_journal_entry_staff_made_with_the_receipt(self):
        receipt = self._receipt()
        name = self._paid_row(receipt, entry_status="Entry Not Made", entry_mode="Make the entry later",
                              entry_attempted_on=now_datetime())
        je = frappe.get_doc({"doctype": JE, "voucher_type": "Bank Entry", "company": t.COMPANY,
                             "posting_date": nowdate(), "cheque_no": receipt, "cheque_date": nowdate(),
                             "accounts": [{"account": self.expense, "debit_in_account_currency": 500,
                                           "cost_center": self.cost_center},
                                          {"account": self.gl, "credit_in_account_currency": 500}]})
        je.insert()
        je.submit()  # what Bank Reconciliation's Create Voucher makes
        b2b_entries.refresh_entries()
        state = self._state(name)
        self.assertEqual((state.journal_entry, state.entry_status), (je.name, "Entry Made"))
