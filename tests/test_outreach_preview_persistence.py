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
    modules["lfx.schema"].Data = type("Data", (), {})
    for name in ("DataInput", "Output", "SecretStrInput", "StrInput"):
        setattr(modules["lfx.io"], name, lambda **kwargs: kwargs)
    path = Path(__file__).resolve().parents[1] / "langflow/components/outreach_preview_persistence.py"
    spec = importlib.util.spec_from_file_location("persistence_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


persistence = load_component()
USER = "50b93869-6c3f-4344-bc9b-2a62ca2ba4b7"
CONNECTION = "64b3d50d-cb53-4d15-bb84-ffb006e86d9f"
CAMPAIGN = "f113d6d8-c6e5-4a22-83ec-2dd605ed381b"


def preview():
    return {
        "operation": "resolved_preview", "can_prepare": True, "user_id": USER,
        "request_id": "preview-001", "sender_connection_id": CONNECTION,
        "sender": {"mailbox_email": "sender@example.com", "composio_entity_id": "entity-A",
                   "composio_connected_account_id": "ca_A"},
        "subject_template": "Hello {{company}}", "body_template": "Hi {{first_name}},\n\nTest.",
        "is_html": False, "send_mode": "now", "errors": [], "excluded": [],
        "recipients": [{"close_lead_id": "lead_A", "recipient_email": "person@example.com",
                        "recipient_name": "Ada", "company_name": "Example",
                        "rendered_subject": "Hello Example", "rendered_body": "Hi Ada,\n\nTest."}],
        "default_value": "",
    }


class FakeDatabase:
    def __init__(self):
        self.campaign = None
        self.rows = []
        self.calls = []
        self.prepare_calls = 0
        self.failure = None
        self.active = True
        self.suppressed = False
        self.sender = preview()["sender"]

    def select(self, table, params):
        self.calls.append((table, params))
        if table == "outreach_members":
            return [{"is_active": self.active}]
        if table == "outlook_connections":
            return [{"id": CONNECTION, "user_id": USER, "status": "active", **self.sender}]
        if table == "email_suppressions":
            return [{"normalized_email": "person@example.com"}] if self.suppressed else []
        if table == "campaigns":
            assert params["created_by"] == f"eq.{USER}"
            return [copy.deepcopy(self.campaign)] if self.campaign else []
        if table == "campaign_recipients":
            assert params["campaign_id"] == f"eq.{CAMPAIGN}"
            offset = int(params["offset"])
            return copy.deepcopy(self.rows[offset:offset + 1])
        raise AssertionError(table)

    def prepare(self, payload):
        self.prepare_calls += 1
        self.payload = payload
        if self.failure == "before":
            raise persistence.DatabaseError("timeout", True)
        self.campaign = {
            "id": CAMPAIGN, "created_by": payload["p_user_id"], "request_id": payload["p_request_id"],
            "sender_connection_id": payload["p_sender_connection_id"],
            "sender_mailbox_snapshot": self.sender["mailbox_email"],
            "sender_entity_snapshot": self.sender["composio_entity_id"],
            "sender_account_snapshot": self.sender["composio_connected_account_id"],
            "subject_template": payload["p_subject_template"], "body_template": payload["p_body_template"],
            "is_html": False, "send_mode": "now", "expected_recipient_count": len(payload["p_recipients"]),
            "preview_ready_at": "2026-11-10T12:00:00Z", "status": "draft",
        }
        self.rows = [{**r, "id": f"recipient-{i}", "status": "ready", "campaign_id": CAMPAIGN}
                     for i, r in enumerate(payload["p_recipients"])]
        if self.failure in ("after", "duplicate"):
            raise persistence.DatabaseError("timeout or concurrent duplicate", True)
        return CAMPAIGN


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.preview = preview()
        self.db = FakeDatabase()

    def test_save_reload_and_rpc_contract(self):
        result = persistence.persist_preview(self.preview, self.db)
        self.assertEqual(result["campaign_id"], CAMPAIGN)
        self.assertTrue(result["can_submit"])
        self.assertFalse(result["reused_existing"])
        self.assertEqual(result["recipients"][0]["rendered_body"], "Hi Ada,\n\nTest.")
        self.assertEqual(set(self.db.payload), {"p_user_id", "p_request_id", "p_sender_connection_id",
                                              "p_subject_template", "p_body_template", "p_is_html", "p_recipients"})

    def test_identical_retry_does_not_write(self):
        persistence.persist_preview(self.preview, self.db)
        result = persistence.persist_preview(self.preview, self.db)
        self.assertTrue(result["reused_existing"])
        self.assertEqual(self.db.prepare_calls, 1)

    def test_conflicting_content_is_rejected(self):
        for change in ("template", "message", "sender"):
            with self.subTest(change=change):
                db = FakeDatabase()
                original = preview()
                persistence.persist_preview(original, db)
                if change == "template":
                    original["subject_template"] = "Changed"
                elif change == "message":
                    original["recipients"][0]["rendered_body"] = "Changed"
                else:
                    original["sender"]["mailbox_email"] = "other@example.com"
                with self.assertRaisesRegex(ValueError, "conflicts"):
                    persistence.persist_preview(original, db)
                self.assertEqual(db.prepare_calls, 1)

    def test_committed_timeout_or_concurrent_insert_recovers(self):
        for failure in ("after", "duplicate"):
            with self.subTest(failure=failure):
                db = FakeDatabase()
                db.failure = failure
                result = persistence.persist_preview(self.preview, db)
                self.assertTrue(result["reused_existing"])
                self.assertEqual(db.prepare_calls, 1)

    def test_unconfirmed_timeout_does_not_retry_write(self):
        self.db.failure = "before"
        with self.assertRaisesRegex(ValueError, "SAME request ID"):
            persistence.persist_preview(self.preview, self.db)
        self.assertEqual(self.db.prepare_calls, 1)

    def test_blocked_and_empty_inputs_do_not_query(self):
        for changes in ({"can_prepare": False}, {"errors": [{"reason": "missing"}]}, {"recipients": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                persistence.persist_preview(self.preview | changes, self.db)
        self.assertEqual(self.db.calls, [])

    def test_membership_sender_and_suppression_checks(self):
        for kind in ("membership", "sender", "suppression"):
            db = FakeDatabase()
            if kind == "membership":
                db.active = False
            elif kind == "sender":
                db.sender["mailbox_email"] = "changed@example.com"
            else:
                db.suppressed = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                persistence.persist_preview(self.preview, db)
            self.assertEqual(db.prepare_calls, 0)

    def test_submitted_campaign_reloads_without_enabling_resubmission(self):
        persistence.persist_preview(self.preview, self.db)
        self.db.campaign["status"] = "submitted"
        self.db.rows[0]["status"] = "submitted"
        result = persistence.persist_preview(self.preview, self.db)
        self.assertFalse(result["can_submit"])
        self.assertEqual(self.db.prepare_calls, 1)

    def test_reloads_all_pages_and_detects_missing_rows(self):
        second = copy.deepcopy(self.preview["recipients"][0])
        second.update(close_lead_id="lead_B", recipient_email="b@example.com")
        self.preview["recipients"].append(second)
        result = persistence.persist_preview(self.preview, self.db)
        self.assertEqual(result["recipient_count"], 2)
        self.db.rows.pop()
        with self.assertRaisesRegex(ValueError, "stored recipients"):
            persistence.persist_preview(self.preview, self.db)

    def test_duplicate_recipient_rejected(self):
        self.preview["recipients"] *= 2
        with self.assertRaisesRegex(ValueError, "duplicate recipients"):
            persistence.persist_preview(self.preview, self.db)
        self.assertEqual(self.db.prepare_calls, 0)

    def test_http_post_auth_and_unique_violation_classification(self):
        for key in ("sb_secret_TEST", "legacy-test"):
            db = persistence.PreviewDatabase("https://example.supabase.co", key)
            with patch.object(db.opener, "open", return_value=io.BytesIO(json.dumps(CAMPAIGN).encode())) as mock:
                self.assertEqual(db.prepare({"p_user_id": USER}), CAMPAIGN)
                req = mock.call_args.args[0]
                self.assertEqual(req.get_method(), "POST")
                self.assertTrue(req.full_url.endswith("/rpc/prepare_outreach_preview"))
                headers = {k.lower(): v for k, v in req.header_items()}
                self.assertEqual("authorization" in headers, not key.startswith("sb_secret_"))
            error = HTTPError("url", 409, "conflict", {}, io.BytesIO(b'{"code":"23505"}'))
            with patch.object(db.opener, "open", side_effect=error):
                with self.assertRaises(persistence.DatabaseError) as caught:
                    db.prepare({})
                self.assertTrue(caught.exception.recoverable)


if __name__ == "__main__":
    unittest.main()
