"""Bench tests (round 4.3): a migrated site shows numbers and names in full (M-Pesa Suite Settings >
Show Numbers and Names in Full, ticked by the migrate's defaults), and the name M-Pesa last paid a phone
under is read from the site's B2C payouts: the latest Paid one, in any stored form of the number (round
4.5: through the M-Pesa Known Name list each Result adds to)."""

import datetime

import frappe

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_out import claim_payees, known_names
from mpsa_suite.utils import privacy

REQ = "B2C Disbursement Request"


class TestFullDetails(IntegrationTestCase):
    def test_a_migrated_site_shows_numbers_and_names_in_full(self):
        self.assertEqual(frappe.db.get_single_value(privacy.SUITE, privacy.FIELD), 1)
        self.assertTrue(privacy.full_details())
        self.assertEqual(privacy.phone("0722 000 111"), "254722000111")

    def _paid(self, phone, received, minutes, status="Paid"):
        doc = frappe.new_doc(REQ)
        doc.update({"phone_number": phone, "receiver_public_name": received, "status": status, "amount": 100,
                    "transaction_completed_datetime": datetime.datetime(2026, 10, 29, 20, 0)
                    + datetime.timedelta(minutes=minutes)})
        doc.name = frappe.generate_hash(length=12)
        doc.docstatus = 1
        doc.db_insert()  # rolled back with the test
        # round 4.5: the names are read from the M-Pesa Known Name list, which each Result adds to
        known_names.record_b2c(doc)
        return doc.name

    def test_the_name_mpesa_paid_a_phone_under_is_read_from_the_sites_payouts(self):
        self._paid("254799000301", "254799000301 - _T OLD NAME", 0)
        latest = self._paid("0799000301", "0799000301 - _T JANE NJERI", 30)
        self._paid("254799000301", "254799000301 - _T NOT PAID", 60, status="Failed")
        found = claim_payees.mpesa_names(["0799 000 301", "0799000302"])
        self.assertEqual(list(found), ["254799000301"])
        self.assertEqual((found["254799000301"].name, found["254799000301"].payout), ("_T JANE NJERI", latest))
