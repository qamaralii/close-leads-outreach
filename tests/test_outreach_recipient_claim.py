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
    modules = {n: types.ModuleType(n) for n in ("lfx", "lfx.custom", "lfx.io", "lfx.schema")}
    modules["lfx.custom"].Component = type("Component", (), {})
    class Data:
        def __init__(self, data):
            self.data = data
    modules["lfx.schema"].Data = Data
    for name in ("DataInput", "Output", "SecretStrInput", "StrInput"):
        setattr(modules["lfx.io"], name, lambda **kwargs: kwargs)
    path = Path(__file__).resolve().parents[1] / "langflow/components/outreach_recipient_claim.py"
    spec = importlib.util.spec_from_file_location("claim_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


claim = load_component()
USER = "50b93869-6c3f-4344-bc9b-2a62ca2ba4b7"
CAMPAIGN = "1736f580-a789-436a-b636-0f9efcd6966f"
RECIPIENT = "266877cd-44cf-4c73-bab8-8165c043ac3c"
OTHER = "f113d6d8-c6e5-4a22-83ec-2dd605ed381b"


class FakeDatabase:
    def __init__(self):
        self.calls = []
        self.rows = [{"recipient_id": RECIPIENT, "attempt_id": OTHER, "claim_token": USER,
                      "entity_id": "entity-test", "connected_account_id": "ca_test",
                      "sender_email": "sender@example.com", "recipient_email": "person@example.com",
                      "subject": "Saved subject", "body": "Saved body\n\nLine 2", "is_html": False}]

    def claim(self, user, campaign, recipient):
        self.calls.append((user, campaign, recipient))
        result = copy.deepcopy(self.rows)
        self.rows = []
        return result


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.input = {"operation": "submitted_campaign", "user_id": USER, "campaign_id": CAMPAIGN,
                      "recipients": [{"id": RECIPIENT, "campaign_id": CAMPAIGN, "status": "queued"}]}

    def run_claim(self, selected=""):
        return claim.claim_recipient(self.input, USER, selected, self.db)

    def test_single_auto_selection_uses_rpc_message(self):
        self.input["recipients"][0]["rendered_body"] = "Untrusted/stale body"
        result = self.run_claim()
        self.assertTrue(result["claimed"])
        self.assertEqual(result["body"], "Saved body\n\nLine 2")
        self.assertEqual(result["attempt_id"], OTHER)
        self.assertEqual(self.db.calls, [(USER, CAMPAIGN, RECIPIENT)])

    def test_repeat_returns_no_send_payload(self):
        self.run_claim()
        result = self.run_claim()
        self.assertFalse(result["claimed"])
        for key in ("body", "subject", "attempt_id", "claim_token", "recipient_email"):
            self.assertNotIn(key, result)

    def test_empty_rpc_result_does_not_guess_reason(self):
        self.db.rows = []
        result = self.run_claim()
        self.assertEqual(result["reason"], "not_claimable")
        self.assertFalse(result["claimed"])

    def test_multiple_recipients_require_selection(self):
        self.input["recipients"].append({"id": OTHER, "campaign_id": CAMPAIGN})
        with self.assertRaisesRegex(ValueError, "select one"):
            self.run_claim()
        self.assertEqual(self.db.calls, [])
        self.assertTrue(self.run_claim(RECIPIENT)["claimed"])

    def test_wrong_owner_or_recipient_blocks_before_rpc(self):
        with self.assertRaises(ValueError):
            claim.claim_recipient(self.input, OTHER, "", self.db)
        with self.assertRaises(ValueError):
            self.run_claim(OTHER)
        self.input["recipients"][0]["campaign_id"] = OTHER
        with self.assertRaises(ValueError):
            self.run_claim()
        self.assertEqual(self.db.calls, [])

    def test_invalid_success_payload_fails_closed_without_retry(self):
        for changes in ({"recipient_id": OTHER}, {"claim_token": "bad"}, {"body": ""}, {"is_html": True}):
            with self.subTest(changes=changes):
                self.db = FakeDatabase()
                self.db.rows[0].update(changes)
                with self.assertRaisesRegex(ValueError, "Do not send"):
                    self.run_claim()
                self.assertEqual(len(self.db.calls), 1)

    def test_component_output(self):
        node = claim.OutreachRecipientClaim()
        node.submitted_campaign = claim.Data(self.input)
        node.trusted_user_id = USER
        node.recipient_id = ""
        node.supabase_url = "https://example.supabase.co"
        node.supabase_key = "secret"
        with patch.object(claim, "ClaimDatabase", return_value=self.db):
            self.assertTrue(node.claim_saved_recipient().data["claimed"])

    def test_http_contract_and_key_modes(self):
        for key in ("sb_secret_test", "legacy-test"):
            db = claim.ClaimDatabase("https://example.supabase.co", key)
            with patch.object(db.opener, "open", return_value=io.BytesIO(b"[]")) as mock:
                self.assertEqual(db.claim(USER, CAMPAIGN, RECIPIENT), [])
                req = mock.call_args.args[0]
                self.assertEqual(req.get_method(), "POST")
                self.assertTrue(req.full_url.endswith("/rpc/claim_outreach_recipient"))
                self.assertEqual(json.loads(req.data), {"p_user_id": USER, "p_campaign_id": CAMPAIGN,
                                                       "p_recipient_id": RECIPIENT})
                headers = {k.lower(): v for k, v in req.header_items()}
                self.assertEqual("authorization" in headers, not key.startswith("sb_secret_"))

    def test_network_and_http_errors_never_retry_or_expose_details(self):
        for error in (TimeoutError(), HTTPError("url", 503, "secret", {}, None),
                      HTTPError("url", 400, "secret", {}, None)):
            db = claim.ClaimDatabase("https://example.supabase.co", "secret")
            with patch.object(db.opener, "open", side_effect=error) as mock:
                with self.assertRaises(ValueError) as caught:
                    db.claim(USER, CAMPAIGN, RECIPIENT)
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(mock.call_count, 1)

    def test_invalid_json_does_not_retry(self):
        db = claim.ClaimDatabase("https://example.supabase.co", "secret")
        with patch.object(db.opener, "open", return_value=io.BytesIO(b"invalid")) as mock:
            with self.assertRaisesRegex(ValueError, "Do not retry"):
                db.claim(USER, CAMPAIGN, RECIPIENT)
            self.assertEqual(mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
