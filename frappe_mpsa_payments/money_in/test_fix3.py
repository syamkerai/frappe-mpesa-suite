"""Round 3 review fixes on a real database (v15 and v16).

P1: an STK prompt whose receipt a cashier put on a sales invoice (POS quick pay: the register is
submitted with no Payment Entry) is never booked: the duplicate check raises, the STK booking and
a manager's Reconcile leave the register and the invoice as they are. P2: a disbursement submit
is refused by another disbursement's Paid payout that nothing books (its Payment Entry cancelled,
or Cancel All), and an amended disbursement counts the Paid payout its copied row links against
what the invoice owes; a Salary Slip is left to its own owed. P3: a reversal result on a draft
register whose receipt an STK prompt booked asks for that booking to be undone by hand. P7: the
STK booking names its receipt in frappe.flags while the reference document books it. P8: the
prepaid-order booker skips a register whose receipt has a live reversal on another register.
P9: a register inserted already submitted finds its receipt's reversal. P11: a register
submitted with neither Payment Entry nor invoice pays a payment intent; one on an invoice does
not.

Nothing is sent to Safaricom; the B2C rows are written with db_insert (no accounts needed).
"""

from unittest.mock import patch

import frappe
from frappe.utils import now_datetime, nowdate

from frappe_mpsa_payments.compat import IntegrationTestCase
from frappe_mpsa_payments.frappe_mpsa_payments.api import mpesa_quick_pay
from frappe_mpsa_payments.frappe_mpsa_payments.api.payment_entry import create_payment_entry
from frappe_mpsa_payments.frappe_mpsa_payments.doctype.b2c_payment_disbursement import (
    b2c_payment_disbursement as disbursement_module,
)
from frappe_mpsa_payments.gateway import intents
from frappe_mpsa_payments.money_in import booking, matching, stk_verify
from frappe_mpsa_payments.money_in import testing as t
from frappe_mpsa_payments.money_in.common import BOOKING_RECEIPT_FLAG, EXPRESS, REGISTER, REVERSAL, REVERSAL_HOLDS
from frappe_mpsa_payments.money_out import reversal as reversal_module
from frappe_mpsa_payments.utils import utils as mpesa_utils

INTENT = intents.INTENT
MOP = f"Mpesa-{t.SETTINGS}"
PARENT = "B2C Payment Disbursement"
REF = "B2C Payment Disbursement Reference"
REQ = "B2C Disbursement Request"
INTENT_CLASS = (
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_payment_intent.mpesa_payment_intent."
    "MpesaPaymentIntent.on_payment_authorized"
)


def _code() -> str:
    return "T" + frappe.generate_hash(length=9).upper()


def _live_entries(receipt: str) -> list:
    return frappe.get_all("Payment Entry", filters={"reference_no": receipt, "docstatus": ["<", 2]}, pluck="name")


def _register(amount: float, billref: str, transid: str | None = None, **values) -> str:
    doc = frappe.get_doc({"doctype": REGISTER, **t.confirmation_payload(amount, billref=billref, transid=transid),
                          **values})
    doc.insert(ignore_permissions=True)
    return doc.name


def _reversal(register: str | None, receipt: str, status: str = "Reversed", **values) -> str:
    doc = frappe.get_doc({"doctype": REVERSAL, "c2b_register": register, "transaction_id": receipt, "amount": 500,
                          "original_amount": 500, "mpesa_settings": t.SETTINGS, "remarks": "test", "status": status,
                          "docstatus": 1, **values})
    doc.name = "MRV-T-" + frappe.generate_hash(length=8)
    doc.db_insert()  # no validate, nothing sent: only the reversal's state matters here
    return doc.name


def _stored_prompt(reference_doctype: str, reference_name: str, **values) -> str:
    """A submitted STK prompt written straight to the table (nothing sent, never saved again)."""
    doc = frappe.get_doc({"doctype": EXPRESS, "settings": t.SETTINGS, "phone_number": "254712345678", "amount": 500,
                          "base_amount": 500, "currency": "KES", "reference_doctype": reference_doctype,
                          "reference_name": reference_name, "status": "In Progress", "docstatus": 1,
                          "creation": now_datetime(), "modified": now_datetime(), **values})
    doc.name = "MER-T-" + frappe.generate_hash(length=8)
    doc.db_insert()
    return doc.name


def _prompt(reference_doctype: str, reference_name: str, **values):
    """A submitted STK prompt made by insert (its own defaults filled), without sending anything."""
    doc = frappe.get_doc({"doctype": EXPRESS, "settings": t.SETTINGS, "phone_number": "254712345678",
                          "currency": "KES", "base_amount": 500, "status": "In Progress",
                          "reference_doctype": reference_doctype, "reference_name": reference_name,
                          "checkout_request_id": "ws_CO_" + frappe.generate_hash(length=12)}).insert(
        ignore_permissions=True)
    frappe.db.set_value(EXPRESS, doc.name, {"docstatus": 1, **values}, update_modified=False)
    return frappe.get_doc(EXPRESS, doc.name)


class TestFix3(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # CI cannot resolve the example webhook host; the SSRF guard has its own unit tests.
        cls._public = patch("frappe_mpsa_payments.gateway.netguard.is_public_url", return_value=True)
        cls._public.start()
        t.bootstrap()
        t.enable_money_in()
        cls.customer = t.customer("_Test Fix3 Customer")
        name = "_Test Fix3 Client"
        if not frappe.db.exists("Mpesa API Client", name):
            frappe.get_doc({"doctype": "Mpesa API Client", "client_name": name, "mpesa_settings": t.SETTINGS,
                            "webhook_url": "https://hooks.example.com/mpesa", "max_amount": 100000,
                            "can_verify_receipts": 1}).insert()
        cls.client = frappe.get_doc("Mpesa API Client", name)

    @classmethod
    def tearDownClass(cls):
        cls._public.stop()
        super().tearDownClass()

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        self.tag = frappe.generate_hash(length=8)

    def tearDown(self):
        frappe.local.form_dict = frappe._dict()
        frappe.flags[BOOKING_RECEIPT_FLAG] = None

    def _intent(self, status: str = "Prompted", **values):
        intent = intents.create(self.client, amount=500, order_ref="F3-" + frappe.generate_hash(length=6),
                                phone="0712345678")[0]
        frappe.db.set_value(INTENT, intent.name, {"status": status, "customer": self.customer, **values})
        return frappe.get_doc(INTENT, intent.name)

    # ------------------------------------------------------------ P1: a register on an invoice
    def _pos_url(self):
        if mpesa_quick_pay.get_mpesa_shortcode_for_mop_and_company(MOP, t.COMPANY) != t.SHORTCODE:
            url = frappe.get_doc({"doctype": "Mpesa C2B Payment Register URL", "mpesa_settings": t.SETTINGS,
                                  "business_shortcode": t.SHORTCODE, "till_number": t.SHORTCODE,
                                  "register_status": "Success", "company": t.COMPANY, "mode_of_payment": MOP})
            url.name = "_Test Fix3 URL " + frappe.generate_hash(length=6)
            url.db_insert()  # no validate: it registers URLs with Safaricom

    def _put_on_invoice(self, register: str) -> str:
        """What a cashier does in POS quick pay: the draft is submitted onto a draft invoice."""
        self._pos_url()
        si = frappe.get_doc({"doctype": "Sales Invoice", "company": t.COMPANY, "customer": self.customer,
                             "posting_date": nowdate(), "due_date": nowdate(), "currency": "KES",
                             "selling_price_list": t.PRICE_LIST,
                             "items": [{"item_code": t.ITEM, "qty": 1, "rate": 500}]}).insert()
        frappe.local.form_dict = frappe._dict(doctype="Sales Invoice", invoice_name=si.name, customer=self.customer,
                                              mpesa_payments=register)
        mpesa_quick_pay.process_mpesa()
        self.assertEqual(frappe.db.get_value(REGISTER, register, ["docstatus", "sales_invoice", "payment_entry"]),
                         (1, si.name, None))
        return si.name

    def test_prompt_never_books_a_receipt_a_cashier_put_on_an_invoice(self):
        reg = t.receipt(500, billref="pos-" + self.tag)
        code = frappe.db.get_value(REGISTER, reg, "transid")
        si = self._put_on_invoice(reg)
        # the prompt's callback lands afterwards, for the same receipt, on another invoice
        other = t.sales_invoice(self.customer, 500)
        req = _prompt("Sales Invoice", other.name, status="Completed", transaction_id=code,
                      transaction_date=now_datetime())

        with self.assertRaisesRegex(frappe.ValidationError, f"Sales Invoice {si}"):
            frappe.get_doc(EXPRESS, req.name).validate_duplicate_c2b_records()
        with patch.object(frappe.db, "commit"):
            self.assertFalse(stk_verify.book(req.name))
        with self.assertRaisesRegex(frappe.ValidationError, f"Sales Invoice {si}"):
            frappe.get_doc(EXPRESS, req.name).book_completed_payment()  # a manager's Reconcile click

        self.assertEqual(frappe.db.get_value(REGISTER, reg, ["docstatus", "sales_invoice"]), (1, si))
        self.assertEqual(frappe.db.get_value(EXPRESS, req.name, "is_reconciled"), 0)
        self.assertEqual(_live_entries(code), [], "the invoice's payment row holds the money")
        self.assertEqual(frappe.db.get_value("Sales Invoice", other.name, "outstanding_amount"), 500)

    # ------------------------------------------------------------ P2: Paid payouts nothing books
    def _disbursement(self, suffix: str, docstatus: int = 1, **values) -> str:
        name = f"_T-B2CPD-{self.tag}-{suffix}"
        frappe.get_doc({"doctype": PARENT, "name": name, "docstatus": docstatus, "status": "Initiated",
                        "payment_type": "Mpesa Disbursement", "party_type": "Supplier",
                        "transaction_to_pay_against": "Purchase Invoice", "posting_date": nowdate(),
                        **values}).db_insert()
        return name

    def _row(self, parent: str, i: int, reference: str, amount: float, request: str | None = None,
             doctype: str = "Purchase Invoice", party: str = "_T Supplier", **request_values) -> str:
        """A disbursement row; with ``request``, its B2C request too (as the payout jobs leave it)."""
        row = f"{parent}-R{i}"
        frappe.get_doc({"doctype": REF, "name": row, "parent": parent, "parenttype": PARENT,
                        "parentfield": "references", "idx": i, "docstatus": request_values.pop("row_docstatus", 1),
                        "reference_doctype": doctype, "reference_name": reference, "party_type": "Supplier",
                        "party": party, "partyb": "254712345678", "allocated_amount": amount,
                        "payment_status": request_values.get("status", "Paid") if request else None,
                        "b2c_disbursement_request": request,
                        "reference_no": request_values.get("transaction_id")}).db_insert()
        if request and not frappe.db.exists(REQ, request):
            frappe.get_doc({"doctype": REQ, "name": request, "docstatus": 1, "status": "Paid", "amount": amount,
                            "reference_doctype": doctype, "reference_name": reference, "party_type": "Supplier",
                            "party": party, "phone_number": "254712345678", "b2c_payment_disbursement": parent,
                            "b2c_payment_disbursement_reference": row, "accounting_booked": 0,
                            "originator_conversation_id": f"OCID-{request}", **request_values}).db_insert()
        return row

    def _paid_request(self, i: int) -> str:
        return f"_T-B2C-{self.tag}-{i}"

    def _payment_entry(self, receipt: str, reference: str, docstatus: int, amount: float) -> str:
        pe = f"_T-PE-{self.tag}-{frappe.generate_hash(length=6)}"
        frappe.get_doc({"doctype": "Payment Entry", "name": pe, "docstatus": docstatus, "payment_type": "Pay",
                        "posting_date": nowdate(), "reference_no": receipt, "reference_date": nowdate()}).db_insert()
        frappe.get_doc({"doctype": "Payment Entry Reference", "name": f"{pe}-1", "parent": pe,
                        "parenttype": "Payment Entry", "parentfield": "references", "idx": 1, "docstatus": docstatus,
                        "reference_doctype": "Purchase Invoice", "reference_name": reference,
                        "allocated_amount": amount}).db_insert()
        return pe

    def _invoice(self, outstanding: float) -> str:
        name = f"_T-PINV-{self.tag}-{frappe.generate_hash(length=6)}"
        frappe.get_doc({"doctype": "Purchase Invoice", "name": name, "docstatus": 1,
                        "outstanding_amount": outstanding, "grand_total": outstanding,
                        "posting_date": nowdate()}).db_insert()
        return name

    def _check(self, disbursement: str):
        frappe.get_doc(PARENT, disbursement).validate_no_double_payout()

    def test_submit_refused_by_a_paid_payout_whose_entry_was_cancelled(self):
        pinv = self._invoice(50000)  # the entry that paid it was cancelled: it owes the amount again
        old = self._disbursement("OLD")
        receipt = "SKT" + self.tag.upper()[:7]
        self._row(old, 1, pinv, 50000, request=self._paid_request(1), transaction_id=receipt, accounting_booked=1)
        self._payment_entry(receipt, pinv, docstatus=2, amount=50000)
        new = self._disbursement("NEW", docstatus=0)
        self._row(new, 1, pinv, 50000)

        with self.assertRaisesRegex(frappe.ValidationError, f"awaiting accounting: {old}"):
            self._check(new)

        self._payment_entry(receipt, pinv, docstatus=1, amount=50000)  # booked again: it is in what PI owes
        self._check(new)

    def test_submit_refused_by_a_cancel_all_paid_payout(self):
        pinv = self._invoice(50000)
        old = self._disbursement("OLD", docstatus=2)  # cancelled with its requests (Cancel All)
        self._row(old, 1, pinv, 50000, request=self._paid_request(1), transaction_id="SKT" + self.tag.upper()[:7],
                  docstatus=2, row_docstatus=2)
        new = self._disbursement("NEW", docstatus=0)
        self._row(new, 1, pinv, 50000)
        with self.assertRaisesRegex(frappe.ValidationError, f"awaiting accounting: {old}"):
            self._check(new)

    def test_amended_disbursement_counts_its_own_paid_payout_against_what_is_owed(self):
        pinv = self._invoice(100000)  # the payout below was never booked
        original = self._disbursement("1", docstatus=2)  # Cancel All after row 1 was paid
        paid = self._paid_request(1)
        self._row(original, 1, pinv, 50000, request=paid, transaction_id="SKT" + self.tag.upper()[:7],
                  docstatus=2, row_docstatus=2)
        amended = self._disbursement("1-1", docstatus=0, amended_from=original)
        self._row(amended, 1, pinv, 50000, request=paid, row_docstatus=0)  # copied: not sent again
        new_row = self._row(amended, 2, pinv, 60000, row_docstatus=0)

        with self.assertRaisesRegex(frappe.ValidationError, "owes 100000.0; this disbursement allocates 110000.0"):
            self._check(amended)

        frappe.db.set_value(REF, new_row, "allocated_amount", 50000)
        self._check(amended)  # not refused by its own payout

    def test_salary_slip_is_left_to_its_own_owed(self):
        # a Salary Slip's owed already takes off every Paid payout, however cancelled (no HRMS
        # here: what it owes is given)
        slip = f"_T-SS-{self.tag}"
        old = self._disbursement("OLD", docstatus=2)
        self._row(old, 1, slip, 50000, request=self._paid_request(1), doctype="Salary Slip", party="_T EMP",
                  transaction_id="SKT" + self.tag.upper()[:7], docstatus=2, row_docstatus=2)
        new = self._disbursement("NEW", docstatus=0)
        self._row(new, 1, slip, 50000, doctype="Salary Slip", party="_T EMP")
        with patch.object(disbursement_module, "current_outstanding", return_value=50000):
            self._check(new)

    def test_copied_row_link_does_not_hide_another_disbursements_live_payout(self):
        # Desk Duplicate no longer copies a row's request link and result (no_copy; amend still does)
        meta = frappe.get_meta(REF)
        for field in ("b2c_disbursement_request", "payment_status", "reference_no", "reference_date"):
            self.assertEqual(meta.get_field(field).no_copy, 1, field)

        pinv = self._invoice(5000)
        d1 = self._disbursement("D1")
        in_flight = self._paid_request(1)
        self._row(d1, 1, pinv, 5000, request=in_flight, status="Initiated")
        d2 = self._disbursement("D2", docstatus=0)  # a copy made before no_copy (or through the API)
        self._row(d2, 1, pinv, 5000, request=in_flight, status="Initiated", row_docstatus=0)
        self._row(d2, 2, pinv, 5000, row_docstatus=0)
        with self.assertRaisesRegex(frappe.ValidationError, f"awaiting accounting: {d1}"):
            self._check(d2)

    def test_receipt_already_on_an_unallocated_entry_is_not_booked_again(self):
        """Astral's old flow made the payout's Payment Entry as an unallocated advance: nothing more
        is booked (Payment Entry would refuse a second entry with the receipt anyway), staff are
        told once, and the invoice cannot be paid again until that entry is allocated to it."""
        from frappe_mpsa_payments.services import b2c_postprocess_service as post

        pinv = self._invoice(32000)
        old = self._disbursement("OLD")
        receipt = "SKU" + self.tag.upper()[:7]
        row = self._row(old, 1, pinv, 32000, request=self._paid_request(1), transaction_id=receipt)
        pe = f"_T-PE-{self.tag}-ADV"
        frappe.get_doc({"doctype": "Payment Entry", "name": pe, "docstatus": 1, "payment_type": "Pay",
                        "posting_date": nowdate(), "reference_no": receipt, "reference_date": nowdate()}).db_insert()

        self.assertFalse(post.handle_successful_payment(old, row))
        self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": receipt}), 1)
        booked, note = frappe.db.get_value(REQ, self._paid_request(1), ["accounting_booked", "accounting_error"])
        self.assertEqual(booked, 0)  # looked at again each pass, quietly
        self.assertIn(f"already on Payment Entry {pe}", note)
        self.assertIn(f"not allocated: allocate it to Purchase Invoice {pinv}", note)
        self.assertFalse(post.handle_successful_payment(old, row))  # the next pass books nothing either
        self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": receipt}), 1)

        new = self._disbursement("NEW", docstatus=0)
        self._row(new, 1, pinv, 32000)
        with self.assertRaisesRegex(frappe.ValidationError, f"awaiting accounting: {old}"):
            self._check(new)

    def test_order_advance_allocated_to_an_invoice_still_counts_as_booked(self):
        """ERPNext moves an order's advance onto the invoice made from it (Get Advances): the
        Payment Entry then names the order only as the row's advance voucher."""
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
        from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_invoice
        from frappe.utils import add_days

        from frappe_mpsa_payments.services.b2c_postprocess_service import booked_docstatus

        if not frappe.db.has_column("Payment Entry Reference", "advance_voucher_no"):
            self.skipTest("this ERPNext keeps the order row on the Payment Entry")
        group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
        supplier = frappe.get_doc({"doctype": "Supplier", "supplier_name": f"_T Fix3 Supplier {self.tag}",
                                   "supplier_group": group}).insert().name
        frappe.db.set_value("Item", t.ITEM, "is_purchase_item", 1)
        po = frappe.get_doc({
            "doctype": "Purchase Order", "supplier": supplier, "company": t.COMPANY, "currency": "KES",
            "conversion_rate": 1, "transaction_date": nowdate(), "schedule_date": add_days(nowdate(), 3),
            "items": [{"item_code": t.ITEM, "qty": 10, "rate": 50000, "schedule_date": add_days(nowdate(), 3),
                       "uom": "Nos", "conversion_factor": 1}],
        }).insert()
        po.submit()

        receipt = "SKA" + self.tag.upper()[:7]
        d1 = self._disbursement("D1", transaction_to_pay_against="Purchase Order")
        row = self._row(d1, 1, po.name, 200000, request=self._paid_request(1), doctype="Purchase Order",
                        party=supplier, transaction_id=receipt, accounting_booked=1)
        pe = get_payment_entry("Purchase Order", po.name, party_amount=200000)
        pe.paid_from = pe.paid_from or frappe.db.get_value(
            "Account", {"company": t.COMPANY, "account_type": "Cash", "is_group": 0}, "name")
        pe.paid_amount = pe.received_amount = 200000
        pe.reference_no, pe.reference_date = receipt, nowdate()
        for ref in pe.references:
            ref.allocated_amount = 200000
        pe.insert()
        pe.submit()

        pi = make_purchase_invoice(po.name)
        pi.items[0].qty = 4
        pi.bill_no = f"F3-{self.tag}"
        pi.set_advances()
        for adv in pi.advances:
            adv.allocated_amount = min(adv.advance_amount, 200000)
        pi.insert()
        pi.submit()
        self.assertEqual([r.reference_doctype for r in frappe.get_doc("Payment Entry", pe.name).references],
                         ["Purchase Invoice"])  # the order row is gone

        self.assertEqual(booked_docstatus(d1, frappe.get_doc(REF, row)), 1)
        d2 = self._disbursement("D2", docstatus=0, transaction_to_pay_against="Purchase Order")
        self._row(d2, 1, po.name, 100000, doctype="Purchase Order", party=supplier, row_docstatus=0)
        self._check(d2)  # the next advance on the 300,000 the order still owes

    # ------------------------------------------------------------ P3: reversal of an STK-booked receipt
    def _reversal_result(self, register: str) -> tuple[str, str | None]:
        code = frappe.db.get_value(REGISTER, register, "transid")
        conversation = f"AG_{self.tag}"
        mrv = _reversal(register, code, status="Initiated", conversation_id=conversation,
                        originator_conversation_id=f"OCID-{self.tag}")
        reversal_module.handle_result(
            {"ResultType": 0, "ResultCode": 0, "ResultDesc": "Reversed", "ConversationID": conversation,
             "OriginatorConversationID": f"OCID-{self.tag}", "TransactionID": "SGT" + self.tag.upper()[:7],
             "ResultParameters": {"ResultParameter": [{"Key": "Charge", "Value": 0}]}},
            mrv,
        )
        self.assertEqual(frappe.db.get_value(REVERSAL, mrv, "status"), "Reversed")
        notes = frappe.get_all("Comment", filters={"reference_doctype": REGISTER, "reference_name": register,
                                                   "comment_type": "Comment"}, pluck="content")
        self.assertEqual(len(notes), 1, notes)
        return notes[0], frappe.db.get_value(REGISTER, register, "match_method")

    def test_reversal_of_a_receipt_a_reconciled_prompt_booked_asks_to_undo_it(self):
        reg = t.receipt(500, billref="rev-" + self.tag)
        _stored_prompt("Sales Invoice", "SINV-F3", status="Completed", is_reconciled=1,
                       transaction_id=frappe.db.get_value(REGISTER, reg, "transid"))
        note, method = self._reversal_result(reg)
        self.assertIn("must be cancelled or reversed by hand", note)
        self.assertNotEqual(method, "Reversed")

    def test_reversal_of_a_receipt_a_payment_entry_carries_asks_to_undo_it(self):
        reg = t.receipt(500, billref="rev-" + self.tag)
        create_payment_entry(t.COMPANY, self.customer, 500, "KES", MOP, "Customer", nowdate(),
                             frappe.db.get_value(REGISTER, reg, "transid"), nowdate(), None, 1)
        note, method = self._reversal_result(reg)
        self.assertIn("must be cancelled or reversed by hand", note)
        self.assertNotEqual(method, "Reversed")

    def test_reversal_of_an_unbooked_draft_says_it_was_not_booked(self):
        reg = t.receipt(500, billref="rev-" + self.tag)
        _stored_prompt("Sales Invoice", "SINV-F3", status="Completed", is_reconciled=0,
                       transaction_id=frappe.db.get_value(REGISTER, reg, "transid"))
        note, method = self._reversal_result(reg)
        self.assertIn("It was not booked and will not be booked now.", note)
        self.assertEqual(method, "Reversed")

    # ------------------------------------------------------------ P7: the booking names its receipt
    def test_stk_booking_names_its_receipt_while_the_intent_books_it(self):
        intent = self._intent(status="Prompted")
        code = _code()
        req = _prompt(INTENT, intent.name, status="Completed", transaction_id=code, transaction_date=now_datetime())
        seen = []

        def on_payment_authorized(doc, status=None):
            seen.append((doc.name, frappe.flags.get(BOOKING_RECEIPT_FLAG)))

        with patch(INTENT_CLASS, on_payment_authorized):
            mpesa_utils.handle_successful_transaction(frappe.get_doc(EXPRESS, req.name),
                                                      frappe.get_doc("Mpesa Settings", t.SETTINGS))
        self.assertEqual(seen, [(intent.name, code)])
        self.assertIsNone(frappe.flags.get(BOOKING_RECEIPT_FLAG))
        self.assertEqual(frappe.db.get_value(EXPRESS, req.name, "is_reconciled"), 1)

        def broken(doc, status=None):
            raise frappe.ValidationError("accounting period closed")

        other = _prompt(INTENT, intent.name, status="Completed", transaction_id=_code(),
                        transaction_date=now_datetime())
        with patch(INTENT_CLASS, broken), self.assertRaises(frappe.ValidationError):
            mpesa_utils.handle_successful_transaction(frappe.get_doc(EXPRESS, other.name),
                                                      frappe.get_doc("Mpesa Settings", t.SETTINGS))
        self.assertIsNone(frappe.flags.get(BOOKING_RECEIPT_FLAG), "cleared when the booking fails")

    # ------------------------------------------------------------ P8: prepaid orders
    def test_order_booker_skips_a_receipt_reversed_on_another_register(self):
        so = t.sales_order(self.customer, 800, submit=True)
        on_order = {"customer": self.customer, "matched_reference_doctype": "Sales Order",
                    "matched_reference_name": so.name}
        first = t.receipt(800, billref="order " + self.tag)
        transid = frappe.db.get_value(REGISTER, first, "transid")
        frappe.db.set_value(REGISTER, first, on_order)
        _reversal(first, transid)  # refunded, then the register was cancelled and amended
        frappe.db.set_value(REGISTER, first, "docstatus", 2)
        amended = _register(800, "order " + self.tag, transid=transid, amended_from=first)
        frappe.db.set_value(REGISTER, amended, on_order)
        clean = t.receipt(800, billref="order " + self.tag)
        frappe.db.set_value(REGISTER, clean, on_order)

        # the reversal names the cancelled register only: matched by the register alone it is picked up
        by_register = frappe.db.sql(
            """SELECT reg.name FROM `tabMpesa C2B Payment Register` reg
            WHERE reg.name = %s AND NOT EXISTS (SELECT 1 FROM `tabMpesa Reversal Request` rv
              WHERE rv.c2b_register = reg.name AND rv.docstatus = 1 AND rv.status IN %s)""",
            (amended, REVERSAL_HOLDS), pluck=True)
        self.assertEqual(by_register, [amended])

        with patch.object(booking, "book_register") as book:
            matching.book_for_submitted_orders(limit=10000)
        picked = [c.args[0] for c in book.call_args_list]
        self.assertNotIn(amended, picked)
        self.assertIn(clean, picked)

    # ------------------------------------------------------------ P9: submitted on insert
    def test_register_inserted_submitted_finds_its_receipts_reversal(self):
        first = t.receipt(500, billref="rev-" + self.tag)
        transid = frappe.db.get_value(REGISTER, first, "transid")
        _reversal(first, transid, status="Unknown")
        frappe.db.set_value(REGISTER, first, "docstatus", 2)
        doc = frappe.get_doc({"doctype": REGISTER, **t.confirmation_payload(500, billref="rev-" + self.tag,
                                                                            transid=transid),
                              "amended_from": first, "customer": self.customer, "submit_payment": 0,
                              "docstatus": 1})
        with self.assertRaisesRegex(frappe.ValidationError, "cannot be booked"):
            doc.insert(ignore_permissions=True)
        self.assertEqual(_live_entries(transid), [])

    # ------------------------------------------------------------ P11: consumed means on an invoice
    def test_stk_receipt_whose_register_was_submitted_without_entry_or_invoice_pays_the_intent(self):
        intent = self._intent(status="Prompted")
        code = _code()
        reg = t.receipt(500, billref="auto-" + self.tag, transid=code)
        frappe.db.set_value(REGISTER, reg, "docstatus", 1)  # auto-reconcile with Submit Payment off
        _stored_prompt(INTENT, intent.name, status="Completed", transaction_id=code, transaction_date=now_datetime())
        self.assertIsNone(intents._consumed_register(code, for_update=True))
        intents.refresh(intent.name)
        self.assertEqual(frappe.db.get_value(INTENT, intent.name, ["status", "receipt"]), ("Paid", code))

    def test_register_naming_the_intent_submitted_without_entry_or_invoice_pays_it(self):
        intent = self._intent(status="Expired", customer=None)
        reg = t.receipt(500, billref=intent.name)
        frappe.db.set_value(REGISTER, reg, "docstatus", 1)
        intents.refresh(intent.name)
        self.assertEqual(frappe.db.get_value(INTENT, intent.name, ["status", "receipt"]),
                         ("Paid", frappe.db.get_value(REGISTER, reg, "transid")))

    def test_register_on_an_invoice_never_pays_the_intent(self):
        prompted = self._intent(status="Prompted")
        code = _code()
        reg = t.receipt(500, billref="pos-" + self.tag, transid=code)
        frappe.db.set_value(REGISTER, reg, {"docstatus": 1, "sales_invoice": f"SINV-F3-{self.tag}"})
        _stored_prompt(INTENT, prompted.name, status="Completed", transaction_id=code, transaction_date=now_datetime())
        self.assertEqual(intents._consumed_register(code).name, reg)

        link = self._intent(status="Expired", customer=None)
        link_reg = t.receipt(500, billref=link.name)
        frappe.db.set_value(REGISTER, link_reg, {"docstatus": 1, "pos_invoice": f"POS-F3-{self.tag}"})

        intents.refresh(prompted.name)
        intents.refresh(link.name)
        self.assertEqual(frappe.db.get_value(INTENT, prompted.name, ["status", "receipt"]), ("Prompted", None))
        self.assertEqual(frappe.db.get_value(INTENT, link.name, ["status", "receipt"]), ("Expired", None))
        self.assertTrue(frappe.db.exists("Mpesa Match Log", {"reference_name": link.name, "result": "Error"}))
