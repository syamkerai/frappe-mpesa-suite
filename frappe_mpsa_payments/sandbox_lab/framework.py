"""Building blocks for lab scenarios: outcomes, the per-case context and callback posting."""

import json
from dataclasses import dataclass, field

import frappe
import requests
from frappe.utils import get_datetime, now_datetime

RUN = "Mpesa Sandbox Test Run"
CASE = "Mpesa Sandbox Test Case"

PENDING, WAITING, PASSED, FAILED, SKIPPED = "Pending", "Waiting", "Passed", "Failed", "Skipped"
AUTO, GUIDED = "Auto", "Guided"


class Skip(Exception):
    """The scenario cannot run with this test run's setup (says what to set)."""


class Fail(Exception):
    """The scenario failed at start (says why)."""


@dataclass
class Scenario:
    key: str
    group: str
    title: str
    start: object  # fn(ctx) -> None; raises Skip / Fail; may call ctx.done()
    check: object  # fn(ctx) -> (status, message); status in (PASSED, FAILED, WAITING)
    mode: str = AUTO
    instructions: str = ""
    needs: tuple = ()
    depends: str | None = None
    timeout_minutes: int = 10
    request_context: bool = False  # start must run in the web request (Daraja callback URL building)
    expectations: list = field(default_factory=list)


class Ctx:
    """One case of one run: the run document, the case row and the case's saved state."""

    def __init__(self, run, case):
        self.run = run
        self.case = case
        try:
            self.state = json.loads(case.get("state") or "{}") or {}
        except ValueError:
            self.state = {}
        self.result = None  # (status, message) when start already decided

    # ---- state
    def save(self, **values) -> None:
        self.state.update(values)
        frappe.db.set_value(CASE, self.case.name, "state", json.dumps(self.state, default=str, indent=1))

    def set_reference(self, doctype: str, name: str) -> None:
        self.case.reference_doctype, self.case.reference_name = doctype, name
        frappe.db.set_value(CASE, self.case.name, {"reference_doctype": doctype, "reference_name": name})

    def done(self, status: str, message: str) -> None:
        self.result = (status, message)

    def other(self, key: str):
        """(status, state, row) of another scenario in the same run."""
        for row in self.run.cases:
            if row.scenario == key:
                fresh = frappe.db.get_value(CASE, row.name, ["status", "state", "reference_name"], as_dict=True)
                try:
                    state = json.loads(fresh.state or "{}") or {}
                except ValueError:
                    state = {}
                return fresh.status, state, fresh
        return None, {}, None

    def minutes_since(self, key: str = "since") -> float:
        value = self.state.get(key)
        if not value:
            return 0.0
        return (now_datetime() - get_datetime(value)).total_seconds() / 60

    # ---- run settings
    def need(self, fieldname: str, label: str | None = None):
        value = self.run.get(fieldname)
        if not value:
            meta_label = label or fieldname.replace("_", " ").title()
            raise Skip(f"Set {meta_label} on the test run")
        return value


def callback_url(method: str, token: bool = True, **params) -> str:
    from urllib.parse import urlencode

    from frappe.utils import get_url

    from ..daraja.callbacks import with_token

    url = f"{get_url()}/api/method/{method}"
    if params:
        url += "?" + urlencode(params)
    return with_token(url) if token else url


def post_callback(method: str, payload: dict, token: bool = True, **params) -> tuple:
    """POST ``payload`` to one of our own Safaricom-facing endpoints, over the public URL, the way
    Safaricom does (so the IP allow-list, token check and integration user all apply).
    Returns (http_status, the endpoint's answer)."""
    url = callback_url(method, token=token, **params)
    response = requests.post(url, json=payload, timeout=(10, 60))
    try:
        body = response.json()
    except ValueError:
        body = {"_raw": (response.text or "")[:500]}
    answer = body.get("message", body) if isinstance(body, dict) else body
    return response.status_code, answer


def rejected(answer) -> bool:
    return isinstance(answer, dict) and str(answer.get("ResultCode")) not in ("0", "None") and \
        answer.get("ResultDesc") == "Rejected"
