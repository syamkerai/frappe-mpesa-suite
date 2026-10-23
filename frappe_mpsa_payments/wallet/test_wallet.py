"""Sub-accounts, statement import and balance checks against the real database (v15 and v16).

Shortcode 600888 of its own: Utility on its Bank Account, Working (MMF) mapped to a second
one, and a company bank that funds Working. Statements are built in the M-PESA Org Portal's
per-account layout (the parser's own tests use the same layout). Each test is rolled back.
"""

import io
from datetime import timedelta

import frappe
import openpyxl
from frappe.utils import flt, now_datetime

from frappe_mpsa_payments.compat import IntegrationTestCase
from frappe_mpsa_payments.money_in import testing as t
from frappe_mpsa_payments.money_out import b2b, statement
from frappe_mpsa_payments.wallet import accounts, balances, statement_import

SETTINGS, SHORTCODE = "TESTWALLET", "600888"
OWN = f"{SHORTCODE} - TEST WALLET LIMITED"
BT, PE, JE, ROW, IMPORT = "Bank Transaction", "Payment Entry", "Journal Entry", "Mpesa Statement Row", "Mpesa Statement Import"
SNAPSHOT = "Mpesa Balance Snapshot"
BANDS = "100 = 0\n1500 = 5\n5000 = 9\n20000 = 11\n250000 = 13"
SUITE_VALUES = {"book_payout_charges": 1, "reconcile_payout_lines": 1, "b2c_charge_bands": BANDS,
                "payout_lines_from": None, "submit_statement_entries": 1, "bank_match_days": 3,
                "balance_check_minutes": 0}
HEADER = ["Receipt No.", "Completion Time", "Initiation Time", "Details", "Transaction Status", "Paid In",
          "Withdrawn", "Balance", "Balance Confirmed", "Reason Type", "Other Party Info", "Linked Transaction ID",
          "A/C No.", "Currency"]
ACCOUNT_NAMES = {"Utility": "Utility Account", "Working": "MMF Account for Organization"}


def _gl(name: str, parent: str) -> str:
    existing = frappe.db.get_value("Account", {"account_name": name, "company": t.COMPANY}, "name")
    if existing:
        return existing
    return frappe.get_doc({"doctype": "Account", "account_name": name, "company": t.COMPANY, "parent_account": parent,
                          "account_type": "Bank", "is_group": 0}).insert().name


def _bank_account(name: str, bank: str, gl: str) -> str:
    existing = frappe.db.get_value("Bank Account", {"account": gl, "is_company_account": 1}, "name")
    if existing:
        return existing
    if not frappe.db.exists("Bank", bank):
        frappe.get_doc({"doctype": "Bank", "bank_name": bank}).insert()
    return frappe.get_doc({"doctype": "Bank Account", "account_name": name, "bank": bank, "account": gl,
                           "company": t.COMPANY, "is_company_account": 1}).insert().name


def _fixtures() -> frappe._dict:
    if not frappe.db.exists("Mpesa Settings", SETTINGS):
        frappe.get_doc({
            "doctype": "Mpesa Settings", "payment_gateway_name": SETTINGS, "use_c2b": 0, "use_stk": 0, "use_b2c": 1,
            "use_b2b": 1, "paybill_type": "Pay Bill", "online_passkey": "passkey", "sandbox": 1, "company": t.COMPANY,
            "consumer_key": "key", "consumer_secret": "secret", "business_shortcode": SHORTCODE,
            "transaction_limit": 250000,
        }).insert()
    utility_gl = frappe.db.get_value("Mode of Payment Account", {"parent": f"Mpesa-{SETTINGS}", "company": t.COMPANY},
                                     "default_account")
    parent = frappe.db.get_value("Account", utility_gl, "parent_account")
    f = frappe._dict(
        utility_gl=utility_gl,
        utility=_bank_account(f"Mpesa Utility {SHORTCODE}", "Mpesa", utility_gl),
        working_gl=_gl(f"_T Mpesa {SHORTCODE} Working", parent),
        bank_gl=_gl(f"_T Bank {SHORTCODE}", parent),
    )
    f.working = _bank_account(f"Mpesa Working {SHORTCODE}", "Mpesa", f.working_gl)
    f.bank = _bank_account(f"_T Bank {SHORTCODE}", "_T Wallet Bank", f.bank_gl)
    charges = frappe.db.get_value("Account", {"account_name": "_Test M-Pesa Charges", "company": t.COMPANY}, "name")
    if not charges:
        group = frappe.db.get_value("Account", {"company": t.COMPANY, "root_type": "Expense", "is_group": 1}, "name",
                                    order_by="lft desc")
        charges = frappe.get_doc({"doctype": "Account", "account_name": "_Test M-Pesa Charges", "company": t.COMPANY,
                                  "parent_account": group, "root_type": "Expense", "report_type": "Profit and Loss",
                                  "is_group": 0}).insert().name
    f.charges = charges
    doc = frappe.get_doc("Mpesa Settings", SETTINGS)
    doc.update({"bank_account": f.utility, "charges_account": charges, "submit_b2c_accounting_entries": 1})
    doc.set("sub_accounts", [{"sub_account": "Working", "bank_account": f.working}])
    doc.save()
    group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
    f.supplier = frappe.db.get_value("Supplier", {"supplier_name": "_T Wallet Supplier"}, "name") or frappe.get_doc(
        {"doctype": "Supplier", "supplier_name": "_T Wallet Supplier", "supplier_group": group}).insert().name
    return f


class TestWallet(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        cls.f = _fixtures()
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        self.now = now_datetime().replace(microsecond=0) - timedelta(hours=2)

    def tearDown(self):
        frappe.db.rollback()
        frappe.local.mpesa_suite_bank_accounts = None

    # ------------------------------------------------------------ helpers
    def receipt(self) -> str:
        return "W" + frappe.generate_hash(length=9).upper()

    def when(self, minutes: int) -> str:
        return (self.now + timedelta(minutes=minutes)).strftime("%d-%m-%Y %H:%M:%S")

    def row(self, receipt, minutes, details, amount, balance, reason="", party=""):
        paid_in, withdrawn = (amount, "") if amount > 0 else ("", amount)
        at = self.when(minutes)
        return [receipt, at, at, details, "Completed", paid_in, withdrawn, balance, "true", reason, party, "", "", "KES"]

    def import_statement(self, sub, rows, file_name=None) -> frappe._dict:
        """Rows newest first, as the portal lists them."""
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "transaction"
        for r in [["Account Holder:", "TEST WALLET LIMITED"], ["Short Code:", SHORTCODE],
                  ["Account:", ACCOUNT_NAMES[sub]],
                  ["Time Period:", "From", self.when(-60), "To", self.when(600)],
                  ["Operator:", "clerk", "Organization:", "TEST WALLET LIMITED", "Date of Report:", self.when(600)],
                  ["Opening Balance:", "0.0", "Closing Balance:", str(rows[0][7] if rows else 0)],
                  HEADER, *rows]:
            sheet.append(r)
        out = io.BytesIO()
        book.save(out)
        name = file_name or f"ORG_{SHORTCODE}_{ACCOUNT_NAMES[sub].replace(' ', '')}_All_{frappe.generate_hash(length=6)}.xlsx"
        url = frappe.get_doc({"doctype": "File", "file_name": name, "content": out.getvalue(), "is_private": 1}).insert().file_url
        doc = frappe.get_doc({"doctype": IMPORT, "statement_file": url}).insert()
        statement_import.import_now(doc.name)
        return frappe._dict(frappe.db.get_value(IMPORT, doc.name, "*", as_dict=True))

    def lines(self, key, bank_account=None):
        filters = {"reference_number": key, "docstatus": 1}
        if bank_account:
            filters["bank_account"] = bank_account
        return frappe.get_all(BT, filters=filters, fields=["name", "bank_account", "deposit", "withdrawal", "status",
                                                           "mpesa_sub_account", "mpesa_completed_at"],
                              order_by="creation asc")

    def rows(self, receipt):
        return frappe.get_all(ROW, filters={"receipt": receipt}, fields=["kind", "result", "note", "sub_account"],
                              order_by="creation asc")

    def transfer(self, receipt):
        return frappe.get_all(PE, filters={"reference_no": receipt, "payment_type": "Internal Transfer",
                                           "docstatus": ["<", 2]},
                              fields=["name", "paid_from", "paid_to", "paid_amount", "docstatus"])

    def bank_line(self, amount, withdrawal=True, description="RTGS SAFARICOM MPESA") -> str:
        bt = frappe.get_doc({"doctype": BT, "date": self.now.date(), "bank_account": self.f.bank, "company": t.COMPANY,
                             "currency": "KES", "deposit": 0 if withdrawal else amount,
                             "withdrawal": amount if withdrawal else 0, "reference_number": self.receipt(),
                             "description": description})
        bt.insert()
        bt.submit()
        return bt.name

    # ------------------------------------------------------------ mapping
    def test_mapping_rules(self):
        doc = frappe.get_doc("Mpesa Settings", SETTINGS)
        doc.append("sub_accounts", {"sub_account": "Working", "bank_account": self.f.working})
        self.assertRaises(frappe.ValidationError, doc.save)  # mapped twice
        doc = frappe.get_doc("Mpesa Settings", SETTINGS)
        doc.append("sub_accounts", {"sub_account": "Utility", "bank_account": self.f.working})
        self.assertRaises(frappe.ValidationError, doc.save)  # receives payments: stays on the shortcode's account

    def test_without_a_mapping_everything_stays_on_the_shortcodes_account(self):
        # a site upgraded from Navari's app: one Bank Account per shortcode, nothing mapped
        main = frappe.db.get_value("Mpesa Settings", t.SETTINGS, "bank_account")
        for sub in accounts.SUB_ACCOUNTS:
            self.assertEqual(accounts.bank_account(t.SETTINGS, sub), main)
        self.assertIsNone(accounts.gl(t.SETTINGS, "Utility"))
        self.assertEqual(accounts.bank_account(SETTINGS, "Working"), self.f.working)
        self.assertEqual(accounts.bank_account(SETTINGS, "Utility"), self.f.utility)
        self.assertEqual(accounts.bank_account(SETTINGS, "Merchant"), self.f.utility)

    # ------------------------------------------------------------ live payouts by sub-account
    def test_b2b_pays_from_working_and_posts_there(self):
        receipt = self.receipt()
        name = f"_T-MB2B-{frappe.generate_hash(length=8)}"
        frappe.get_doc({
            "doctype": "Mpesa B2B Payment Request", "name": name, "docstatus": 1, "status": "Paid",
            "mpesa_settings": SETTINGS, "command_id": "BusinessPayBill", "receiver_shortcode": "888880",
            "account_reference": "INV-1", "amount": 10, "supplier": self.f.supplier, "company": t.COMPANY,
            "transaction_id": receipt, "trans_completed_time": self.now, "result_code": "0",
            "debit_party_charges": "Business Pay Bill Charge|KES|2.00",
            "debit_account_balance": "Working Account|KES|1476.00|1476.00|0.00|0.00",
        }).db_insert()
        pe = b2b.book_payment_entry(name)
        self.assertEqual(frappe.db.get_value(PE, pe, "paid_from"), self.f.working_gl)
        (line,) = self.lines(receipt)
        self.assertEqual((line.bank_account, line.mpesa_sub_account, line.status), (self.f.working, "Working", "Reconciled"))
        self.assertTrue(line.mpesa_completed_at)
        (charge,) = self.lines(f"CHG-{receipt}")
        self.assertEqual((charge.bank_account, charge.status), (self.f.working, "Reconciled"))

    def test_b2c_posts_to_utility(self):
        receipt = self.receipt()
        name = f"_T-B2C-{frappe.generate_hash(length=8)}"
        frappe.get_doc({"doctype": "B2C Disbursement Request", "name": name, "docstatus": 1, "status": "Paid",
                        "amount": 1200, "transaction_id": receipt, "mpesa_settings": SETTINGS, "result_code": "0",
                        "transaction_completed_datetime": self.now, "posting_date": self.now.date()}).db_insert()
        statement.after_payout("B2C Disbursement Request", name)
        (line,) = self.lines(receipt)
        self.assertEqual((line.bank_account, line.mpesa_sub_account), (self.f.utility, "Utility"))

    # ------------------------------------------------------------ statement import
    def test_working_statement_books_funding_transfers_and_finds_payouts(self):
        funding, transfer = self.receipt(), self.receipt()
        bank = self.bank_line(100000)  # the bank's own line for the funding
        result = self.import_statement("Working", [
            self.row(transfer, 30, "Business Transfer from MMF Account to Utility Account via WEB by TEST\\clerk",
                     -90000, 10000, "Business Transfer From MMF Account To Utility Account via WE", OWN),
            self.row(funding, 10, "Deposit into MMF Control Account from M-PESA", 100000, 100000,
                     "Organization Deposit of Funds", "SP"),
        ])
        self.assertEqual(result.status, "Imported", result.error)
        self.assertEqual((result.rows_read, result.reconciled, result.entries_made), (2, 2, 2))

        (deposit,) = self.lines(funding)
        self.assertEqual((deposit.bank_account, flt(deposit.deposit), deposit.status), (self.f.working, 100000, "Reconciled"))
        (entry,) = self.transfer(funding)
        self.assertEqual((entry.paid_from, entry.paid_to, entry.docstatus), (self.f.bank_gl, self.f.working_gl, 1))
        self.assertEqual(frappe.db.get_value(BT, bank, "status"), "Reconciled")

        sides = {r.bank_account: r for r in self.lines(transfer)}
        self.assertEqual(flt(sides[self.f.working].withdrawal), 90000)
        self.assertEqual(flt(sides[self.f.utility].deposit), 90000)
        self.assertEqual({r.status for r in sides.values()}, {"Reconciled"})
        (entry,) = self.transfer(transfer)
        self.assertEqual((entry.paid_from, entry.paid_to), (self.f.working_gl, self.f.utility_gl))

        # the newest row's balance is where balance checks count from
        self.assertEqual(flt(frappe.db.get_value(SNAPSHOT, {"shortcode": SHORTCODE, "sub_account": "Working",
                                                            "source": "Statement"}, "balance",
                                                 order_by="taken_at desc")), 10000)

        # the other side's statement, and the same file again: nothing twice
        again = self.import_statement("Utility", [
            self.row(transfer, 30, "Business Transfer from MMF Account to Utility Account via WEB by TEST\\clerk",
                     90000, 90000, "Business Transfer From MMF Account To Utility Account via WE", OWN),
        ])
        self.assertEqual((again.lines_made, again.entries_made, again.reconciled), (0, 0, 1))
        self.assertEqual(len(self.lines(transfer)), 2)
        self.assertEqual(len(self.transfer(transfer)), 1)

    def test_utility_statement_payouts_and_exact_charges(self):
        fed, portal = self.receipt(), self.receipt()
        name = f"_T-B2C-{frappe.generate_hash(length=8)}"
        frappe.get_doc({"doctype": "B2C Disbursement Request", "name": name, "docstatus": 1, "status": "Paid",
                        "amount": 6000, "transaction_id": fed, "mpesa_settings": SETTINGS, "result_code": "0",
                        "transaction_completed_datetime": self.now + timedelta(minutes=5),
                        "posting_date": self.now.date()}).db_insert()
        result = self.import_statement("Utility", [
            self.row(portal, 20, "Salary Payment to 25470****222 - JOHN **** ROE", -500, 3489,
                     "Salary Payment via API", "25470****222 - JOHN **** ROE"),
            self.row(fed, 5, "B2C Payment Charge", -11, 3989, "Salary Payment via API", "SP"),
            self.row(fed, 5, "Salary Payment to 25470****111 - JANE **** DOE", -6000, 4000,
                     "Salary Payment via API", "25470****111 - JANE **** DOE"),
        ])
        self.assertEqual(result.status, "Imported", result.error)
        (charge,) = self.lines(f"CHG-{fed}")
        self.assertEqual(flt(charge.withdrawal), 11)  # what Safaricom took, not a guess
        (je,) = frappe.get_all(JE, filters={"cheque_no": f"CHG-{fed}", "docstatus": 1}, pluck="name")
        self.assertEqual(charge.status, "Reconciled")
        values = frappe.db.get_value("B2C Disbursement Request", name,
                                     ["bank_transaction", "charge_bank_transaction", "mpesa_charge"], as_dict=True)
        self.assertEqual((values.charge_bank_transaction, flt(values.mpesa_charge)), (charge.name, 11))
        self.assertEqual(values.bank_transaction, self.lines(fed)[0].name)
        # a payout made on the portal: its line, left for staff to book
        (line,) = self.lines(portal)
        self.assertEqual(line.status, "Unreconciled")
        self.assertEqual([r.result for r in self.rows(portal)], ["Line Made"])
        self.assertTrue(je)

    def test_transfer_within_one_bank_account_moves_nothing(self):
        receipt = self.receipt()
        self.import_statement("Utility", [
            self.row(receipt, 5, "Utility Account to Organization Settlement Account", -5000, 1000,
                     "Utility Account to Organization Settlement Account", OWN),
        ])
        self.assertEqual(self.lines(receipt), [])
        self.assertEqual([r.result for r in self.rows(receipt)], ["Skipped"])

    def test_funding_without_its_bank_line_waits_for_it(self):
        funding = self.receipt()
        rows = [self.row(funding, 10, "Deposit into MMF Control Account from M-PESA", 25000, 25000,
                         "Organization Deposit of Funds", "SP")]
        self.import_statement("Working", rows)
        (line,) = self.lines(funding)
        self.assertEqual(line.status, "Unreconciled")
        (row,) = self.rows(funding)
        self.assertIn("no unmatched bank line", row.note)
        self.bank_line(25000)
        self.import_statement("Working", rows)  # once the bank's statement is in
        self.assertEqual(self.lines(funding)[0].status, "Reconciled")
        self.assertEqual([r.result for r in self.rows(funding)], ["Reconciled"])

    def test_two_possible_bank_lines_are_left_for_staff(self):
        funding = self.receipt()
        self.bank_line(7000)
        self.bank_line(7000)
        self.import_statement("Working", [self.row(funding, 10, "Deposit into MMF Control Account from M-PESA", 7000,
                                                   7000, "Organization Deposit of Funds", "SP")])
        self.assertEqual(self.transfer(funding), [])
        self.assertIn("2 unmatched bank line", self.rows(funding)[0].note)

    def staff_funding(self, amount, into_gl=None) -> str:
        """How Astral books funding today: one Journal Entry, Dr the M-Pesa GL / Cr the bank."""
        je = frappe.get_doc({
            "doctype": JE, "voucher_type": "Journal Entry", "company": t.COMPANY, "posting_date": self.now.date(),
            "user_remark": "Transfer to B2C",
            "accounts": [{"account": into_gl or self.f.working_gl, "debit_in_account_currency": amount},
                         {"account": self.f.bank_gl, "credit_in_account_currency": amount}],
        })
        je.insert()
        je.submit()
        return je.name

    def test_funding_matches_the_entry_staff_booked(self):
        funding = self.receipt()
        je = self.staff_funding(60000)
        self.import_statement("Working", [self.row(funding, 10, "Deposit into MMF Control Account from M-PESA", 60000,
                                                   60000, "Organization Deposit of Funds", "SP")])
        (line,) = self.lines(funding)
        self.assertEqual(line.status, "Reconciled")
        self.assertEqual(frappe.get_doc(BT, line.name).payment_entries[0].payment_entry, je)
        self.assertEqual(self.transfer(funding), [])  # nothing booked twice
        self.assertEqual([r.result for r in self.rows(funding)], ["Reconciled"])

    def test_entry_already_matched_to_another_line_is_named(self):
        funding = self.receipt()
        je = self.staff_funding(61000)
        # an older hand-made top-up line already took that entry
        old = frappe.get_doc({"doctype": BT, "date": self.now.date(), "bank_account": self.f.working,
                              "company": t.COMPANY, "currency": "KES", "deposit": 61000, "withdrawal": 0,
                              "reference_number": self.receipt(), "description": "B2C float top-up"}).insert()
        old.submit()
        from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import reconcile_vouchers

        reconcile_vouchers(old.name, frappe.as_json([{"payment_doctype": JE, "payment_name": je, "amount": 61000}]))
        self.import_statement("Working", [self.row(funding, 10, "Deposit into MMF Control Account from M-PESA", 61000,
                                                   61000, "Organization Deposit of Funds", "SP")])
        self.assertEqual(self.lines(funding)[0].status, "Unreconciled")
        (row,) = self.rows(funding)
        self.assertIn(old.name, row.note)
        self.assertIn("cancel one of the two lines", row.note)

    def test_entry_in_the_shortcodes_gl_is_named_when_working_is_separate(self):
        funding = self.receipt()
        je = self.staff_funding(62000, into_gl=self.f.utility_gl)  # booked the old way, into the main GL
        self.import_statement("Working", [self.row(funding, 10, "Deposit into MMF Control Account from M-PESA", 62000,
                                                   62000, "Organization Deposit of Funds", "SP")])
        (row,) = self.rows(funding)
        self.assertEqual(row.result, "Line Made")
        self.assertIn(je, row.note)
        self.assertIn(f"amend it to {self.f.working_gl}", row.note)

    def test_old_charge_line_on_an_uncharged_payout_is_flagged(self):
        payout = self.receipt()
        old = frappe.get_doc({"doctype": BT, "date": self.now.date(), "bank_account": self.f.utility,
                              "company": t.COMPANY, "currency": "KES", "deposit": 0, "withdrawal": 48.02,
                              "reference_number": f"CHG-{payout}", "description": "derived charge"}).insert()
        old.submit()
        self.import_statement("Utility", [self.row(payout, 5, "Salary Payment to 07****999 - TEST", -100, 900,
                                                   "Salary Payment via API", "07****999 - TEST")])
        (row,) = self.rows(payout)
        self.assertEqual(row.result, "Left for Staff")
        self.assertIn("charged nothing", row.note)
        self.assertIn(old.name, row.note)

    def test_bank_line_naming_mpesa_wins_a_tie(self):
        funding = self.receipt()
        self.bank_line(8000, description="SUPPLIER PAYMENT 0042")
        named = self.bank_line(8000, description="RTGS TO SAFARICOM LTD")
        self.import_statement("Working", [self.row(funding, 10, "Deposit into MMF Control Account from M-PESA", 8000,
                                                   8000, "Organization Deposit of Funds", "SP")])
        self.assertEqual(frappe.db.get_value(BT, named, "status"), "Reconciled")
        self.assertEqual(self.lines(funding)[0].status, "Reconciled")

    def test_transfer_skipped_while_shared_is_booked_once_mapped(self):
        transfer = self.receipt()
        rows = [self.row(transfer, 30, "Business Transfer from MMF Account to Utility Account via WEB by TEST\\clerk",
                         -4000, 0, "Business Transfer From MMF Account To Utility Account via WE", OWN)]
        doc = frappe.get_doc("Mpesa Settings", SETTINGS)
        doc.set("sub_accounts", [])
        doc.save()  # as on a site that has not mapped Working yet
        frappe.local.mpesa_suite_bank_accounts = None
        self.import_statement("Working", rows)
        self.assertEqual(self.lines(transfer), [])
        self.assertEqual([r.result for r in self.rows(transfer)], ["Skipped"])

        doc = frappe.get_doc("Mpesa Settings", SETTINGS)
        doc.set("sub_accounts", [{"sub_account": "Working", "bank_account": self.f.working}])
        doc.save()
        frappe.local.mpesa_suite_bank_accounts = None
        self.import_statement("Working", rows)
        self.assertEqual({r.bank_account for r in self.lines(transfer)}, {self.f.working, self.f.utility})
        self.assertEqual(len(self.transfer(transfer)), 1)
        self.assertEqual([r.result for r in self.rows(transfer)], ["Reconciled"])

    def test_combined_export_is_refused(self):
        book = openpyxl.Workbook()
        book.active.append(["Receipt No", "Completion Time", "Details", "Transaction Status", "Paid In (KSHs)",
                            "Withdrawn (KSHs)", "Balance (KSHs)"])
        book.active.append([self.receipt(), "22/10/2026 17:34:53", "B2C Payment Charge", "Completed", "0.00", "-5.00",
                            "100.00"])
        out = io.BytesIO()
        book.save(out)
        url = frappe.get_doc({"doctype": "File", "file_name": f"Statements for {SHORTCODE}.xlsx",
                              "content": out.getvalue(), "is_private": 1}).insert().file_url
        doc = frappe.get_doc({"doctype": IMPORT, "statement_file": url, "mpesa_settings": SETTINGS}).insert()
        statement_import.import_now(doc.name)
        status, error = frappe.db.get_value(IMPORT, doc.name, ["status", "error"])
        self.assertEqual(status, "Failed")
        self.assertIn("Export each account separately", error)

    # ------------------------------------------------------------ balance checks
    def test_balance_gap_shows_money_without_a_line_and_closes_on_import(self):
        self.import_statement("Working", [self.row(self.receipt(), 0, "Deposit into MMF Control Account from M-PESA",
                                                   1000, 1000, "Organization Deposit of Funds", "SP")])
        later = (self.now + timedelta(minutes=60)).strftime("%Y%m%d%H%M%S")
        (snap,) = balances.from_account_balance(
            SETTINGS, "Working Account|KES|1500.00|1500.00|0.00|0.00&Utility Account|KES|0.00|0.00|0.00|0.00", later
        )[:1]
        self.assertEqual(balances.check(snap), "Gap")
        self.assertEqual(flt(frappe.db.get_value(SNAPSHOT, snap, "difference")), 500)

        # the statement with the missing row arrives
        self.bank_line(500)
        self.import_statement("Working", [self.row(self.receipt(), 30, "Deposit into MMF Control Account from M-PESA",
                                                   500, 1500, "Organization Deposit of Funds", "SP")])
        self.assertEqual(frappe.db.get_value(SNAPSHOT, snap, "check_status"), "Explained")

    def test_balance_agrees_with_the_lines_since_the_statement(self):
        self.import_statement("Working", [self.row(self.receipt(), 0, "Deposit into MMF Control Account from M-PESA",
                                                   2000, 2000, "Organization Deposit of Funds", "SP")])
        receipt = self.receipt()
        name = f"_T-MB2B-{frappe.generate_hash(length=8)}"
        frappe.get_doc({
            "doctype": "Mpesa B2B Payment Request", "name": name, "docstatus": 1, "status": "Paid",
            "mpesa_settings": SETTINGS, "command_id": "BusinessPayBill", "receiver_shortcode": "888880",
            "account_reference": "INV-1", "amount": 100, "supplier": self.f.supplier, "company": t.COMPANY,
            "transaction_id": receipt, "trans_completed_time": self.now + timedelta(minutes=10), "result_code": "0",
            "debit_party_charges": "Business Pay Bill Charge|KES|2.00",
            "debit_account_balance": "Working Account|KES|1898.00|1898.00|0.00|0.00",
        }).db_insert()
        b2b.book_payment_entry(name)
        later = (self.now + timedelta(minutes=60)).strftime("%Y%m%d%H%M%S")
        snaps = balances.from_account_balance(SETTINGS, "Working Account|KES|1898.00|1898.00|0.00|0.00", later)
        self.assertEqual(balances.check(snaps[0]), "Agrees")

    def test_balance_cannot_be_checked_before_any_statement(self):
        snaps = balances.from_account_balance(SETTINGS, "Merchant Account|KES|10.00|10.00|0.00|0.00",
                                              (self.now - timedelta(days=400)).strftime("%Y%m%d%H%M%S"))
        self.assertEqual(balances.check(snaps[0]), "Cannot Check")
