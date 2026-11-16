import importlib.util
import json
import base64
import hashlib
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
        if name == "prepare_outreach_delivery":
            return self.job.copy() if not self.gated else {"authorized": False}
        if name == "dispatch_outreach_delivery":
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


class DraftAPI(FakeAPI):
    def __init__(self, size=3_500_000):
        super().__init__()
        self.content = b"x" * size
        self.job["cc"] = ["manager@example.com"]
        self.job["attachments"] = [{"id": "file-id", "name": "proposal.pdf", "size_bytes": size,
                                    "content_type": "application/pdf", "sha256": hashlib.sha256(self.content).hexdigest(), "path": "files/user/file-id"}]
        self.draft = None
        self.attached = []
        self.draft_creates = 0
        self.upload_offsets = []
        self.server_offset = 0
        self.session_creates = 0
        self.fail_creation = False
        self.fail_chunk = False
        self.fail_final_chunk = False
        self.fail_small = False
        self.tamper_body = False
        self.tamper_file = False
        self.permission_error = False

    def attachment(self, file):
        return self.content

    def graph(self, job, method, path, body=None):
        if self.permission_error:
            raise worker.APIHTTPError(403)
        if path.startswith("mailFolders/drafts/messages"):
            return {"value": [{"id": "draft"}] if self.draft else []}
        if method == "POST" and path == "messages":
            self.draft_creates += 1
            self.draft = body.copy()
            if self.fail_creation:
                self.fail_creation = False
                raise worker.PreparationPending("lost creation response")
            return {"id": "draft"}
        if path.endswith("/attachments/createUploadSession"):
            self.session_creates += 1
            return {"uploadUrl": "https://outlook.office.com/session?authtoken=test"}
        if path.endswith("/attachments") and method == "POST":
            self.attached.append({"id": "provider-file", "name": "proposal.pdf", "size": len(self.content)})
            if self.fail_small:
                self.fail_small = False
                raise worker.PreparationPending("lost small attachment response")
            return {"id": "provider-file"}
        if "/attachments?" in path:
            return {"value": self.attached}
        if "/attachments/provider-file" in path:
            content = b"y" * len(self.content) if self.tamper_file else self.content
            return {"contentBytes": base64.b64encode(content).decode()}
        if path.startswith("messages/draft?"):
            return {**self.draft, "isDraft": True, "body": {"content": "tampered" if self.tamper_body else self.job["body"]}}
        raise AssertionError((method, path))

    def upload_status(self, url):
        return {"nextExpectedRanges": [f"{self.server_offset}-"]}

    def upload_range(self, url, content, offset):
        self.upload_offsets.append(offset)
        self.server_offset = min(len(content), offset + 3_276_800)
        if self.server_offset == len(content):
            self.attached.append({"id": "provider-file", "name": "proposal.pdf", "size": len(content)})
            if self.fail_final_chunk:
                self.fail_final_chunk = False
                raise worker.PreparationPending("lost final chunk response")
            return 201, {}
        if self.fail_chunk:
            self.fail_chunk = False
            raise worker.PreparationPending("lost chunk response")
        return 200, {"nextExpectedRanges": [f"{self.server_offset}-"]}

    def proxy(self, job, method, endpoint, body=None, parameters=None):
        assert endpoint.endswith("/messages/draft/send")
        assert self.gated
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
            if name == "dispatch_outreach_delivery":
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

    def pending_row(self):
        return {"user_id": "user", "campaign_id": "campaign", "recipient_id": "recipient", "attempt_id": "attempt", "claim_token": "token"}

    def test_large_upload_resumes_after_restart_and_lost_chunk_response(self):
        api = DraftAPI()
        api.fail_chunk = True
        worker.execute(api, self.journal, self.row)
        self.assertEqual(api.sent, [])
        self.assertFalse(api.gated)
        self.journal.db.close()
        self.journal = worker.Journal(self.path)
        worker.execute(api, self.journal, self.pending_row())
        self.assertEqual(api.upload_offsets, [0, 3_276_800])
        self.assertEqual(api.draft_creates, 1)
        self.assertEqual(api.session_creates, 1)
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(api.sent[0]["cc"], ["manager@example.com"])
        self.assertEqual(api.calls[-1][1]["p_status"], "submitted")

    def test_lost_creation_response_recovers_original_draft(self):
        api = DraftAPI(12)
        api.fail_creation = True
        worker.execute(api, self.journal, self.row)
        self.assertEqual(api.sent, [])
        worker.execute(api, self.journal, self.pending_row())
        self.assertEqual(api.draft_creates, 1)
        self.assertEqual(len(api.attached), 1)
        self.assertEqual(len(api.sent), 1)

    def test_lost_small_attachment_response_does_not_attach_twice(self):
        api = DraftAPI(12)
        api.fail_small = True
        worker.execute(api, self.journal, self.row)
        worker.execute(api, self.journal, self.pending_row())
        self.assertEqual(len(api.attached), 1)
        self.assertEqual(len(api.sent), 1)

    def test_lost_final_upload_response_reconciles_before_send(self):
        api = DraftAPI()
        api.fail_final_chunk = True
        worker.execute(api, self.journal, self.row)
        self.assertEqual(api.sent, [])
        worker.execute(api, self.journal, self.pending_row())
        self.assertEqual(len(api.attached), 1)
        self.assertEqual(len(api.sent), 1)

    def test_graph_metadata_size_overhead_does_not_change_actual_byte_validation(self):
        api = DraftAPI(12)
        api.fail_small = True
        worker.execute(api, self.journal, self.row)
        api.attached[0]["size"] = 64  # Graph metadata size need not be raw byte length.
        worker.execute(api, self.journal, self.pending_row())
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(len(api.attached), 1)

    def test_25_mb_attachment_uses_bounded_chunks_and_final_one_time_gate(self):
        api = DraftAPI(25_000_000)
        worker.execute(api, self.journal, self.row)
        self.assertEqual(api.upload_offsets, list(range(0, 25_000_000, 3_276_800)))
        self.assertEqual(len(api.sent), 1)
        worker.execute(api, self.journal, self.pending_row())
        self.assertEqual(len(api.sent), 1)

    def test_changed_outlook_draft_or_file_blocks_dispatch(self):
        for setting in ("tamper_body", "tamper_file", "permission_error"):
            with self.subTest(setting=setting):
                api = DraftAPI(12)
                setattr(api, setting, True)
                worker.execute(api, self.journal, self.row)
                self.assertFalse(api.gated)
                self.assertEqual(api.sent, [])
                self.assertEqual(api.calls[-1][1]["p_status"], "failed")

    def test_consumed_dispatch_gate_is_not_replayed_after_restart(self):
        api = DraftAPI(12)
        original = api.rpc
        def rpc(name, payload):
            result = original(name, payload)
            if name == "dispatch_outreach_delivery":
                raise TimeoutError()
            return result
        with patch.object(api, "rpc", side_effect=rpc), self.assertRaises(TimeoutError):
            worker.execute(api, self.journal, self.row)
        self.journal.db.close()
        self.journal = worker.Journal(self.path)
        worker.execute(api, self.journal, self.pending_row())
        self.assertEqual(api.sent, [])
        self.assertEqual(api.calls[-1][1]["p_status"], "unknown")

    def test_draft_send_response_loss_never_resends(self):
        api = DraftAPI(12)
        api.fail_send = True
        worker.execute(api, self.journal, self.row)
        self.assertEqual(api.calls[-1][1]["p_status"], "unknown")
        worker.execute(api, self.journal, self.pending_row())
        self.assertEqual(len(api.sent), 1)

    def test_suppressed_cc_preparation_rejection_is_actionable_without_dispatch(self):
        with patch.object(self.api, "rpc", side_effect=worker.APIHTTPError(400)):
            worker.execute(self.api, self.journal, self.pending_row())  # finish response fails too
        receipt = json.loads(self.journal.db.execute("SELECT receipt FROM outcomes").fetchone()[0])
        self.assertEqual(receipt["p_status"], "failed")
        self.assertEqual(self.api.sent, [])

    def test_upload_url_cannot_receive_service_credentials_or_redirect(self):
        api = worker.API("https://example.supabase.co", "sb_secret_test", "composio_test")
        for url in ["https://attacker.example/session", "http://outlook.office.com/session", "https://user@outlook.office.com/session", "https://outlook.office.com:444/session"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                api.upload_range(url, b"hello", 0)
        class Response:
            status = 201
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return b""
        with patch.object(api.opener, "open", return_value=Response()) as opened:
            api.upload_range("https://outlook.office.com/session?authtoken=test", b"hello", 0)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.get_header("Content-range"), "bytes 0-4/5")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Apikey"))
        self.assertIsNone(request.get_header("X-api-key"))


if __name__ == "__main__":
    unittest.main()
