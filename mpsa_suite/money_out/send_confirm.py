"""Send via M-Pesa / Approve: the one deliberate step that sends a payout or approves a payee
account, in place of Submit (the user's decision 2, 2026-10-27). Signatures only: WP1 builds it.

The contract:
- ``prepare_send(doctype, name)``: checks that the session user may send (or approve) the
  document now (roles, document permission, a named login, maker-checker) and returns what the
  confirmation shows (receiver; destination: phone, paybill and account, till, or bank and
  account; amount; charge estimate; who prepared it) with a token. The token is bound to the
  user, the DocType, the name, the document's ``modified``, the amount and the destination. It
  can be used from 5 seconds to 10 minutes after it is issued, and once.
- ``confirm_send(doctype, name, token)``: takes the token and submits (or approves) the document
  as that user, after checking that nothing the token is bound to has changed.
- ``assert_deliberate(doc)``: called from every payout's before_submit (B2B request, B2C Payment
  Disbursement, B2C Disbursement Request, Reversal) and every payee account approval. Refuses
  unless the document is being sent by ``confirm_send`` with its token, or inside
  ``deliberate()``. One rule that closes list-view bulk submit, REST, ``frappe.client.submit``
  and ``run_doc_method``.
- ``deliberate(reason)``: for server flows that are themselves the deliberate step (Pay
  Selected's confirm, the payment schedule's send jobs), so the documents they submit pass
  ``assert_deliberate``.
"""

from contextlib import contextmanager


def prepare_send(doctype: str, name: str) -> dict:
    """What the Send via M-Pesa (or Approve) confirmation shows, with its token."""
    raise NotImplementedError("Send via M-Pesa is not built yet")


def confirm_send(doctype: str, name: str, token: str):
    """Send (or approve) ``name`` with the token ``prepare_send`` gave this user."""
    raise NotImplementedError("Send via M-Pesa is not built yet")


def assert_deliberate(doc) -> None:
    """Throw unless ``doc`` is being sent or approved through the deliberate step. Lets
    everything through until WP1 builds the step."""


@contextmanager
def deliberate(reason: str):
    """Documents submitted inside this block pass ``assert_deliberate``: ``reason`` names the
    server flow that is itself the deliberate step. Does nothing until WP1 builds the step."""
    yield
