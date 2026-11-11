import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "worker/worker.py"
spec = importlib.util.spec_from_file_location("railway_worker", PATH)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.sent = []
        self.claimed = False
        self.gated = False
        self.fail_finish = False
        self.fail_preflight = False
        self.fail_send = False
        self.send_status = 202
        self.job = {"authorized": True, "campaign_id": "campaign", "recipient_id": "recipient",
                    "entity_id": "entity", "connected_account_id": "ca_test", "sender_email": "sender@example.com",
                    "recipient_email": "person@example.com", "subject": "Exact saved subject",
                    "body": "Exact saved body\n\nLine two", "is_html": False}

    def rpc(self, name, payload):
        self.calls.append((name, payload.copy()))
        if name == "claim_outreach_recipient":
            if self.claimed:
                return []
            self.claimed = True
            return [{"attempt_id": "attempt", "claim_token": "token"}]
        if name == "begin_outreach_send":
            if self.gated:
                return {"authorized": False}
            self.gated = True
            return self.job.copy()
        if name == "finish_outreach_attempt":
            if self.fail_finish:
                raise TimeoutError()
            return payload["p_status"]
        raise AssertionError(name)

    def preflight(self, job):
        if self.fail_preflight:
            raise ValueError("bad account")

    def send(self, job):
        self.sent.append(job.copy())
        if self.fail_send:
            raise TimeoutError()
        return {"status": self.send_status}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "outcomes.sqlite")
        self.journal = worker.Journal(self.path)
        self.api = FakeAPI()
        self.row = {"id": "recipient", "campaign_id": "campaign", "campaigns": {"created_by": "user"}}

    def tearDown(self):
        self.journal.db.close()
        self.temp.cleanup()

    def test_successful_send_records_exact_original_attempt(self):
        worker.execute(self.api, self.journal, self.row)
        finish = self.api.calls[-1][1]
        self.assertEqual(finish["p_status"], "submitted")
        self.assertEqual(finish["p_provider_status_code"], 202)
        self.assertEqual(finish["p_attempt_id"], "attempt")
        self.assertEqual(self.api.sent[0]["body"], "Exact saved body\n\nLine two")
        self.assertEqual(self.journal.db.execute("SELECT count(*) FROM outcomes").fetchone()[0], 0)

    def test_outbox_survives_restart_and_only_retries_finish(self):
        self.api.fail_finish = True
        worker.execute(self.api, self.journal, self.row)
        self.journal.db.close()
        self.journal = worker.Journal(self.path)
        self.api.fail_finish = False
        self.journal.flush(self.api)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.api.calls[-1][0], "finish_outreach_attempt")
        self.assertEqual(self.journal.db.execute("SELECT count(*) FROM outcomes").fetchone()[0], 0)

    def test_repeated_candidate_does_not_send_twice(self):
        worker.execute(self.api, self.journal, self.row)
        worker.execute(self.api, self.journal, self.row)
        self.assertEqual(len(self.api.sent), 1)

    def test_lost_send_response_is_unknown_and_not_retried(self):
        self.api.fail_send = True
        worker.execute(self.api, self.journal, self.row)
        self.assertEqual(self.api.calls[-1][1]["p_status"], "unknown")
        self.assertEqual(len(self.api.sent), 1)

    def test_preflight_failure_never_sends(self):
        self.api.fail_preflight = True
        worker.execute(self.api, self.journal, self.row)
        self.assertEqual(self.api.calls[-1][1]["p_status"], "failed")
        self.assertEqual(self.api.sent, [])

    def test_lost_gate_response_stops_before_provider(self):
        original = self.api.rpc
        def rpc(name, payload):
            result = original(name, payload)
            if name == "begin_outreach_send":
                raise TimeoutError()
            return result
        with patch.object(self.api, "rpc", side_effect=rpc), self.assertRaises(TimeoutError):
            worker.execute(self.api, self.journal, self.row)
        self.assertEqual(self.api.sent, [])

    def test_graph_status_classification(self):
        for status, expected in [(403, "failed"), (429, "failed"), (408, "unknown"), (503, "unknown"), (200, "unknown"), ("202", "unknown")]:
            with self.subTest(status=status):
                api = FakeAPI()
                api.send_status = status
                worker.execute(api, self.journal, self.row)
                self.assertEqual(api.calls[-1][1]["p_status"], expected)

    def test_proxy_pins_connection_and_entity(self):
        api = worker.API("https://example.supabase.co", "sb_secret_test", "composio_test")
        with patch.object(api, "request", return_value={"status": 202}) as request:
            api.send(self.api.job)
        url, headers, payload = request.call_args.args
        self.assertEqual(url, "https://backend.composio.dev/api/v3.1/tools/execute/proxy")
        self.assertEqual(payload["connected_account_id"], "ca_test")
        self.assertEqual(payload["user_id"], "entity")
        self.assertEqual(payload["body"]["message"]["body"]["contentType"], "Text")

    def test_app_migration_syntax(self):
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest("pglast unavailable")
        sql = (PATH.parents[1] / "supabase/migrations/202611100003_app_preview.sql").read_text()
        self.assertEqual(len(parse_sql(sql)), 7)

    def test_queue_keyset_advances_past_blocked_candidates(self):
        from urllib.parse import urlsplit, parse_qs
        api = worker.API("https://example.supabase.co", "sb_secret_test", "composio_test")
        with patch.object(api, "request", return_value=[]) as request:
            api.queued(("2026-11-10T09:00:00+00:00", "recipient-id"))
        params = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(params["order"], ["due_at.asc,id.asc"])
        self.assertEqual(params["campaigns.status"], ["in.(scheduled,submitted)"])
        self.assertTrue(params["due_at"][0].startswith("lte."))
        self.assertIn("id.gt.recipient-id", params["or"][0])


if __name__ == "__main__":
    unittest.main()
