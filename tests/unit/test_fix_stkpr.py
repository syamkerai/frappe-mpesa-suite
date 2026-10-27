"""STK prompts on Payment Requests: the amount asked, and the Payment Entry booked when paid.

C01: a Payment Request above the transaction limit is prompted in parts; every part used to
be overwritten with the request's grand total (two prompts for the whole amount).
C02: a paid prompt used to book the request's whole outstanding, not what was paid, and a
part payment was refused after its C2B booking had already been cancelled.
Repair: a request booked only in full is never prompted for a part; the cents STK cannot ask
are written off; a receipt from C2B of another amount is never re-booked at the prompt's.
"""

import importlib
import sys
import types

import pytest

import fake_frappe

PR = "ACC-PRQ-1"
SO = "SO-1"
EXPRESS = "Mpesa Express Request"


class Meta:
    def __init__(self, fields):
        self.fields = fields

    def has_field(self, fieldname):
        return fieldname in self.fields


class Row:
    """A Payment Entry Reference row as ERPNext leaves it after create_payment_entry."""

    def __init__(self, **kw):
        self.payment_request = None
        self.payment_term = None
        self.__dict__.update(kw)

    def precision(self, fieldname):
        return 2


class FakePaymentEntry:
    def __init__(self, frappe, **kw):
        self._frappe = frappe
        self.name = None
        self.docstatus = 0
        self.references = []
        self.deductions = []
        self.company, self.cost_center, self.source_exchange_rate = "_Test Co", None, 1
        self.paid_from_account_currency = self.paid_to_account_currency = "KES"
        self.__dict__.update(kw)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)

    def append(self, key, row):
        getattr(self, key).append(types.SimpleNamespace(**row))

    def insert(self, ignore_permissions=False):
        self.name = f"ACC-PAY-{next(self._frappe._seq)}"
        self._frappe.db.table("Payment Entry")[self.name] = {
            "name": self.name, "docstatus": 0, "reference_no": self.reference_no, "doc": self,
        }
        return self

    def submit(self):
        """ERPNext's checks and its update_payment_requests_as_per_pe_references."""
        total = sum(r.allocated_amount for r in self.references)
        assert total <= self.paid_amount + sum(d.amount for d in self.deductions) + 0.005, "allocated above paid"
        for r in self.references:
            if not r.payment_request:
                continue
            pr = self._frappe.db.table("Payment Request")[r.payment_request]
            left = round(pr["outstanding_amount"] - r.allocated_amount, 2)
            if left < 0:
                raise self._frappe.ValidationError("allocated amount is greater than the outstanding amount of Payment Request")
            pr["outstanding_amount"] = left
            pr["status"] = "Paid" if left == 0 else "Partially Paid"
        self.docstatus = 1
        self._frappe.db.table("Payment Entry")[self.name]["docstatus"] = 1


class FakePaymentRequest(fake_frappe.FakeDoc):
    def run_method(self, method, *args):
        pass

    def create_payment_entry(self, submit=True):
        """ERPNext: an entry for the request's outstanding, allocated to its document."""
        frappe = self._frappe
        doc_owed = frappe.doc_owed[self.reference_name]
        party_amount = self.outstanding_amount or doc_owed  # get_payment_entry falls back to the document
        terms = frappe.payment_terms.get(self.reference_name)
        if terms:
            rows = [Row(reference_doctype="Sales Order", reference_name=self.reference_name, payment_term=t,
                        allocated_amount=a, outstanding_amount=doc_owed, payment_request=self.name)
                    for t, a in terms]
        else:
            rows = [Row(reference_doctype="Sales Order", reference_name=self.reference_name,
                        allocated_amount=party_amount, outstanding_amount=doc_owed, payment_request=self.name)]
        pe = FakePaymentEntry(frappe, paid_amount=party_amount, received_amount=party_amount, references=rows,
                              reference_no=self.name)
        frappe.created_entries.append(pe)
        if submit:
            pe.insert()
            pe.submit()
        return pe


class FakeExpress:
    def __init__(self, **kw):
        self.name = "MEXP-1"
        self.reference_doctype = "Payment Request"
        self.reference_name = PR
        self.owner = "cashier@example.com"
        self.phone_number = "254708374149"
        self.payment_gateway = "Mpesa-TEST"
        self.transaction_date = None
        self.is_reconciled = 0
        self.__dict__.update(kw)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def reload(self):
        pass

    def save(self, ignore_permissions=False):
        pass


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.db.db_type = "sqlite"  # named_lock is a no-op off MariaDB
        frappe.metas = {"Payment Request": Meta({"outstanding_amount", "grand_total", "currency"})}
        frappe.get_meta = lambda doctype: frappe.metas.get(doctype, Meta(set()))
        frappe.doc_owed, frappe.payment_terms, frappe.created_entries, frappe.invoices_made = {}, {}, [], []
        frappe.companies = {"_Test Co": ("Round Off - TC", "Main - TC", "Main - TC")}
        frappe.get_cached_value = lambda doctype, name, fields: frappe.companies.get(name)
        frappe.logger = lambda *a, **kw: types.SimpleNamespace(debug=print, info=print, error=print)
        frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)

        plain_get_doc = frappe.get_doc

        def get_doc(doctype, name=None, **kw):
            if doctype == "Payment Request":
                return FakePaymentRequest(frappe, doctype, frappe.db.table(doctype)[name])
            return plain_get_doc(doctype, name, **kw)

        frappe.get_doc = get_doc

        exceptions = types.ModuleType("frappe.exceptions")
        exceptions.DoesNotExistError = frappe.DoesNotExistError
        monkeypatch.setitem(sys.modules, "frappe.exceptions", exceptions)
        api = types.ModuleType("mpsa_suite.mpsa_suite.api.m_pesa_api")
        api.check_transaction_status = api.initiate_stk_push = lambda **kw: None
        sys.modules[api.__name__] = api  # dropped again by fake_frappe.uninstall

        def make_sales_invoice(source_name, ignore_permissions=False):
            frappe.invoices_made.append(source_name)
            return types.SimpleNamespace(insert=lambda **kw: types.SimpleNamespace(submit=lambda: None))

        so_module = types.ModuleType("erpnext.selling.doctype.sales_order.sales_order")
        so_module.make_sales_invoice = make_sales_invoice
        for name in ("erpnext", "erpnext.selling", "erpnext.selling.doctype", "erpnext.selling.doctype.sales_order"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, so_module.__name__, so_module)

        utils = importlib.import_module("mpsa_suite.utils.utils")
        mer = importlib.import_module(
            "mpsa_suite.mpsa_suite.doctype.mpesa_express_request.mpesa_express_request"
        )
        yield fake_frappe._dict(frappe=frappe, utils=utils, mer=mer)
    finally:
        fake_frappe.uninstall(saved)


def payment_request(env, grand_total=200000.0, outstanding=None, doc_owed=None, **kw):
    row = {"name": PR, "docstatus": 1, "status": "Requested", "currency": "KES", "party_account_currency": "KES",
           "grand_total": grand_total, "outstanding_amount": grand_total if outstanding is None else outstanding,
           "reference_doctype": "Sales Order", "reference_name": SO}
    row.update(kw)
    env.frappe.db.table("Payment Request")[PR] = row
    env.frappe.doc_owed[SO] = row["outstanding_amount"] if doc_owed is None else doc_owed
    return row


def entries(env):
    return [r["doc"] for r in env.frappe.db.table("Payment Entry").values() if r["docstatus"] == 1]


def pay(env, amount, receipt, auto_invoice=0):
    req = FakeExpress(amount=amount, transaction_id=receipt, name=f"MEXP-{receipt}")
    env.utils.handle_successful_transaction(req, types.SimpleNamespace(auto_create_sales_invoice=auto_invoice))
    return req


# ================================================================ C01: the amount a prompt asks
def express_doc(env, base_amount=None, **kw):
    doc = env.mer.MpesaExpressRequest()
    doc.__dict__.update(
        reference_doctype="Payment Request", reference_name=PR, base_amount=base_amount, amount=None,
        currency="KES", request_id="MPESAEXP1", settings="TEST", payment_gateway="Mpesa-TEST",
        phone_number="254708374149",
    )
    doc.__dict__.update(kw)
    return doc


def test_split_part_keeps_its_own_amount(env):
    payment_request(env, 200000)
    for part in (150000, 50000):  # prompts for parts of a request (request_for_payment never splits one)
        doc = express_doc(env, part)
        doc.validate_payment_request_amount()
        doc.set_missing_values()
        assert (doc.base_amount, doc.amount) == (part, part)


def test_amount_defaults_to_and_never_exceeds_what_the_request_owes(env):
    payment_request(env, 100000, outstanding=60000)  # 40,000 already paid against it
    doc = express_doc(env)
    doc.validate_payment_request_amount()
    assert doc.base_amount == 60000
    doc = express_doc(env, 100000)
    doc.validate_payment_request_amount()
    assert doc.base_amount == 60000


def test_paid_request_is_not_prompted_again(env):
    payment_request(env, 100000, outstanding=0, status="Paid")
    with pytest.raises(env.frappe.ValidationError, match="already paid"):
        express_doc(env, 100000).validate_payment_request_amount()


def test_draft_request_and_old_erpnext_fall_back_to_grand_total(env):
    payment_request(env, 80000, outstanding=0, docstatus=0)
    doc = express_doc(env)
    doc.validate_payment_request_amount()
    assert doc.base_amount == 80000

    payment_request(env, 80000, outstanding=0)
    env.frappe.metas["Payment Request"] = Meta({"grand_total", "currency"})  # no outstanding tracked
    doc = express_doc(env)
    doc.validate_payment_request_amount()
    assert doc.base_amount == 80000


def test_outstanding_in_another_currency_is_not_compared(env):
    # ERPNext keeps a USD request's outstanding in the KES party account currency
    payment_request(env, 100, outstanding=13000, currency="USD", party_account_currency="KES")
    doc = express_doc(env, 60, currency="USD")
    doc.validate_payment_request_amount()
    assert doc.base_amount == 60
    doc = express_doc(env, 150, currency="USD")
    doc.validate_payment_request_amount()
    assert doc.base_amount == 100


@pytest.mark.parametrize("pr_currency,account_currency,tracks_outstanding", [
    ("USD", "USD", True),  # USD customer account: ERPNext books the whole outstanding
    ("KES", "USD", True),  # a KES request on a USD receivable account: the same booking
    ("KES", "KES", False),  # ERPNext before partial Payment Requests: no outstanding to book against
])
def test_part_of_a_request_booked_only_in_full_is_refused_before_any_prompt(
    env, pr_currency, account_currency, tracks_outstanding
):
    # e.g. KES 300,000 split by a 250,000 limit: the 250,000 part, once paid, would be refused
    # as short and its receipt booked nowhere; nor is it raised to the whole amount once per part
    payment_request(env, 300000, currency=pr_currency, party_account_currency=account_currency)
    if not tracks_outstanding:
        env.frappe.metas["Payment Request"] = Meta({"grand_total", "currency"})
    with pytest.raises(env.frappe.ValidationError, match="only be booked when paid in full"):
        express_doc(env, 250000, currency=pr_currency).validate_payment_request_amount()
    for asked in (None, 300000, 400000):  # the whole amount (or nothing) is still prompted, capped
        doc = express_doc(env, asked, currency=pr_currency)
        doc.validate_payment_request_amount()
        assert doc.base_amount == 300000


def test_prompt_above_the_stk_maximum_is_refused_at_submit(env):
    doc = express_doc(env, settings=None, amount=300000)
    with pytest.raises(env.frappe.ValidationError, match="at most 250,000"):
        doc.before_submit()
    express_doc(env, settings=None, amount=250000).before_submit()


# ================================================================ C02: what a paid prompt books
def test_split_parts_each_book_what_they_collected(env):
    payment_request(env, 200000)
    pay(env, 150000, "UGG7TBGIGV", auto_invoice=1)
    [first] = entries(env)
    assert (first.paid_amount, first.received_amount, first.reference_no) == (150000, 150000, "UGG7TBGIGV")
    assert [(r.reference_name, r.allocated_amount, r.payment_request) for r in first.references] == [(SO, 150000, PR)]
    pr = env.frappe.db.table("Payment Request")[PR]
    assert (pr["outstanding_amount"], pr["status"]) == (50000, "Partially Paid")  # not forced to Paid
    assert env.frappe.invoices_made == [], "the order is billed once the request is paid, not after one part"

    pay(env, 50000, "UGG7TBGIGW", auto_invoice=1)
    second = entries(env)[1]
    assert (second.paid_amount, second.reference_no) == (50000, "UGG7TBGIGW")
    assert (pr["outstanding_amount"], pr["status"]) == (0, "Paid")
    assert env.frappe.invoices_made == [SO]


def test_prompt_above_the_outstanding_books_everything_received(env):
    # 40,000 was paid by bank against the request; the re-prompt still asked 100,000
    payment_request(env, 100000, outstanding=60000)
    pay(env, 100000, "UHH1AAAAAA")
    [pe] = entries(env)
    assert pe.paid_amount == 100000, "the whole receipt is in the ledger"
    assert [(r.allocated_amount, r.payment_request) for r in pe.references] == [(60000, PR)]  # rest unallocated
    assert env.frappe.db.table("Payment Request")[PR]["status"] == "Paid"


def test_document_owing_less_than_the_request_gets_no_more_than_it_owes(env):
    payment_request(env, 100000, doc_owed=30000)  # the order was mostly paid without the request
    pay(env, 100000, "UHH1AAAAAB")
    [pe] = entries(env)
    assert [r.allocated_amount for r in pe.references] == [30000]
    assert pe.paid_amount == 100000


def test_settled_request_still_pays_its_document_without_the_request_link(env):
    payment_request(env, 100000, outstanding=0, status="Paid", doc_owed=30000)
    pay(env, 50000, "UHH1AAAAAC")
    [pe] = entries(env)
    assert [(r.allocated_amount, r.payment_request) for r in pe.references] == [(30000, None)]
    assert pe.paid_amount == 50000
    assert env.frappe.db.table("Payment Request")[PR]["outstanding_amount"] == 0


def test_payment_term_rows_share_what_the_document_owes(env):
    payment_request(env, 100000)
    env.frappe.payment_terms[SO] = [("Deposit", 60000), ("Balance", 40000)]
    pay(env, 70000, "UHH1AAAAAD")
    [pe] = entries(env)
    assert [(r.payment_term, r.allocated_amount) for r in pe.references] == [("Deposit", 60000), ("Balance", 10000)]


def test_partial_payment_drops_an_early_payment_discount(env):
    payment_request(env, 100000)
    original = FakePaymentRequest.create_payment_entry

    def with_discount(self, submit=True):
        pe = original(self, submit=False)
        pe.paid_amount = pe.received_amount = 98000
        pe.deductions = [types.SimpleNamespace(amount=2000)]
        return pe

    FakePaymentRequest.create_payment_entry = with_discount
    try:
        pay(env, 50000, "UHH1AAAAAE")
    finally:
        FakePaymentRequest.create_payment_entry = original
    [pe] = entries(env)
    assert (pe.deductions, [r.allocated_amount for r in pe.references]) == ([], [50000])


def test_request_in_another_currency_on_a_kes_account_books_what_was_paid(env):
    payment_request(env, 1000, outstanding=129000, currency="USD", party_account_currency="KES")
    pay(env, 130000, "UHH1AAAAAK")  # the prompt converted USD 1,000 at today's rate
    [pe] = entries(env)
    assert pe.paid_amount == 130000
    assert [(r.allocated_amount, r.payment_request) for r in pe.references] == [(129000, PR)]


def test_request_on_an_account_in_another_currency_keeps_erpnext_booking(env):
    payment_request(env, 1000, currency="USD", party_account_currency="USD")
    pay(env, 130000, "UHH1AAAAAL")
    [pe] = entries(env)
    assert (pe.reference_no, pe.paid_amount) == (PR, 1000)  # ERPNext's own entry, as before
    assert env.frappe.db.table("Payment Request")[PR]["status"] == "Paid"


def test_receipt_already_on_a_payment_entry_is_not_booked_twice(env):
    payment_request(env, 100000)
    env.frappe.db.table("Payment Entry")["ACC-PAY-OLD"] = {"name": "ACC-PAY-OLD", "docstatus": 1,
                                                          "reference_no": "UHH1AAAAAF", "doc": None}
    pay(env, 100000, "UHH1AAAAAF")
    assert env.frappe.created_entries == []


def test_account_in_another_currency_fails_loudly(env):
    payment_request(env, 100000)
    original = FakePaymentRequest.create_payment_entry

    def usd_bank(self, submit=True):
        pe = original(self, submit=False)
        pe.paid_to_account_currency = "USD"
        return pe

    FakePaymentRequest.create_payment_entry = usd_bank
    try:
        with pytest.raises(env.frappe.ValidationError):
            pay(env, 100000, "UHH1AAAAAG")
    finally:
        FakePaymentRequest.create_payment_entry = original
    assert entries(env) == []


def test_short_payment_on_old_erpnext_raises_so_the_c2b_cancellation_rolls_back(env):
    payment_request(env, 200000)
    env.frappe.metas["Payment Request"] = Meta({"grand_total", "currency"})  # no partial requests
    with pytest.raises(env.frappe.ValidationError, match="short of Payment Request"):
        pay(env, 150000, "UHH1AAAAAH")
    assert env.frappe.created_entries == []


def test_full_payment_on_old_erpnext_still_books_the_request(env):
    payment_request(env, 200000)
    env.frappe.metas["Payment Request"] = Meta({"grand_total", "currency"})
    pay(env, 200000, "UHH1AAAAAI")
    [pe] = entries(env)
    assert pe.reference_no == PR  # ERPNext's own entry, unchanged on old versions
    assert env.frappe.db.table("Payment Request")[PR]["status"] == "Paid"


def test_manual_reconcile_cancels_the_c2b_register_before_booking(env, monkeypatch):
    # Its Payment Entry carries the receipt and would otherwise read as "already booked"
    # just before the register (and that entry) are cancelled for the prompt.
    calls = []
    env.frappe.db.table("Mpesa Settings")["TEST"] = {"name": "TEST"}
    registers = env.frappe.db.table("Mpesa C2B Payment Register")
    registers["MPC2B-1"] = {"name": "MPC2B-1", "transid": "UHH1AAAAAJ", "docstatus": 1}
    doc = express_doc(env, 100000, status="Completed", transaction_id="UHH1AAAAAJ", is_reconciled=0)
    doc.reload = lambda: None
    monkeypatch.setattr(type(doc), "validate_duplicate_c2b_records", lambda self: calls.append("c2b"), raising=False)
    monkeypatch.setattr(env.mer, "handle_successful_transaction", lambda req, settings: calls.append("book"))
    doc.book_completed_payment()
    assert calls == ["c2b", "book"]

    calls.clear()
    registers["MPC2B-1"]["docstatus"] = 2  # stk_verify.book cancelled it already
    doc.book_completed_payment()
    assert calls == ["book"]


def test_manual_reconcile_refuses_a_c2b_receipt_of_another_amount(env, monkeypatch):
    # sync_statuses gave the prompt (KES 5,000) the receipt of a 6,000 paybill payment: replacing
    # the register's booking with a 5,000 one would leave 1,000 of the receipt in no ledger
    calls = []
    env.frappe.db.table("Mpesa Settings")["TEST"] = {"name": "TEST"}
    registers = env.frappe.db.table("Mpesa C2B Payment Register")
    registers["MPC2B-2"] = {"name": "MPC2B-2", "transid": "UHH1AAAAAM", "docstatus": 1, "transamount": 6000}
    doc = express_doc(env, 5000, amount=5000, status="Completed", transaction_id="UHH1AAAAAM", is_reconciled=0)
    doc.reload = lambda: None
    monkeypatch.setattr(type(doc), "validate_duplicate_c2b_records", lambda self: calls.append("c2b"), raising=False)
    monkeypatch.setattr(env.mer, "handle_successful_transaction", lambda req, settings: calls.append("book"))
    with pytest.raises(env.frappe.ValidationError, match="reconcile it by hand"):
        doc.book_completed_payment()
    assert calls == [], "the register (and its 6,000 entry) is left as it is, nothing booked"

    registers["MPC2B-2"]["docstatus"] = 2  # cancelled since: still the receipt's amount
    with pytest.raises(env.frappe.ValidationError, match="reconcile it by hand"):
        doc.book_completed_payment()
    assert calls == []

    registers["MPC2B-2"].update(docstatus=1, transamount=5000)  # same receipt amount: the prompt books it
    doc.book_completed_payment()
    assert calls == ["c2b", "book"]


def test_cents_the_prompt_could_not_ask_are_written_off_and_the_order_billed_once(env):
    # rounded totals off: the request is 1,000.40, STK asked (and the customer paid) 1,000
    payment_request(env, 1000.40)
    pay(env, 1000, "UHH1AAAAAN", auto_invoice=1)
    [pe] = entries(env)
    assert pe.paid_amount == 1000, "only what M-Pesa received"
    assert [(r.allocated_amount, r.payment_request) for r in pe.references] == [(1000.40, PR)]
    assert [(d.account, d.cost_center, d.amount) for d in pe.deductions] == [
        ("Round Off - TC", "Main - TC", pytest.approx(0.40))
    ]
    pr = env.frappe.db.table("Payment Request")[PR]
    assert (pr["outstanding_amount"], pr["status"]) == (0, "Paid")
    assert env.frappe.invoices_made == [SO]


def test_cents_of_a_split_part_are_written_off_and_more_than_cents_never(env):
    payment_request(env, 200000.40)
    pay(env, 150000, "UHH1AAAAAO", auto_invoice=1)
    [first] = entries(env)
    assert first.deductions == [], "a part payment is no rounding"
    assert env.frappe.db.table("Payment Request")[PR]["outstanding_amount"] == pytest.approx(50000.40)
    assert env.frappe.invoices_made == []

    pay(env, 50000, "UHH1AAAAAP", auto_invoice=1)  # the last part, asked as 50,000
    second = entries(env)[1]
    assert [d.amount for d in second.deductions] == [pytest.approx(0.40)]
    assert env.frappe.db.table("Payment Request")[PR]["status"] == "Paid"
    assert env.frappe.invoices_made == [SO]


def test_cents_are_not_written_off_beyond_what_the_document_owes(env):
    payment_request(env, 1000.40, doc_owed=1000)  # the order owes less than the request
    pay(env, 1000, "UHH1AAAAAQ")
    [pe] = entries(env)
    assert ([r.allocated_amount for r in pe.references], pe.deductions) == ([1000], [])


def test_without_a_round_off_account_the_cents_stay_open_and_the_order_is_still_billed(env):
    env.frappe.companies = {}
    payment_request(env, 1000.40)
    pay(env, 1000, "UHH1AAAAAR", auto_invoice=1)
    [pe] = entries(env)
    assert ([r.allocated_amount for r in pe.references], pe.deductions) == ([1000], [])
    assert env.frappe.db.table("Payment Request")[PR]["outstanding_amount"] == pytest.approx(0.40)
    assert env.frappe.invoices_made == [SO]

    pay(env, 1, "UHH1AAAAAS", auto_invoice=1)  # a late receipt: the order is not billed again
    assert env.frappe.invoices_made == [SO]


# ================================================================ final review: a prompt paid, not booked yet
def prompt_row(env, name, **kw):
    row = dict(name=name, reference_doctype="Payment Request", reference_name=PR, docstatus=1, status="Completed",
               transaction_id=f"T{name[-1]}RECEIPT", amount=5000.0, base_amount=5000.0, currency="KES",
               is_reconciled=0)
    row.update(kw)
    env.frappe.db.table(EXPRESS)[name] = row
    return row


def test_a_paid_prompt_not_booked_yet_is_not_asked_for_again(env):
    # MEXP-1 paid KES 5,000 of PR-1 (KES 5,000); its booking failed or has not run: PR-1 still owes 5,000
    payment_request(env, 5000)
    prompt_row(env, "MEXP-1")
    with pytest.raises(env.frappe.ValidationError, match="paid by M-Pesa prompt MEXP-1 .receipt T1RECEIPT., which is "
                                                         "being booked"):
        express_doc(env, 5000, name="MEXP-2").validate_payment_request_amount()


def test_a_paid_prompt_not_booked_yet_counts_against_what_is_left(env, monkeypatch):
    # a request booked in parts: 5,000 of 8,000 paid by MEXP-1 (not booked yet), so at most 3,000 is asked
    payment_request(env, 8000)
    env.frappe.metas["Payment Request"] = Meta({"outstanding_amount", "grand_total", "currency"})
    prompt_row(env, "MEXP-1")
    doc = express_doc(env, 8000, name="MEXP-2")
    monkeypatch.setattr(env.mer, "stk_books_part_payments", lambda pr: True)
    doc.validate_payment_request_amount()
    assert doc.base_amount == 3000


@pytest.mark.parametrize("values", [
    {"is_reconciled": 1},  # booked
    {"status": "Failed", "transaction_id": None},  # never paid
    {"docstatus": 2},
])
def test_only_a_paid_unbooked_prompt_holds_the_request(env, values):
    payment_request(env, 5000)
    prompt_row(env, "MEXP-1", **values)
    doc = express_doc(env, 5000, name="MEXP-2")
    doc.validate_payment_request_amount()
    assert doc.base_amount == 5000


def test_a_receipt_booked_another_way_holds_nothing(env):
    payment_request(env, 5000)
    prompt_row(env, "MEXP-1")
    env.frappe.db.table("Payment Entry")["ACC-PAY-9"] = {"name": "ACC-PAY-9", "reference_no": "T1RECEIPT",
                                                        "docstatus": 1}
    pr = importlib.import_module("mpsa_suite.money_in.payment_request")
    assert pr.unbooked_prompts(PR) == []
    assert [p.name for p in pr.unbooked_prompts(PR, exclude="MEXP-0")] == []


def test_resending_a_prompt_while_another_paid_one_is_being_booked_is_refused(env, monkeypatch):
    env.frappe.utils.now = lambda: "2026-10-27 12:00:00"
    stkpush = importlib.import_module("mpsa_suite.www.mpesa.stkpush")
    payment_request(env, 5000)
    prompt_row(env, "MEXP-1")
    failed = types.SimpleNamespace(name="MEXP-2", status="Failed", reference_doctype="Payment Request",
                                   reference_name=PR, base_amount=5000.0, amount=5000.0,
                                   check_permission=lambda ptype: None)
    failed.get = lambda key, default=None: getattr(failed, key, default)
    with pytest.raises(env.frappe.ValidationError, match="being booked"):
        stkpush._check_can_push(failed)
    env.frappe.db.table(EXPRESS)["MEXP-1"]["is_reconciled"] = 1  # booked: PR-1 owes what is left in ERPNext
    stkpush._check_can_push(failed)
