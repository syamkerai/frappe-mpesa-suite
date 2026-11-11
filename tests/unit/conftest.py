import sys
from pathlib import Path

# Import the app's pure-Python helpers without a Frappe bench.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


import importlib  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def masked_numbers(monkeypatch):
    """M-Pesa Suite Settings > Show Numbers and Names in Full switched off (round 4.3): phones read
    2547****0111 and account numbers ****6789. List it after the test's site fixture, which installs
    the fake Frappe the app's modules are imported under."""
    privacy = importlib.import_module("mpsa_suite.utils.privacy")
    monkeypatch.setattr(privacy, "full_details", lambda: False)
