"""Bench tests: an STK number is normalised (2547... / 2541..., the newer 014X / 018X lines too) or
refused with the forms to type, never guessed from its last 9 digits, which can be another
subscriber's; and the refusal comes before any prompt is built."""

from unittest.mock import patch

import frappe

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.mpsa_suite.api import m_pesa_api


class TestStkPhoneNumbers(IntegrationTestCase):
    def test_a_kenyan_mobile_number_is_normalised(self):
        for typed, msisdn in (("0111 444 514", "254111444514"), ("0142000280", "254142000280"),
                              ("+254 0712 345 678", "254712345678"), ("254712345678", "254712345678"),
                              ("00254 712 345 678", "254712345678"), ("0254712345678", "254712345678")):
            self.assertEqual(m_pesa_api.sanitize_mobile_number(typed), msisdn, typed)

    def test_a_mistyped_number_is_refused_not_guessed(self):
        # the old '254' + last 9 digits gave another subscriber's number (0112 345 670 for the first two)
        for typed in ("0111234567.0", "0111 234 5670", "25416055453"):
            with self.assertRaises(frappe.ValidationError, msg=typed) as caught:
                m_pesa_api.sanitize_mobile_number(typed)
            self.assertIn("01XX XXX XXX", str(caught.exception))
        with self.assertRaises(frappe.ValidationError):
            m_pesa_api.sanitize_mobile_number("")

    def test_a_prompt_to_a_mistyped_number_is_refused_before_it_is_built(self):
        with patch.object(m_pesa_api.apis, "require"), patch.object(m_pesa_api, "build_callback_url") as built:
            with self.assertRaises(frappe.ValidationError):
                m_pesa_api.initiate_stk_push(payment_gateway="Mpesa-TEST", phone_number="0111 234 5670",
                                             request_amount=10)
        built.assert_not_called()
