"""Bench tests: the Daraja token cache against Frappe's real RedisWrapper (its per-request
``frappe.local.cache`` differs between v15 and v16), and money-out Result matching against
the real doctypes and Frappe's own parsing of Safaricom's JSON POST."""

import pickle
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.app import make_form_dict
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from mpsa_suite.compat import IntegrationTestCase, get_cache
from mpsa_suite.daraja import callbacks, client
from mpsa_suite.money_out import api
from mpsa_suite.money_out.b2b import B2B_DOCTYPE
from mpsa_suite.money_out.b2c_status import QUERY_DOCTYPE
from mpsa_suite.money_out.results import find_request
from mpsa_suite.money_out.reversal import REVERSAL_DOCTYPE
from mpsa_suite.utils.doctype_names import B2C_REQUEST_DOCTYPE

SUITE = "M-Pesa Suite Settings"


class Resp:
    def __init__(self, data):
        self._data, self.status_code, self.ok, self.text = data, 200, True, str(data)

    def json(self):
        return self._data


class TestTokenCache(IntegrationTestCase):
    def setUp(self):
        self.consumer_key = f"fix-tokens-{frappe.generate_hash(length=10)}"
        self.key = client._token_key(self.consumer_key, client.SANDBOX_URL)
        self.cache = get_cache()
        self.minted = []

        def fake_oauth(url, auth=None, timeout=None):
            self.minted.append(f"minted-{len(self.minted) + 1}")
            return Resp({"access_token": self.minted[-1], "expires_in": "3599"})

        patcher = patch.object(client.requests, "get", fake_oauth)  # never reaches Safaricom
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.cache.delete_value(self.key)

    def _fetch(self):
        return client.fetch_token(self.consumer_key, "secret", client.SANDBOX_URL)

    def _other_worker_stores(self, token):
        """Another worker's set_value: Redis changes, this job's frappe.local.cache does not."""
        self.cache.set(self.cache.make_key(self.key), pickle.dumps(token), ex=600)

    def test_token_fetched_after_a_miss_is_reused_in_the_same_job(self):
        self.assertEqual([self._fetch() for _ in range(3)], ["minted-1"] * 3)
        self.assertEqual(len(self.minted), 1)

    def test_worker_that_waited_on_the_lock_reuses_the_token_stored_meanwhile(self):
        self.cache.get_value(self.key)  # an earlier plain read in this job kept the miss locally
        real_lock = client._fetch_lock

        @contextmanager
        def lock_while_other_worker_fetches(cache, key):
            with real_lock(cache, key):
                self._other_worker_stores("from-other-worker")
                yield

        with patch.object(client, "_fetch_lock", lock_while_other_worker_fetches):
            self.assertEqual(self._fetch(), "from-other-worker")
        self.assertEqual(self.minted, [])

    def test_invalidate_keeps_a_token_another_worker_stored(self):
        self.cache.set_value(self.key, "mine")  # also kept in this job's local cache
        self._other_worker_stores("theirs")
        settings = frappe._dict(consumer_key=self.consumer_key, sandbox=1)

        client.invalidate_token(settings, rejected="mine")
        self.assertEqual(self._fetch(), "theirs")  # kept, and the retry picks it up
        self.assertEqual(self.minted, [])

        client.invalidate_token(settings, rejected="theirs")
        self.assertEqual(self._fetch(), "minted-1")

    def test_invalidate_never_deletes_a_token_stored_after_the_refused_one_was_dropped(self):
        # Worker 1 already dropped T0 and is minting Ta; its set lands between our GET and DEL.
        settings = frappe._dict(consumer_key=self.consumer_key, sandbox=1)
        real_get = self.cache.get_value

        def read_then_other_worker_stores(key, *args, **kwargs):
            value = real_get(key, *args, **kwargs)
            self._other_worker_stores("Ta")
            return value

        with patch.object(self.cache, "get_value", read_then_other_worker_stores):
            client.invalidate_token(settings, rejected="T0")
        self.assertEqual(self._fetch(), "Ta")
        self.assertEqual(self.minted, [])


class TestResultMatching(IntegrationTestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        settings = frappe.get_single(SUITE)
        settings.enforce_token_results = 0  # the shipped default: anyone can reach the endpoint
        settings.enforce_ip_allowlist = 0
        settings.missing_integration_user = "Warn"
        settings.flags.ignore_permissions = True
        settings.save()
        self.token = callbacks.callback_token()
        self.assertTrue(self.token)

    def tearDown(self):
        frappe.local.form_dict = frappe._dict()
        frappe.db.rollback()
        frappe.set_user("Administrator")

    def _insert(self, doctype, prefix, **values):
        name = f"{prefix}-FIXTOK-{frappe.generate_hash(length=8)}"
        frappe.get_doc({"doctype": doctype, "name": name, "docstatus": 1, **values}).db_insert()
        return name

    def _post(self, method, body, **url_args):
        """Call ``method`` exactly as Frappe does for Safaricom's JSON POST to its Result URL."""
        environ = EnvironBuilder(
            method="POST", path=f"/api/method/{api.__name__}.{method.__name__}", query_string=url_args, json=body
        ).get_environ()
        request = Request(environ)
        make_form_dict(request)
        with patch.object(frappe.local, "request", request, create=True):
            return method(**frappe.local.form_dict)

    # ------------------------------------------------------------ find_request
    def test_acked_request_is_matched_by_its_ids_only(self):
        name = self._insert(B2B_DOCTYPE, "MB2B", status="Initiated", conversation_id="AG_FT_1",
                            originator_conversation_id="OC_FT_1")
        forged = {"ResultCode": 0, "TransactionID": "QAB1234567"}
        self.assertIsNone(find_request(B2B_DOCTYPE, forged, name))
        made_up = dict(forged, ConversationID="AG_made_up", OriginatorConversationID="OC_made_up")
        self.assertIsNone(find_request(B2B_DOCTYPE, made_up, name))
        real = dict(forged, ConversationID="AG_FT_1", OriginatorConversationID="OC_FT_1")
        self.assertEqual(find_request(B2B_DOCTYPE, real, name).name, name)

    def test_ref_matches_only_a_request_that_may_have_been_sent_without_an_ack(self):
        result = {"ResultCode": 0, "ConversationID": "AG_FT_2", "OriginatorConversationID": "OC_FT_2"}
        unknown = self._insert(B2B_DOCTYPE, "MB2B", status="Unknown")
        self.assertEqual(find_request(B2B_DOCTYPE, result, unknown).name, unknown)
        self.assertIsNone(find_request(B2B_DOCTYPE, {"ResultCode": 0}, unknown))  # no IDs
        for status in ("Failed", "Not Initiated"):
            self.assertIsNone(find_request(B2B_DOCTYPE, result, self._insert(B2B_DOCTYPE, "MB2B", status=status)))

        lost_ack = self._insert(QUERY_DOCTYPE, "MB2CQ", status="Failed to Send",
                                outcome_reason="Transport error (unknown)")
        self.assertEqual(find_request(QUERY_DOCTYPE, result, lost_ack).name, lost_ack)
        for reason in ("Transport error (not_sent)", "500.001.1001 Unable to lock subscriber"):
            query = self._insert(QUERY_DOCTYPE, "MB2CQ", status="Failed to Send", outcome_reason=reason)
            self.assertIsNone(find_request(QUERY_DOCTYPE, result, query))

    # ------------------------------------------------------------ over HTTP
    def test_queue_timeout_gets_ref_from_the_url_of_a_json_post(self):
        name = self._insert(B2B_DOCTYPE, "MB2B", status="Initiated", conversation_id="AG_FT_3")
        answer = self._post(api.b2b_queue_timeout, {"Result": {"ResultCode": 1}}, ref=name, t=self.token)
        self.assertEqual(answer, api.ACCEPTED)
        self.assertNotIn("ref", frappe.local.form_dict)  # Frappe passes a JSON body's keys only
        self.assertEqual(frappe.db.get_value(B2B_DOCTYPE, name, "queue_timed_out"), 1)

    def test_forged_result_without_ids_never_settles_an_acked_request(self):
        name = self._insert(B2B_DOCTYPE, "MB2B", status="Initiated", conversation_id="AG_FT_4",
                            originator_conversation_id="OC_FT_4")
        forged = {"Result": {"ResultCode": 0, "TransactionID": "QAB1234567"}}
        self._post(api.b2b_result, dict(forged, ref=name))  # ref in the body, no token
        self._post(api.b2b_result, forged, ref=name)  # ref in the URL, no token
        self._post(api.b2b_result, forged, ref=name, t=self.token)  # even with the token: no IDs
        row = frappe.db.get_value(B2B_DOCTYPE, name, ["status", "result_code", "transaction_id"], as_dict=True)
        self.assertEqual((row.status, row.result_code, row.transaction_id), ("Initiated", None, None))

    def test_operator_ids_from_the_body_never_match(self):
        # Frappe reads a list filter value as [operator, value]: ["is", "set"] would match the
        # newest acked request. No ref, no token: only the body.
        conv, occ = "AG_20191219_00005797af5d7d75f652", "16740-34861180-1"
        b2b = self._insert(B2B_DOCTYPE, "MB2B", status="Initiated", conversation_id=conv,
                           originator_conversation_id=occ)
        rev = self._insert(REVERSAL_DOCTYPE, "MRV", status="Initiated", conversation_id=f"{conv}R",
                           originator_conversation_id=f"{occ}R")
        payout = self._insert(B2C_REQUEST_DOCTYPE, "B2C", status="Initiated", amount=300,
                              originator_conversation_id="OCID-FT-B2C")
        query = self._insert(QUERY_DOCTYPE, "MB2CQ", docstatus=0, status="Sent", b2c_request=payout,
                             conversation_id=f"{conv}Q", originator_conversation_id=f"{occ}Q")
        params = {"ResultParameter": [{"Key": "TransactionStatus", "Value": "Failed"},
                                      {"Key": "Amount", "Value": "300.00"},
                                      {"Key": "OriginatorConversationID", "Value": "OCID-FT-B2C"}]}

        for value in (["is", "set"], ["like", "%"]):
            for key in ("ConversationID", "OriginatorConversationID"):
                forged = {"ResultType": 0, "ResultCode": 0, key: value, "TransactionID": "QAB1234567"}
                self._post(api.b2b_result, {"Result": forged})
                self._post(api.reversal_result, {"Result": forged})
                self._post(api.b2c_status_result, {"Result": dict(forged, ResultParameters=params)})

        row = frappe.db.get_value(B2B_DOCTYPE, b2b, ["status", "result_code", "transaction_id"], as_dict=True)
        self.assertEqual((row.status, row.result_code, row.transaction_id), ("Initiated", None, None))
        row = frappe.db.get_value(REVERSAL_DOCTYPE, rev, ["status", "result_code"], as_dict=True)
        self.assertEqual((row.status, row.result_code), ("Initiated", None))
        self.assertEqual(frappe.db.get_value(QUERY_DOCTYPE, query, "status"), "Sent")
        row = frappe.db.get_value(B2C_REQUEST_DOCTYPE, payout, ["status", "result_code"], as_dict=True)
        self.assertEqual((row.status, row.result_code), ("Initiated", None))

        # Safaricom's own IDs still pass
        self.assertEqual(find_request(B2B_DOCTYPE, {"ConversationID": conv}, None).name, b2b)
        self.assertEqual(find_request(B2B_DOCTYPE, {"OriginatorConversationID": occ}, None).name, b2b)

    def test_ref_with_operator_ids_is_refused(self):
        name = self._insert(B2B_DOCTYPE, "MB2B", status="Unknown")
        forged = {"ResultCode": 0, "ConversationID": ["is", "set"], "OriginatorConversationID": ["like", "%"]}
        self._post(api.b2b_result, {"Result": forged}, ref=name, t=self.token)
        self.assertEqual(frappe.db.get_value(B2B_DOCTYPE, name, "status"), "Unknown")
