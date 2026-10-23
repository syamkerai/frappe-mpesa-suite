# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt
# Query/result coverage is in tests/unit/test_money_out_flow.py.

import frappe

from mpsa_suite.compat import IntegrationTestCase


class TestMpesaB2CStatusQuery(IntegrationTestCase):
    def test_nobody_can_create_or_edit_from_desk(self):
        meta = frappe.get_meta("Mpesa B2C Status Query")
        self.assertFalse([p.role for p in meta.permissions if p.create or p.write])
