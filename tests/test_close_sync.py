import base64
import copy
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

spec = importlib.util.spec_from_file_location("close_sync", Path(__file__).resolve().parents[1] / "sync/worker.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
CSV = b"\xef\xbb\xbfid,lead_name,display_name,primary_contact_name,primary_contact_first_name,primary_contact_primary_email,custom.Industry,date_created\nlead_A,Northstar,,Alex Morgan,Alex,alex@example.com,Technology,2026-11-01T10:00:00Z\nlead_B,,Summit,Jordan Lee,Jordan,jordan@example.com,Finance,2026-11-02T10:00:00Z\n"


class FakeDB:
    def __init__(self):
        self.job = {"id": str(uuid4()), "lease_token": str(uuid4()), "status": "queued", "phase": "exporting", "export_id": None,
                    "synced_at": "2026-11-16T10:00:00+00:00", "total_rows": None, "rows_processed": 0, "csv_sha256": None}
        self.calls = []; self.rows = {}; self.batches = []
        self.lose_batch = None

    def completed(self, ids):
        return {self.job["id"]} & set(ids) if self.job["status"] == "completed" else set()

    def rpc(self, name, data):
        self.calls.append((name, copy.deepcopy(data)))
        j = self.job
        if name == "claim_close_lead_sync":
            if j["status"] not in ("queued", "retrying"):
                return None
            j["status"] = j["phase"]; j["lease_token"] = str(uuid4())
            return copy.deepcopy(j)
        if data["p_token"] != j["lease_token"] or j["status"] not in ("exporting", "downloading", "importing"):
            raise sync.LeaseLost()
        if name == "update_close_lead_sync":
            op, value = data["p_operation"], data["p_data"]
            if op == "export": j["export_id"] = value["export_id"]
            if op == "download": j["status"] = j["phase"] = "downloading"
            if op == "prepared":
                if j["csv_sha256"] and j["csv_sha256"] != value["csv_sha256"]:
                    raise sync.SyncFailure("Export changed")
                j.update(value); j["status"] = j["phase"] = "importing"
            if op == "complete":
                assert j["rows_processed"] == j["total_rows"]
                j["status"] = "completed"
            if op == "failure":
                j["status"] = "retrying" if value.get("retryable") else "failed"
                j["error"] = value["error"]
            return copy.deepcopy(j)
        if name == "import_close_lead_batch":
            assert data["p_offset"] == j["rows_processed"]
            loss = self.lose_batch; self.lose_batch = None
            if loss == "before":
                raise sync.SyncFailure("Response lost", True)
            for row in data["p_rows"]: self.rows[row["close_lead_id"]] = copy.deepcopy(row)
            self.batches.append(copy.deepcopy(data)); j["rows_processed"] += len(data["p_rows"])
            if loss == "after":
                raise sync.SyncFailure("Response lost", True)
            return copy.deepcopy(j)
        raise AssertionError(name)


class FakeClose:
    def __init__(self, content=CSV):
        self.content = content; self.creates = 0; self.downloads = 0; self.fail_download = False
    def create_export(self):
        self.creates += 1; return "expo_Test"
    def export(self, export_id):
        assert export_id == "expo_Test"
        return {"status": "done", "download_url": "https://api.close.com/download"}
    def download(self, url, destination, heartbeat, suffix):
        self.downloads += 1
        if self.fail_download:
            self.fail_download = False
            raise sync.SyncFailure("Download interrupted", True)
        destination.write_bytes(self.content)


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class CloseSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.cache = Path(self.temp.name)
        self.db, self.close = FakeDB(), FakeClose()
        self.worker = sync.Worker(self.db, self.close, self.cache)
    def tearDown(self): self.temp.cleanup()

    def test_exact_primary_contact_mapping_keeps_raw_source_strings(self):
        record = sync.map_lead({"id": "lead_A", "lead_name": " ", "display_name": " Company ", "primary_contact_name": " Alex ", "primary_contact_first_name": " Alex ", "primary_contact_last_name": " Morgan ", "primary_contact_title": " Director ", "primary_contact_primary_email": " alex@example.com ", "url": " https://example.com ", "status_label": " Customer ", "custom.Industry": " Technology ", "missing": None}, "timestamp")
        self.assertEqual(record["company_name"], "Company")
        self.assertEqual(record["email"], "alex@example.com")
        self.assertEqual(record["contact_title"], "Director")
        self.assertEqual(record["last_name"], "Morgan")
        self.assertEqual(record["source_data"]["display_name"], " Company ")
        self.assertEqual(record["source_data"]["missing"], "")
        self.assertEqual(record["last_synced_at"], "timestamp")

    def test_full_sync_uses_bom_csv_mapping_and_completes(self):
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.db.job["status"], "completed")
        self.assertEqual(self.db.job["rows_processed"], 2)
        self.assertEqual(self.db.rows["lead_B"]["company_name"], "Summit")
        self.assertEqual(self.db.rows["lead_A"]["source_data"]["date_created"], "2026-11-01T10:00:00Z")
        self.assertEqual(self.close.creates, 1)
        self.assertFalse(list(self.cache.iterdir()))

    def test_entire_export_is_validated_before_any_import(self):
        for content in [b"id,email\n", b"email\na@example.com\n", b"id,email\nlead_A,a@example.com\nlead_A,b@example.com\n", b"id,email\nlead_A,a@example.com\n,b@example.com\n", b"id,id\nlead_A,lead_A\n", b'id,email\nlead_A,"unterminated\n']:
            with self.subTest(content=content):
                db, close = FakeDB(), FakeClose(content)
                sync.Worker(db, close, self.cache).run_once()
                self.assertEqual(db.job["status"], "failed")
                self.assertEqual(db.rows, {})
                self.assertEqual(db.batches, [])

    def test_lost_committed_batch_response_resumes_without_double_counting(self):
        self.db.lose_batch = "after"
        self.worker.run_once()
        self.assertEqual(self.db.job["status"], "retrying")
        self.assertEqual(self.db.job["rows_processed"], 2)
        sync.Worker(self.db, self.close, self.cache).run_once()
        self.assertEqual(self.db.job["status"], "completed")
        self.assertEqual(len(self.db.batches), 1)
        self.assertEqual(self.close.creates, 1)
        self.assertEqual(self.close.downloads, 1)

    def test_lost_uncommitted_batch_response_retries_original_offset(self):
        self.db.lose_batch = "before"
        self.worker.run_once()
        self.assertEqual(self.db.job["rows_processed"], 0)
        self.worker.run_once()
        self.assertEqual(self.db.job["status"], "completed")
        self.assertEqual(self.db.batches[0]["p_offset"], 0)
        self.assertEqual(self.close.creates, 1)

    def test_completed_cache_cleanup_handles_lost_completion_response(self):
        original = self.db.rpc
        def rpc(name, data):
            result = original(name, data)
            if data.get("p_operation") == "complete":
                raise sync.SyncFailure("Completion response lost", True)
            return result
        with patch.object(self.db, "rpc", side_effect=rpc):
            self.worker.run_once()
        self.assertEqual(self.db.job["status"], "completed")
        self.assertTrue(list(self.cache.iterdir()))
        other = self.cache / (str(uuid4()) + ".csv"); other.write_bytes(b"failed job cache")
        unrelated = self.cache / "notes.txt"; unrelated.write_text("not an application cache")
        named_note = self.cache / (self.db.job["id"] + ".txt"); named_note.write_text("keep")
        self.worker.cleanup_completed()
        self.assertTrue(other.exists()); self.assertTrue(unrelated.exists()); self.assertTrue(named_note.exists())
        self.assertFalse((self.cache / (self.db.job["id"] + ".csv")).exists())

    def test_download_failure_resumes_same_export(self):
        self.close.fail_download = True
        self.worker.run_once()
        self.assertEqual(self.db.job["status"], "retrying")
        self.assertEqual(self.db.job["export_id"], "expo_Test")
        self.worker.run_once()
        self.assertEqual(self.close.creates, 1)
        self.assertEqual(self.close.downloads, 2)
        self.assertEqual(self.db.job["status"], "completed")

    def test_corrupted_cache_is_redownloaded_without_mixing_exports(self):
        self.db.lose_batch = "before"
        self.worker.run_once()
        (self.cache / (self.db.job["id"] + ".csv")).write_bytes(b"corrupt")
        self.worker.run_once()
        self.assertEqual(self.close.downloads, 2)
        self.assertEqual(self.db.job["status"], "completed")

    def test_changed_export_hash_blocks_resume_before_any_new_batch(self):
        self.db.lose_batch = "after"; self.worker.batch_size = 1
        self.worker.run_once()
        self.assertEqual(self.db.job["rows_processed"], 1)
        (self.cache / (self.db.job["id"] + ".csv")).unlink()
        self.close.content = CSV.replace(b"Northstar", b"New Name")
        self.worker.run_once()
        self.assertEqual(self.db.job["status"], "failed")
        self.assertEqual(len(self.db.batches), 1)
        self.assertNotIn("lead_B", self.db.rows)

    def test_stale_worker_is_fenced_before_requesting_a_new_export(self):
        old = self.db.rpc("claim_close_lead_sync", {})
        self.db.job["lease_token"] = str(uuid4())
        with self.assertRaises(sync.LeaseLost): self.worker.process(old)
        self.assertEqual(self.close.creates, 0)
        self.assertEqual(self.db.rows, {})

    def test_stopping_preserves_checkpoint_for_restart(self):
        worker = sync.Worker(self.db, self.close, self.cache, stopping=lambda: True)
        worker.run_once()
        self.assertEqual(self.close.creates, 0)
        self.assertNotEqual(self.db.job["status"], "failed")

    def test_close_export_request_matches_supplied_flow_without_filter_or_done_email(self):
        close = sync.Close("secret")
        with patch.object(close.opener, "open", return_value=Response(b'{"id":"expo_Test"}')) as opened:
            self.assertEqual(close.create_export(), "expo_Test")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.close.com/api/v1/export/lead/")
        self.assertEqual(request.get_header("Authorization"), "Basic " + base64.b64encode(b"secret:").decode())
        self.assertEqual(json.loads(request.data), {"format": "csv", "type": "leads", "date_format": "iso8601", "send_done_email": False})

    def test_download_scopes_close_auth_and_decodes_gzip(self):
        close = sync.Close("secret")
        for url, authenticated in [("https://api.close.com/download", True), ("https://storage.example/export.csv", False)]:
            with self.subTest(url=url), patch.object(close.download_opener, "open", return_value=Response(gzip.compress(CSV))) as opened:
                destination = self.cache / "export.csv"
                close.download(url, destination, lambda **_: None, "test")
                self.assertEqual(destination.read_bytes(), CSV)
                request = opened.call_args.args[0]
                self.assertEqual("Authorization" in request.unredirected_hdrs, authenticated)
                self.assertNotIn("Authorization", request.headers)
                self.assertEqual(request.get_header("Accept-encoding"), "identity")
        redirected = sync.DownloadRedirects().redirect_request(Request("https://api.close.com/download", headers={"Authorization": "Basic secret"}), None, 302, "Found", {}, "https://storage.example/file")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(sync.SyncFailure):
            sync.DownloadRedirects().redirect_request(Request("https://api.close.com/download"), None, 302, "Found", {}, "http://storage.example/file")

    def test_rate_limit_retry_delay_and_errors_do_not_expose_credentials(self):
        close = sync.Close("secret-do-not-print")
        for code in [401, 403, 429, 500]:
            error = HTTPError("https://api.close.com/", code, "Secret body", {"Retry-After": "90"}, io.BytesIO(b"secret"))
            with self.subTest(code=code), patch.object(close.opener, "open", side_effect=error), self.assertRaises(sync.SyncFailure) as raised:
                close.create_export()
            self.assertNotIn("secret", str(raised.exception).lower())
            self.assertEqual(raised.exception.retryable, code in (429,500))
            if code == 429: self.assertEqual(raised.exception.delay, 90)

    def test_database_lost_response_and_lease_error_are_distinguished(self):
        db = sync.Database("https://project.supabase.co", "sb_secret_test")
        with patch.object(db.opener, "open", side_effect=TimeoutError()), self.assertRaises(sync.SyncFailure) as raised:
            db.rpc("import_close_lead_batch", {})
        self.assertTrue(raised.exception.retryable)
        error = HTTPError("https://project.supabase.co", 400, "Bad", {}, io.BytesIO(b'{"message":"Sync lease lost"}'))
        with patch.object(db.opener, "open", side_effect=error), self.assertRaises(sync.LeaseLost):
            db.rpc("import_close_lead_batch", {})
        self.assertNotIn("Authorization", db.headers)


if __name__ == "__main__": unittest.main()
