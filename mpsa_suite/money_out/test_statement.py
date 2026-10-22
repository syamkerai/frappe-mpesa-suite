"""Payout statement lines against the real database (ERPNext v15 and v16).

A shortcode of its own (600999, with its own Bank Account and M-Pesa Charges Account), so
the wallet balances one test writes never meet another module's payouts. Each test is
rolled back. Unit coverage of the charge rules is in tests/unit/test_payout_statement.py.
"""

from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe.utils import add_days, flt, getdate, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import bank_transaction
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import b2b, statement

SETTINGS = "TESTPAYOUT"
SHORTCODE = "600999"
B2C, B2B, BT, JE = statement.B2C, statement.B2B, "Bank Transaction", "Journal Entry"
CHARGES = "_Test M-Pesa Charges"
BANDS = "100 = 0\n1500 = 5\n5000 = 9\n20000 = 11\n250000 = 13"
SUITE_VALUES = dict(book_payout_charges=1, reconcile_payout_lines=1, b2c_charge_bands=BANDS, payout_lines_from=None)


def _payout_settings() -> tuple[str, str]:
    """Mpesa Settings TESTPAYOUT (B2C + B2B) mapped to its own Bank Account; (bank account, GL)."""
    if not frappe.db.exists("Mpesa Settings", SETTINGS):
        frappe.get_doc(
            {
                "doctype": "Mpesa Settings",
                "payment_gateway_name": SETTINGS,
                "use_c2b": 0,
                "use_stk": 0,
                "use_b2c": 1,
                "use_b2b": 1,
                "paybill_type": "Pay Bill",
                "online_passkey": "passkey",
                "sandbox": 1,
                "company": t.COMPANY,
                "consumer_key": "key",
                "consumer_secret": "secret",
                "business_shortcode": SHORTCODE,
                "transaction_limit": 250000,
            }
        ).insert()
    gl = frappe.db.get_value("Mode of Payment Account", {"parent": f"Mpesa-{SETTINGS}", "company": t.COMPANY},
                             "default_account")
    bank_account = frappe.db.get_value("Bank Account", {"account": gl, "is_company_account": 1}, "name")
    if not bank_account:
        bank_account = frappe.get_doc(
            {"doctype": "Bank Account", "account_name": f"Mpesa B2C {SHORTCODE}", "bank": "Mpesa", "account": gl,
             "company": t.COMPANY, "is_company_account": 1, "bank_account_no": SHORTCODE}
        ).insert().name
    charges = frappe.db.get_value("Account", {"account_name": CHARGES, "company": t.COMPANY}, "name")
    if not charges:
        parent = frappe.db.get_value("Account", {"company": t.COMPANY, "root_type": "Expense", "is_group": 1},
                                     "name", order_by="lft desc")
        charges = frappe.get_doc({"doctype": "Account", "account_name": CHARGES, "company": t.COMPANY,
                                  "parent_account": parent, "root_type": "Expense",
                                  "report_type": "Profit and Loss", "is_group": 0}).insert().name
    frappe.db.set_value("Mpesa Settings", SETTINGS, {"bank_account": bank_account, "charges_account": charges,
                                                     "submit_b2c_accounting_entries": 1})
    return bank_account, gl


def _supplier() -> str:
    name = frappe.db.get_value("Supplier", {"supplier_name": "_T Payout Supplier"}, "name")
    if name:
        return name
    group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    return frappe.get_doc({"doctype": "Supplier", "supplier_name": "_T Payout Supplier", "supplier_group": group}).insert().name


class TestPayoutStatement(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        cls.bank_account, cls.gl = _payout_settings()
        cls.supplier = _supplier()
        cls.charges = frappe.db.get_value("Account", {"account_name": CHARGES, "company": t.COMPANY}, "name")
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None

    def tearDown(self):
        frappe.db.rollback()
        frappe.local.mpesa_suite_bank_accounts = None

    # ------------------------------------------------------------ helpers
    @contextmanager
    def suite(self, **values):
        t.enable_money_in(**{**SUITE_VALUES, **values})
        try:
            yield
        finally:
            t.enable_money_in(**SUITE_VALUES)

    def _receipt(self) -> str:
        return "T" + frappe.generate_hash(length=9).upper()

    def _b2c(self, amount, utility=None, completed=None, **values) -> frappe._dict:
        completed = completed or now_datetime()
        row = frappe._dict(
            doctype=B2C, name=f"_T-B2C-{frappe.generate_hash(length=8)}", docstatus=1, status="Paid",
            amount=amount, transaction_id=self._receipt(), mpesa_settings=SETTINGS, result_code="0",
            transaction_completed_datetime=completed, posting_date=getdate(completed), currency="KES",
            utility_acct_avlbl_funds=utility or 0, charges_paid_acct_avlbl_funds=0, commandid="BusinessPayment",
            phone_number="254712345678", receiver_public_name="254712345678 - JANE DOE",
            creation=completed - timedelta(seconds=3),
        )
        row.update(values)
        frappe.get_doc(row).db_insert()
        return row

    def _b2b(self, amount=10, charges="Business Pay Bill Charge|KES|2.00") -> frappe._dict:
        row = frappe._dict(
            doctype=B2B, name=f"_T-MB2B-{frappe.generate_hash(length=8)}", docstatus=1, status="Paid",
            mpesa_settings=SETTINGS, command_id="BusinessPayBill", receiver_shortcode="888880",
            account_reference="INV-1", amount=amount, supplier=self.supplier, company=t.COMPANY,
            transaction_id=self._receipt(), trans_completed_time=now_datetime(), result_code="0",
            debit_party_charges=charges, receiver_public_name="888880 - TEST BILLER",
        )
        frappe.get_doc(row).db_insert()
        return row

    def _payment_entry(self, receipt, amount, submit=True) -> str:
        pe = frappe.new_doc("Payment Entry")
        pe.update({"payment_type": "Pay", "company": t.COMPANY, "party_type": "Supplier", "party": self.supplier,
                   "paid_from": self.gl, "paid_amount": amount, "received_amount": amount,
                   "source_exchange_rate": 1, "target_exchange_rate": 1, "reference_no": receipt,
                   "reference_date": nowdate(), "posting_date": nowdate()})
        pe.setup_party_account_field()
        pe.set_missing_values()
        pe.insert()
        if submit:
            pe.submit()
        return pe.name

    def _journal(self, cheque_no, amount) -> str:
        je = frappe.get_doc({
            "doctype": JE, "voucher_type": "Bank Entry", "company": t.COMPANY, "posting_date": nowdate(),
            "cheque_no": cheque_no, "cheque_date": nowdate(),
            "accounts": [
                {"account": self.charges, "debit_in_account_currency": amount,
                 "cost_center": frappe.get_cached_value("Company", t.COMPANY, "cost_center")},
                {"account": self.gl, "credit_in_account_currency": amount},
            ],
        })
        je.insert()
        je.submit()
        return je.name

    def _line(self, key, withdrawal, transaction_id=True, bank_account=None) -> str:
        """A line made the way Astral's feed made them (charge lines carry no transaction_id)."""
        bt = frappe.get_doc({"doctype": BT, "date": nowdate(), "bank_account": bank_account or self.bank_account,
                             "company": t.COMPANY, "currency": "KES", "deposit": 0, "withdrawal": withdrawal,
                             "reference_number": key, "transaction_id": key if transaction_id else None,
                             "description": "AIL feed"})
        bt.insert()
        bt.submit()
        return bt.name

    def _lines(self, key) -> list:
        return frappe.get_all(BT, filters={"reference_number": key, "docstatus": ["<", 2]},
                              fields=["name", "withdrawal", "deposit", "status", "bank_account", "unallocated_amount"],
                              order_by="creation asc")

    def _matched(self, line) -> list:
        return [(r.payment_document, r.payment_entry) for r in frappe.get_doc(BT, line).payment_entries]

    def _charge_entries(self, receipt) -> list:
        return frappe.get_all(JE, filters={"cheque_no": f"CHG-{receipt}", "docstatus": ["<", 2]},
                              fields=["name", "docstatus", "total_debit"], order_by="creation asc")

    def _logs(self, reference_name, method) -> list:
        return frappe.get_all("Mpesa Match Log", filters={"reference_name": reference_name, "method": method},
                              fields=["result", "details"], order_by="creation asc")

    # ------------------------------------------------------------ B2B: the real booking job
    def test_paid_b2b_gets_line_and_charge_both_reconciled(self):
        p = self._b2b()
        pe = b2b.book_payment_entry(p.name)  # the job a Paid result enqueues

        self.assertEqual(frappe.db.get_value("Payment Entry", pe, "docstatus"), 1)
        (line,) = self._lines(p.transaction_id)
        self.assertEqual((flt(line.withdrawal), line.bank_account, line.status), (10, self.bank_account, "Reconciled"))
        self.assertEqual(self._matched(line.name), [("Payment Entry", pe)])
        (charge,) = self._lines(f"CHG-{p.transaction_id}")
        self.assertEqual((flt(charge.withdrawal), charge.status), (2, "Reconciled"))
        (entry,) = self._charge_entries(p.transaction_id)
        self.assertEqual((entry.docstatus, flt(entry.total_debit)), (1, 2))
        self.assertEqual(self._matched(charge.name), [("Journal Entry", entry.name)])
        accounts = {(a.account, flt(a.debit), flt(a.credit)) for a in frappe.get_doc(JE, entry.name).accounts}
        self.assertEqual(accounts, {(self.charges, 2, 0), (self.gl, 0, 2)})
        values = frappe.db.get_value(B2B, p.name, ["bank_transaction", "charge_bank_transaction", "mpesa_charge"],
                                     as_dict=True)
        self.assertEqual((values.bank_transaction, values.charge_bank_transaction, flt(values.mpesa_charge)),
                         (line.name, charge.name, 2))

        # a re-run (re-sent result, the sweep) makes nothing twice
        b2b.book_payment_entry(p.name)
        statement.sweep_missing()
        self.assertEqual(len(self._lines(p.transaction_id)), 1)
        self.assertEqual(len(self._lines(f"CHG-{p.transaction_id}")), 1)
        self.assertEqual(len(self._charge_entries(p.transaction_id)), 1)

    def test_b2b_charged_nothing_gets_only_its_line(self):
        p = self._b2b(charges="")
        b2b.book_payment_entry(p.name)
        self.assertEqual(len(self._lines(p.transaction_id)), 1)
        self.assertEqual(self._lines(f"CHG-{p.transaction_id}"), [])

    # ------------------------------------------------------------ B2C
    def test_paid_b2c_line_reconciles_with_its_payment_entry(self):
        p = self._b2c(1200)
        pe = self._payment_entry(p.transaction_id, 1200)
        statement.after_payout(B2C, p.name)

        (line,) = self._lines(p.transaction_id)
        self.assertEqual(self._matched(line.name), [("Payment Entry", pe)])
        (charge,) = self._lines(f"CHG-{p.transaction_id}")
        self.assertEqual((flt(charge.withdrawal), charge.status), (5, "Reconciled"))  # band: up to 1,500 -> 5
        self.assertEqual(flt(frappe.db.get_value(B2C, p.name, "mpesa_charge")), 5)

    def test_salary_payout_reconciles_with_its_journal_entry(self):
        p = self._b2c(3000, reference_doctype="Salary Slip")
        je = self._journal(p.transaction_id, 3000)  # journal_service: receipt as Cheque No
        statement.after_payout(B2C, p.name)
        (line,) = self._lines(p.transaction_id)
        self.assertEqual(self._matched(line.name), [("Journal Entry", je)])

    def test_entry_booked_later_is_reconciled_by_the_sweep(self):
        p = self._b2c(1200)
        statement.after_payout(B2C, p.name)
        (line,) = self._lines(p.transaction_id)
        self.assertEqual(line.status, "Unreconciled")
        pe = self._payment_entry(p.transaction_id, 1200)
        statement.reconcile_run()
        self.assertEqual(self._matched(line.name), [("Payment Entry", pe)])

    def test_entry_of_another_amount_is_left_for_staff(self):
        p = self._b2c(1200)
        self._payment_entry(p.transaction_id, 1000)
        statement.after_payout(B2C, p.name)
        (line,) = self._lines(p.transaction_id)
        self.assertEqual(line.status, "Unreconciled")
        logs = self._logs(line.name, "Bank Reconciliation")
        self.assertEqual(logs[0].result, "Ambiguous")
        self.assertIn("no entry has that amount", logs[0].details)

    def test_balances_agreeing_with_the_band_book_it(self):
        start = now_datetime() - timedelta(minutes=10)
        self._b2c(1000, utility=10000, completed=start)
        p = self._b2c(1000, utility=8995, completed=start + timedelta(seconds=5))
        statement.after_payout(B2C, p.name)
        (charge,) = self._lines(f"CHG-{p.transaction_id}")
        self.assertEqual(flt(charge.withdrawal), 5)
        self.assertEqual(self._logs(p.name, "Payout Charge"), [])

    def test_balances_disputing_the_band_book_no_charge(self):
        start = now_datetime() - timedelta(minutes=10)
        self._b2c(1000, utility=10000, completed=start)
        p = self._b2c(1000, utility=8987, completed=start + timedelta(seconds=5))  # fell 1,013: 13, not 5
        statement.after_payout(B2C, p.name)
        self.assertEqual(len(self._lines(p.transaction_id)), 1)
        self.assertEqual(self._lines(f"CHG-{p.transaction_id}"), [])
        (log,) = self._logs(p.name, "Payout Charge")
        self.assertEqual(log.result, "Ambiguous")

    def test_without_bands_the_balances_charge_is_booked(self):
        with self.suite(b2c_charge_bands=""):
            start = now_datetime() - timedelta(minutes=10)
            self._b2c(1000, utility=10000, completed=start)
            p = self._b2c(6000, utility=3989, completed=start + timedelta(seconds=5))
            # sent before p completed, its result not in yet
            unsettled = self._b2c(500, completed=start - timedelta(minutes=1), status="Initiated",
                                  transaction_completed_datetime=None)
            statement.after_payout(B2C, p.name)
            self.assertEqual(self._lines(f"CHG-{p.transaction_id}"), [])  # a payout still in flight
            frappe.db.set_value(B2C, unsettled.name, "status", "Failed")
            statement.after_payout(B2C, p.name)
            (charge,) = self._lines(f"CHG-{p.transaction_id}")
            self.assertEqual(flt(charge.withdrawal), 11)

    # ------------------------------------------------------------ Astral's feed
    def test_lines_and_entry_astrals_feed_made_are_used_not_duplicated(self):
        p = self._b2c(1000)
        pe = self._payment_entry(p.transaction_id, 1000)
        line = self._line(p.transaction_id, 1000)
        charge = self._line(f"CHG-{p.transaction_id}", 5, transaction_id=False)
        je = self._journal(f"CHG-{p.transaction_id}", 5)

        statement.after_payout(B2C, p.name)

        self.assertEqual([r.name for r in self._lines(p.transaction_id)], [line])
        self.assertEqual([r.name for r in self._lines(f"CHG-{p.transaction_id}")], [charge])
        self.assertEqual([r.name for r in self._charge_entries(p.transaction_id)], [je])
        self.assertEqual(self._matched(line), [("Payment Entry", pe)])
        self.assertEqual(self._matched(charge), [("Journal Entry", je)])
        self.assertEqual(frappe.db.get_value(B2C, p.name, ["bank_transaction", "charge_bank_transaction"]),
                         (line, charge))

    def test_line_on_another_mpesa_account_is_not_made_twice(self):
        # Astral's feed found a shortcode's account by its number; the suite by Mpesa Settings
        gl = frappe.get_doc({"doctype": "Account", "account_name": "_T Old Feed 600999", "company": t.COMPANY,
                             "parent_account": frappe.db.get_value("Account", self.gl, "parent_account"),
                             "account_type": "Bank", "is_group": 0}).insert().name
        old = frappe.get_doc({"doctype": "Bank Account", "account_name": "_T Old Feed 600999", "bank": "Mpesa",
                              "account": gl, "company": t.COMPANY, "is_company_account": 1}).insert().name
        frappe.local.mpesa_suite_bank_accounts = None
        p = self._b2c(1000)
        self._line(p.transaction_id, 1000, bank_account=old)
        statement.after_payout(B2C, p.name)
        self.assertEqual([r.bank_account for r in self._lines(p.transaction_id)], [old])
        (log,) = self._logs(p.name, "Bank Reconciliation")
        self.assertEqual(log.result, "Ambiguous")

    # ------------------------------------------------------------ switches, sweep, scope
    def test_charges_and_reconciliation_follow_their_switches(self):
        with self.suite(book_payout_charges=0, reconcile_payout_lines=0):
            p = self._b2c(1200)
            self._payment_entry(p.transaction_id, 1200)
            statement.after_payout(B2C, p.name)
            (line,) = self._lines(p.transaction_id)
            self.assertEqual(line.status, "Unreconciled")
            self.assertEqual(self._lines(f"CHG-{p.transaction_id}"), [])

    def test_no_charges_account_leaves_the_charge_line_for_staff(self):
        frappe.db.set_value("Mpesa Settings", SETTINGS, "charges_account", None)
        p = self._b2c(1200)
        statement.after_payout(B2C, p.name)
        (charge,) = self._lines(f"CHG-{p.transaction_id}")
        self.assertEqual(charge.status, "Unreconciled")
        self.assertEqual(self._charge_entries(p.transaction_id), [])
        (log,) = self._logs(p.name, "Payout Charge")
        self.assertEqual(log.result, "Error")

    def test_sweep_fills_missed_payouts_from_payout_lines_from(self):
        today = self._b2c(1200)
        older = self._b2c(1200, completed=now_datetime() - timedelta(days=3))
        with self.suite(payout_lines_from=add_days(nowdate(), -1)):
            statement.sweep_missing()
        self.assertEqual(len(self._lines(today.transaction_id)), 1)
        self.assertEqual(len(self._lines(f"CHG-{today.transaction_id}")), 1)
        self.assertEqual(self._lines(older.transaction_id), [])

    def test_receipt_on_both_sides_of_one_account_is_two_lines(self):
        # a B2B between two shortcodes mapped to one Bank Account
        receipt = self._receipt()
        common = dict(bank_account=self.bank_account, receipt=receipt, amount=10, date=nowdate(),
                      description="test", transaction_type="Pay Bill")
        deposit = bank_transaction.create_statement_line(**common)
        withdrawal = bank_transaction.create_statement_line(**common, withdrawal=True)
        self.assertNotEqual(deposit, withdrawal)
        self.assertEqual(bank_transaction.create_statement_line(**common, withdrawal=True), withdrawal)
