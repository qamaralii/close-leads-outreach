"""Standalone Langflow 1.13.1 component: persist previews, never send email."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from lfx.custom import Component
from lfx.io import DataInput, Output, SecretStrInput, StrInput
from lfx.schema import Data


class DatabaseError(ValueError):
    def __init__(self, message, recoverable=False):
        super().__init__(message)
        self.recoverable = recoverable


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PreviewDatabase:
    def __init__(self, url, key):
        url = str(url).strip()
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Supabase Project URL must be an HTTPS origin without /rest/v1.")
        self.url = url.rstrip("/")
        self.key = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or "")
        if not self.key.strip():
            raise ValueError("Supabase Backend Key is required.")
        self.opener = build_opener(NoRedirects())

    def call(self, path, params=None, payload=None):
        headers = {"apikey": self.key, "Accept": "application/json"}
        if not self.key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {self.key}"
        data = None
        if payload is not None:
            data = json.dumps(payload, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = f"{self.url}/rest/v1/{path}"
        if params:
            url += "?" + urlencode(params)
        try:
            with self.opener.open(Request(url, data=data, headers=headers), timeout=120) as response:
                return json.load(response)
        except HTTPError as exc:
            code = None
            try:
                details = json.loads(exc.read(8192))
                code = details.get("code") if isinstance(details, dict) else None
            except (ValueError, OSError):
                pass
            raise DatabaseError(
                f"Supabase request failed (HTTP {exc.code}).",
                recoverable=code == "23505" or exc.code >= 500,
            ) from None
        except (URLError, TimeoutError, OSError):
            raise DatabaseError("Supabase network/timeout error; retain the same request ID.", True) from None
        except (ValueError, UnicodeError):
            raise DatabaseError("Supabase returned invalid JSON; retain the same request ID.", True) from None

    def select(self, table, params):
        rows = self.call(table, params=params)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DatabaseError("Unexpected Supabase table response.")
        return rows

    def prepare(self, payload):
        return self.call("rpc/prepare_outreach_preview", payload=payload)


RECIPIENT_FIELDS = (
    "close_lead_id", "recipient_email", "recipient_name", "company_name",
    "rendered_subject", "rendered_body",
)


def uuid_value(value, label):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{label} must be a UUID.") from None


def persist_preview(preview, database):
    if not isinstance(preview, dict) or preview.get("operation") != "resolved_preview":
        raise ValueError("Connect the resolver's Resolved Preview output.")
    if preview.get("can_prepare") is not True or preview.get("errors") != []:
        raise ValueError("Preview is blocked. Resolve all personalization errors before saving.")
    user_id = uuid_value(preview.get("user_id"), "User ID")
    connection_id = uuid_value(preview.get("sender_connection_id"), "Sender connection ID")
    if preview.get("is_html") is not False or preview.get("send_mode") != "now":
        raise ValueError("Only immediate plain-text previews are supported.")
    for field in ("request_id", "subject_template", "body_template"):
        if not isinstance(preview.get(field), str) or not preview[field].strip():
            raise ValueError(f"Resolved preview is missing {field}.")
    sender = preview.get("sender")
    sender_fields = ("mailbox_email", "composio_entity_id", "composio_connected_account_id")
    if not isinstance(sender, dict) or any(not isinstance(sender.get(k), str) or not sender[k].strip() for k in sender_fields):
        raise ValueError("Resolved preview has incomplete sender metadata.")
    source_recipients = preview.get("recipients")
    if not isinstance(source_recipients, list) or not 1 <= len(source_recipients) <= 1000:
        raise ValueError("Preview requires between 1 and 1000 eligible recipients.")
    recipients = []
    for row in source_recipients:
        if not isinstance(row, dict):
            raise ValueError("Invalid preview recipient.")
        item = {key: row.get(key) for key in RECIPIENT_FIELDS}
        for key in ("close_lead_id", "recipient_email", "rendered_subject", "rendered_body"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise ValueError(f"Recipient is missing {key}.")
        for key in ("recipient_name", "company_name"):
            if item[key] is not None and not isinstance(item[key], str):
                raise ValueError(f"Recipient {key} must be text or null.")
        if (len(item["rendered_subject"]) > 998 or len(item["rendered_body"]) > 100_000
                or any(c in item["rendered_subject"] for c in "\r\n\t")
                or any("{{" in item[k] or "}}" in item[k] for k in ("rendered_subject", "rendered_body"))):
            raise ValueError("Invalid rendered message; rerun the resolver.")
        recipients.append(item)
    if (len({r["close_lead_id"] for r in recipients}) != len(recipients)
            or len({r["recipient_email"].strip().lower() for r in recipients}) != len(recipients)):
        raise ValueError("Resolved preview contains duplicate recipients.")

    # Even the existing-request recovery path requires current app membership.
    members = database.select("outreach_members", {
        "select": "user_id,is_active", "user_id": f"eq.{user_id}", "limit": "2"
    })
    if len(members) != 1 or members[0].get("is_active") is not True:
        raise ValueError("User is not an active outreach member.")

    def find_campaign():
        rows = database.select("campaigns", {
            "select": "*", "created_by": f"eq.{user_id}",
            "request_id": f"eq.{preview['request_id']}", "limit": "2",
        })
        if len(rows) > 1:
            raise ValueError("Multiple campaigns found for the request ID.")
        return rows[0] if rows else None

    campaign = find_campaign()
    reused = campaign is not None
    if campaign is None:
        connections = database.select("outlook_connections", {
            "select": "id,user_id,status,mailbox_email,composio_entity_id,composio_connected_account_id",
            "id": f"eq.{connection_id}", "user_id": f"eq.{user_id}", "limit": "2",
        })
        if (len(connections) != 1 or connections[0].get("status") != "active"
                or any(connections[0].get(k) != sender[k] for k in sender_fields)):
            raise ValueError("Sender mapping changed; rerun resolution with a new request ID.")
        # Fail closed if suppression changed since resolution. The send stage must recheck again.
        for start in range(0, len(recipients), 50):
            emails = [r["recipient_email"].strip().lower() for r in recipients[start:start + 50]]
            suppressed = database.select("email_suppressions", {
                "select": "normalized_email", "limit": "1",
                "normalized_email": "in.(" + ",".join(json.dumps(email) for email in emails) + ")",
            })
            if suppressed:
                raise ValueError("A recipient is now suppressed; rerun resolution with a new request ID.")
        payload = {
            "p_user_id": user_id, "p_request_id": preview["request_id"],
            "p_sender_connection_id": connection_id,
            "p_subject_template": preview["subject_template"], "p_body_template": preview["body_template"],
            "p_is_html": False, "p_recipients": recipients,
        }
        try:
            returned_id = database.prepare(payload)
        except DatabaseError as exc:
            if not exc.recoverable:
                raise
            # A duplicate request or timeout may mean the transaction already committed.
            campaign = find_campaign()
            if campaign is None:
                raise ValueError(
                    "Preview save could not be confirmed. Retry with the SAME request ID; do not create a new one."
                ) from None
            reused = True
        else:
            campaign = find_campaign()
            if campaign is None or campaign.get("id") != returned_id:
                raise ValueError("Saved preview could not be confirmed. Retry with the same request ID.")

    expected = {
        "created_by": user_id, "request_id": preview["request_id"], "sender_connection_id": connection_id,
        "sender_mailbox_snapshot": sender["mailbox_email"],
        "sender_entity_snapshot": sender["composio_entity_id"],
        "sender_account_snapshot": sender["composio_connected_account_id"],
        "subject_template": preview["subject_template"], "body_template": preview["body_template"],
        "is_html": False, "send_mode": "now", "expected_recipient_count": len(recipients),
    }
    if any(campaign.get(k) != value for k, value in expected.items()):
        raise ValueError("Request ID conflicts with stored preview content. Use a new request ID for edits.")
    if not campaign.get("preview_ready_at"):
        raise ValueError("Stored preview is incomplete; do not submit it.")
    campaign_id = uuid_value(campaign.get("id"), "Stored campaign ID")
    saved = []
    while True:
        page = database.select("campaign_recipients", {
            "select": "id,campaign_id,status," + ",".join(RECIPIENT_FIELDS),
            "campaign_id": f"eq.{campaign_id}", "order": "id.asc",
            "limit": "50", "offset": str(len(saved)),
        })
        if not page:
            break
        saved.extend(page)
        if len(saved) > len(recipients):
            raise ValueError("Stored preview recipient count differs from the resolved request.")
    def canonical(rows):
        return sorted(json.dumps({k: r.get(k) for k in RECIPIENT_FIELDS}, sort_keys=True) for r in rows)
    if len(saved) != len(recipients) or canonical(saved) != canonical(recipients):
        raise ValueError("Request ID conflicts with stored recipients/messages. Use a new request ID for edits.")
    return {
        "operation": "persisted_preview", "campaign_id": campaign_id,
        "user_id": user_id, "request_id": preview["request_id"], "reused_existing": reused,
        "campaign_status": campaign["status"], "preview_ready_at": campaign["preview_ready_at"],
        "can_submit": campaign["status"] == "draft" and all(r["status"] == "ready" for r in saved),
        "sender": {"mailbox_email": campaign["sender_mailbox_snapshot"],
                   "composio_entity_id": campaign["sender_entity_snapshot"],
                   "composio_connected_account_id": campaign["sender_account_snapshot"]},
        "recipients": saved,
        # Exclusion reports are informational resolver output, not persisted campaign records.
        "resolution_exclusions": preview.get("excluded", []),
        "recipient_count": len(saved),
    }


class OutreachPreviewPersistence(Component):
    display_name = "Persist Outreach Preview"
    description = "Atomically save and reload exact reviewed messages without submitting or sending."
    icon = "Save"
    name = "OutreachPreviewPersistence"
    inputs = [
        DataInput(name="resolved_preview", display_name="Resolved Preview", required=True),
        StrInput(name="supabase_url", display_name="Supabase Project URL", required=True),
        SecretStrInput(name="supabase_key", display_name="Supabase Backend Key", required=True),
    ]
    outputs = [Output(name="persisted_preview", display_name="Persisted Preview", method="save_preview")]

    def save_preview(self) -> Data:
        preview = self.resolved_preview
        if isinstance(preview, Data):
            preview = preview.data
        result = persist_preview(preview, PreviewDatabase(self.supabase_url, self.supabase_key))
        self.status = f"Preview stored: {result['recipient_count']} recipient(s); reused={result['reused_existing']}"
        return Data(data=result)
