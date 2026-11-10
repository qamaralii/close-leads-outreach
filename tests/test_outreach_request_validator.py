"""Validation tests; import stubs avoid requiring a running Langflow installation."""

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


def load_component():
    class Data:
        def __init__(self, data):
            self.data = data

    modules = {name: types.ModuleType(name) for name in ("lfx", "lfx.custom", "lfx.io", "lfx.schema")}
    modules["lfx.custom"].Component = type("Component", (), {})
    for name in ("MessageTextInput", "MultilineInput", "Output"):
        setattr(modules["lfx.io"], name, lambda **kwargs: kwargs)
    modules["lfx.schema"].Data = Data
    path = Path(__file__).resolve().parents[1] / "langflow/components/outreach_request_validator.py"
    spec = importlib.util.spec_from_file_location("validator_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


validator = load_component()
USER_ID = "a5020e3d-94d3-4dfe-bfba-12a159f973e2"


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "request_id": "preview-001",
            "selected_lead_ids": ["lead_ABC", "lead_DEF"],
            "subject": " Hello {{ company }} ",
            "body": "Hi {{first_name}},\n\nCafé — your text.\n",
        }

    def validate(self, **changes):
        return validator.validate_outreach_request(json.dumps(self.request | changes), USER_ID)

    def test_valid_request_preserves_exact_content_and_trusted_identity(self):
        result = self.validate()
        self.assertEqual(result["user_id"], USER_ID)
        self.assertEqual(result["subject"], self.request["subject"])
        self.assertEqual(result["body"], self.request["body"])
        self.assertEqual(result["placeholders"], ["company", "first_name"])
        self.assertEqual(result["operation"], "prepare_preview")

    def test_identity_and_sender_overrides_are_rejected(self):
        for key in ("user_id", "entity_id", "to", "sender_connection_id", "campaign_id"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate(**{key: "override"})

    def test_composio_id_is_not_a_supabase_uuid(self):
        for user in ("pg-test-" + USER_ID, "", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(user=user), self.assertRaises(ValueError):
                validator.validate_outreach_request(json.dumps(self.request), user)

    def test_bad_lead_selections(self):
        for ids in ([], "lead_ABC", [None], [12], ["wrong"], ["lead_A", "lead_A"],
                    ["lead_A "] , [f"lead_{i}" for i in range(1001)]):
            with self.subTest(ids=str(ids)[:50]), self.assertRaises(ValueError):
                self.validate(selected_lead_ids=ids)

    def test_deferred_or_wrongly_typed_options(self):
        for changes in ({"is_html": True}, {"is_html": 0}, {"is_html": "false"},
                        {"send_mode": "schedule"}, {"send_mode": None},
                        {"scheduled_at": "2030-01-01T00:00:00Z"}, {"timezone": "Asia/Kolkata"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.validate(**changes)

    def test_unresolved_template_syntax_is_rejected(self):
        for body in ("Hi {{last_name}}", "Hi {{First_name}}", "Hi {{first_name", "Hi first_name}}",
                     "Hi {{{first_name}}}"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.validate(body=body)

    def test_invalid_text_and_limits(self):
        for changes in ({"body": " "}, {"body": False}, {"subject": "one\r\nBcc: two"},
                        {"body": "null\x00byte"}, {"subject": "x" * 999},
                        {"body": "x" * 100001}, {"request_id": " "}):
            with self.subTest(keys=list(changes)), self.assertRaises(ValueError):
                self.validate(**changes)

    def test_invalid_json_and_missing_fields(self):
        for raw in ('[]', 'null', '{', '{}', '{"request_id":"a","request_id":"b"}',
                    '{"x":NaN}', '"' + 'x' * 256001 + '"'):
            with self.subTest(raw=raw[:60]), self.assertRaises(ValueError):
                validator.validate_outreach_request(raw, USER_ID)

    def test_optional_personalization(self):
        self.assertEqual(self.validate(subject="Hello", body="Exact message.")["placeholders"], [])

    def test_component_returns_data(self):
        component = validator.OutreachRequestValidator()
        component.request_json = json.dumps(self.request)
        component.trusted_user_id = USER_ID
        self.assertEqual(component.validate_request().data["request_id"], "preview-001")
        self.assertIn("2 selected lead(s)", component.status)


if __name__ == "__main__":
    unittest.main()
