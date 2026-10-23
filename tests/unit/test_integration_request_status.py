"""Integration Request.status only takes Queued/Authorized/Completed/Cancelled/Failed. Upstream passed
the B2C payout status ("Paid") and every successful B2C result failed validation and rolled back."""

import importlib

import pytest

import fake_frappe


@pytest.fixture
def mod():
    frappe, saved = fake_frappe.install()
    try:
        yield importlib.import_module("mpsa_suite.connectors.base_connector")
    finally:
        fake_frappe.uninstall(saved)


@pytest.mark.parametrize(
    "given,expected",
    [("Completed", "Completed"), ("Failed", "Failed"), ("Queued", "Queued"), ("Cancelled", "Cancelled"),
     ("Paid", "Completed"), ("Initiated", "Completed"), ("failed", "Failed"), (None, "Completed")],
)
def test_integration_request_status(mod, given, expected):
    assert mod.integration_request_status(given) == expected
