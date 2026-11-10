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
    path = Path(__file__).resolve().parents[1] / "langflow/components/outreach_contact_resolver.py"
    spec = importlib.util.spec_from_file_location("resolver_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


resolver = load_component()
USER = "50b93869-6c3f-4344-bc9b-2a62ca2ba4b7"


class FakeReader:
    def __init__(self, leads, suppressions=()):
        self.leads = leads
        self.suppressions = suppressions
        self.active = True
        self.connection_active = True
        self.calls = []

    def select(self, table, params):
        self.calls.append((table, params))
        if table == "outreach_members":
            return [{"user_id": USER, "is_active": self.active}]
        if table == "outlook_connections":
            return [{"id": "sender-id", "user_id": USER, "status": "active" if self.connection_active else "disconnected",
                     "mailbox_email": "sender@example.com", "composio_entity_id": "entity-A",
                     "composio_connected_account_id": "ca_A"}]
        key = "close_lead_id" if table == "close_leads" else "normalized_email"
        values = json.loads("[" + params[key][4:-1] + "]")
        rows = self.leads if table == "close_leads" else [{key: email} for email in self.suppressions]
        rows = sorted((r for r in rows if r[key] in values), key=lambda r: r[key])
        offset = int(params.get("offset", 0))
        # Simulate a server row cap lower than the requested page size.
        return rows[offset:offset + 1]


def lead(id="lead_A", email="person@example.com", first="Ada", company="Example Co"):
    return {"close_lead_id": id, "email": email, "first_name": first,
            "company_name": company, "contact_name": "Ada Test"}


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.request = {"operation": "prepare_preview", "user_id": USER, "request_id": "test-001",
                        "selected_lead_ids": ["lead_A"], "subject": "Hello {{company}}",
                        "body": "Hi {{ first_name }},\n\nAn exact message.\n", "is_html": False,
                        "send_mode": "now", "scheduled_at": None, "timezone": None}

    def test_personalization_and_line_breaks(self):
        output = resolver.resolve_contacts(self.request, FakeReader([lead()]))
        self.assertTrue(output["can_prepare"])
        self.assertEqual(output["recipients"][0]["rendered_body"], "Hi Ada,\n\nAn exact message.\n")
        self.assertEqual(output["recipients"][0]["rendered_subject"], "Hello Example Co")
        self.assertEqual(output["sender"]["composio_entity_id"], "entity-A")

    def test_pagination_deduplication_and_exclusions(self):
        self.request["selected_lead_ids"] = ["lead_B", "lead_A", "lead_C", "lead_D", "lead_E"]
        output = resolver.resolve_contacts(self.request, FakeReader([
            lead(), lead("lead_B", " PERSON@example.com "), lead("lead_C", "blocked@example.com"),
            lead("lead_D", "two@example.com,other@example.com")
        ], ["blocked@example.com"]))
        self.assertTrue(output["can_prepare"])
        self.assertEqual(output["recipients"][0]["close_lead_id"], "lead_B")
        self.assertEqual([x["reason"] for x in output["excluded"]],
                         ["duplicate_email", "suppressed", "missing_or_invalid_email", "lead_not_found"])
        self.assertEqual(output["counts"], {"selected": 5, "eligible": 1, "excluded": 4, "blocked": 0})

    def test_missing_personalization_blocks_entire_preview(self):
        self.request["selected_lead_ids"] += ["lead_B"]
        output = resolver.resolve_contacts(self.request, FakeReader([lead(), lead("lead_B", "b@example.com", first=None)]))
        self.assertFalse(output["can_prepare"])
        self.assertEqual(output["errors"][0]["fields"], ["first_name"])
        self.assertEqual(len(output["recipients"]), 1)

    def test_subject_injection_and_nested_placeholders_block(self):
        for company in ("Example\r\nBcc: other@example.com", "{{first_name}}", "X" * 1000):
            with self.subTest(company=company[:40]):
                output = resolver.resolve_contacts(self.request, FakeReader([lead(company=company)]))
                self.assertFalse(output["can_prepare"])
                self.assertEqual(output["errors"][0]["reason"], "invalid_rendered_content")

    def test_inactive_members_or_connections_fail_before_lead_reads(self):
        for attribute in ("active", "connection_active"):
            reader = FakeReader([lead()])
            setattr(reader, attribute, False)
            with self.assertRaises(ValueError):
                resolver.resolve_contacts(self.request, reader)
            self.assertNotIn("close_leads", [table for table, _ in reader.calls])

    def test_no_eligible_recipients_blocks(self):
        output = resolver.resolve_contacts(self.request, FakeReader([lead()], ["person@example.com"]))
        self.assertFalse(output["can_prepare"])
        self.assertEqual(output["recipients"], [])

    def test_plain_message_does_not_require_first_name(self):
        self.request.update(subject="Hello", body="Exact copy.")
        output = resolver.resolve_contacts(self.request, FakeReader([lead(first=None, company=None)]))
        self.assertTrue(output["can_prepare"])

    def test_email_validator_rejects_lists_and_malformed_addresses(self):
        for value in (None, "", "a..b@example.com", "x@example", "x@-example.com", "Name <x@example.com>",
                      "x@example.com\r\nBcc: y@example.com", "ü@example.com"):
            with self.subTest(value=value):
                self.assertFalse(resolver.valid_email(value))
        self.assertTrue(resolver.valid_email("person+tag@example.co.uk"))

    def test_url_validation(self):
        for url in ("http://example.com", "https://example.com/rest/v1", "https://key@example.com", "https://example.com?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                resolver.SupabaseReader(url, "test-key")

    def test_key_header_modes_and_sanitized_http_errors(self):
        for key in ("sb_secret_TEST", "legacy-test-key"):
            reader = resolver.SupabaseReader("https://project.supabase.co", key)
            with patch.object(reader.opener, "open", return_value=io.BytesIO(b"[]")) as mock:
                self.assertEqual(reader.select("close_leads", {"select": "close_lead_id"}), [])
                headers = {k.lower(): v for k, v in mock.call_args.args[0].header_items()}
                self.assertEqual(headers["apikey"], key)
                self.assertEqual("authorization" in headers, not key.startswith("sb_secret_"))
            with patch.object(reader.opener, "open", side_effect=HTTPError("url", 401, key, {}, None)):
                with self.assertRaises(ValueError) as context:
                    reader.select("close_leads", {})
                self.assertNotIn(key, str(context.exception))


if __name__ == "__main__":
    unittest.main()
