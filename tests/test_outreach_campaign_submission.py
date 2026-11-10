import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


def load_component():
    modules = {name: types.ModuleType(name) for name in ("lfx", "lfx.custom", "lfx.io", "lfx.schema")}
    modules["lfx.custom"].Component = type("Component", (), {})
    class Data:
        def __init__(self, data):
            self.data = data
    modules["lfx.schema"].Data = Data
    for name in ("DataInput", "Output", "SecretStrInput", "StrInput"):
        setattr(modules["lfx.io"], name, lambda **kwargs: kwargs)
    path = Path(__file__).resolve().parents[1] / "langflow/components/outreach_campaign_submission.py"
    spec = importlib.util.spec_from_file_location("submission_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


submission = load_component()
USER = "50b93869-6c3f-4344-bc9b-2a62ca2ba4b7"
CAMPAIGN = "1736f580-a789-436a-b636-0f9efcd6966f"
OTHER = "f113d6d8-c6e5-4a22-83ec-2dd605ed381b"


class FakeDatabase:
    def __init__(self):
        self.campaign = {"id": CAMPAIGN, "created_by": USER, "status": "draft",
                         "request_id": "preview-test-001", "preview_ready_at": "timestamp",
                         "submitted_at": None, "expected_recipient_count": 2}
        self.rows = [{"id": str(i), "campaign_id": CAMPAIGN, "status": "ready",
                      "due_at": None, "recipient_email": f"{i}@example.com",
                      "rendered_subject": "Hello", "rendered_body": "Hi,\n\nTest."} for i in range(2)]
        self.active = True
        self.calls = []
        self.writes = 0
        self.failure = None

    def select(self, table, params):
        self.calls.append((table, params))
        if table == "outreach_members":
            assert params["user_id"] == f"eq.{USER}"
            return [{"is_active": self.active}]
        if table == "campaigns":
            assert params["created_by"] == f"eq.{USER}"
            assert params["id"] == f"eq.{CAMPAIGN}"
            return [copy.deepcopy(self.campaign)] if self.campaign["created_by"] == USER else []
        if table == "campaign_recipients":
            assert params["campaign_id"] == f"eq.{CAMPAIGN}"
            offset = int(params["offset"])
            return copy.deepcopy(self.rows[offset:offset + 1])
        raise AssertionError(table)

    def submit(self, user_id, campaign_id):
        assert (user_id, campaign_id) == (USER, CAMPAIGN)
        if self.failure == "before":
            raise submission.DatabaseError("timeout", True)
        if self.failure == "rejected":
            raise submission.DatabaseError("Sender unavailable", False)
        if self.campaign["status"] == "draft":
            self.writes += 1
            self.campaign.update(status="submitted", submitted_at="submitted timestamp")
            for row in self.rows:
                row.update(status="queued", due_at="due timestamp")
        if self.failure == "after":
            raise submission.DatabaseError("timeout", True)
        return campaign_id


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.preview = {"operation": "persisted_preview", "user_id": USER,
                        "campaign_id": CAMPAIGN, "can_submit": True}

    def run_submit(self):
        return submission.submit_campaign(self.preview, USER, CAMPAIGN, self.db)

    def test_submit_and_paginated_reload(self):
        result = self.run_submit()
        self.assertEqual(result["recipient_status_counts"], {"queued": 2})
        self.assertEqual(result["recipients"][0]["rendered_body"], "Hi,\n\nTest.")
        self.assertFalse(result["already_submitted"])
        self.assertEqual(self.db.writes, 1)

    def test_confirmation_and_identity_fail_before_queries(self):
        for user, confirmation in ((USER, ""), (USER, OTHER), (OTHER, CAMPAIGN)):
            with self.subTest(user=user, confirmation=confirmation), self.assertRaises(ValueError):
                submission.submit_campaign(self.preview, user, confirmation, self.db)
        self.assertEqual(self.db.calls, [])

    def test_inactive_member_or_wrong_owner_blocks_submission(self):
        self.db.active = False
        with self.assertRaisesRegex(ValueError, "active"):
            self.run_submit()
        self.db.active = True
        self.db.campaign["created_by"] = OTHER
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.run_submit()
        self.assertEqual(self.db.writes, 0)

    def test_repeat_preserves_processed_recipient_states(self):
        self.run_submit()
        self.db.rows[0]["status"] = "sending"
        self.db.rows[1]["status"] = "unknown"
        self.preview["can_submit"] = False
        result = self.run_submit()
        self.assertTrue(result["already_submitted"])
        self.assertEqual(result["recipient_status_counts"], {"sending": 1, "unknown": 1})
        self.assertEqual(self.db.writes, 1)

    def test_timeout_after_commit_recovers(self):
        self.db.failure = "after"
        result = self.run_submit()
        self.assertTrue(result["recovered_after_error"])
        self.assertEqual(self.db.writes, 1)

    def test_timeout_before_commit_requires_same_id_retry(self):
        self.db.failure = "before"
        with self.assertRaisesRegex(ValueError, "SAME campaign ID"):
            self.run_submit()
        self.assertEqual(self.db.writes, 0)

    def test_rpc_rejection_is_not_reported_as_success(self):
        self.db.failure = "rejected"
        with self.assertRaisesRegex(submission.DatabaseError, "Sender unavailable"):
            self.run_submit()
        self.assertEqual(self.db.writes, 0)

    def test_incomplete_or_cancelled_campaign_blocks(self):
        self.db.campaign["preview_ready_at"] = None
        with self.assertRaises(ValueError):
            self.run_submit()
        self.db.campaign.update(preview_ready_at="timestamp", status="cancelled")
        with self.assertRaises(ValueError):
            self.run_submit()
        self.assertEqual(self.db.writes, 0)

    def test_missing_recipient_reload_reports_failure(self):
        self.db.rows.pop()
        with self.assertRaisesRegex(ValueError, "reload is incomplete"):
            self.run_submit()
        self.assertEqual(self.db.writes, 1)

    def test_component_data_output(self):
        component = submission.OutreachCampaignSubmission()
        component.persisted_preview = submission.Data(self.preview)
        component.trusted_user_id = USER
        component.confirmed_campaign_id = CAMPAIGN
        component.supabase_url = "https://example.supabase.co"
        component.supabase_key = "secret"
        with patch.object(submission, "SubmissionDatabase", return_value=self.db):
            result = component.submit_saved_campaign()
        self.assertEqual(result.data["operation"], "submitted_campaign")

    def test_http_rpc_contract_auth_and_sanitized_errors(self):
        for key in ("sb_secret_TEST", "legacy-test"):
            db = submission.SubmissionDatabase("https://example.supabase.co", key)
            with patch.object(db.opener, "open", return_value=io.BytesIO(json.dumps(CAMPAIGN).encode())) as mock:
                self.assertEqual(db.submit(USER, CAMPAIGN), CAMPAIGN)
                request = mock.call_args.args[0]
                self.assertEqual(request.get_method(), "POST")
                self.assertTrue(request.full_url.endswith("/rpc/submit_outreach_campaign"))
                self.assertEqual(json.loads(request.data), {"p_user_id": USER, "p_campaign_id": CAMPAIGN})
                headers = {k.lower(): v for k, v in request.header_items()}
                self.assertEqual("authorization" in headers, not key.startswith("sb_secret_"))
            for status in (400, 503):
                error = HTTPError("url", status, "private details", {}, io.BytesIO(b"secret"))
                with patch.object(db.opener, "open", side_effect=error):
                    with self.assertRaises(submission.DatabaseError) as caught:
                        db.submit(USER, CAMPAIGN)
                    self.assertEqual(caught.exception.recoverable, status >= 500)
                    self.assertNotIn("secret", str(caught.exception))

    def test_invalid_origins_and_redirects(self):
        for url in ("http://example.com", "https://example.com/rest/v1", "https://user:pass@example.com"):
            with self.assertRaises(ValueError):
                submission.SubmissionDatabase(url, "secret")
        self.assertIsNone(submission.NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.com"))


if __name__ == "__main__":
    unittest.main()
