"""Standalone Langflow 1.13.1 component: explicitly submit a saved campaign."""

import json
from collections import Counter
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


class SubmissionDatabase:
    def __init__(self, url, key):
        parsed = urlsplit(str(url).strip())
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Supabase Project URL must be an HTTPS origin without /rest/v1.")
        self.url = str(url).strip().rstrip("/")
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
            raise DatabaseError(f"Supabase request failed (HTTP {exc.code}).", exc.code >= 500) from None
        except (URLError, TimeoutError, OSError):
            raise DatabaseError("Supabase network/timeout error. Retry with the same campaign ID.", True) from None
        except (ValueError, UnicodeError):
            raise DatabaseError("Supabase returned invalid JSON. Retry with the same campaign ID.", True) from None

    def select(self, table, params):
        rows = self.call(table, params=params)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise DatabaseError("Unexpected Supabase table response.")
        return rows

    def submit(self, user_id, campaign_id):
        return self.call("rpc/submit_outreach_campaign", payload={
            "p_user_id": user_id, "p_campaign_id": campaign_id,
        })


def uuid_value(value, label):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{label} must be a UUID.") from None


def submit_campaign(preview, trusted_user_id, confirmed_campaign_id, database):
    if not isinstance(preview, dict) or preview.get("operation") != "persisted_preview":
        raise ValueError("Connect the Persist Outreach Preview output.")
    user_id = uuid_value(trusted_user_id, "Trusted User ID")
    campaign_id = uuid_value(preview.get("campaign_id"), "Campaign ID")
    if uuid_value(preview.get("user_id"), "Preview user ID") != user_id:
        raise ValueError("Preview owner differs from the trusted user.")
    if not confirmed_campaign_id:
        raise ValueError("Review the saved preview, then enter its ID in Confirmed Campaign ID.")
    if uuid_value(confirmed_campaign_id, "Confirmed Campaign ID") != campaign_id:
        raise ValueError("Confirmed Campaign ID does not match the saved preview.")

    def require_membership():
        members = database.select("outreach_members", {
            "select": "user_id,is_active", "user_id": f"eq.{user_id}", "limit": "2",
        })
        if len(members) != 1 or members[0].get("is_active") is not True:
            raise ValueError("User is not an active outreach member.")

    def load_campaign():
        rows = database.select("campaigns", {
            "select": "*", "id": f"eq.{campaign_id}", "created_by": f"eq.{user_id}", "limit": "2",
        })
        if len(rows) != 1:
            raise ValueError("Campaign unavailable for the trusted user.")
        return rows[0]

    require_membership()
    before = load_campaign()
    if before.get("status") not in ("draft", "submitted") or not before.get("preview_ready_at"):
        raise ValueError("Campaign is not a finalized draft or an existing submission.")
    recovered = False
    # Use database state, not the input's possibly stale can_submit or recipient statuses.
    # The RPC locks the campaign, validates owner/sender, and makes repeated submission a no-op.
    try:
        returned_id = database.submit(user_id, campaign_id)
    except DatabaseError as exc:
        if not exc.recoverable:
            raise
        require_membership()
        campaign = load_campaign()
        if campaign.get("status") != "submitted":
            raise ValueError("Submission could not be confirmed. Retry with the SAME campaign ID.") from None
        recovered = True
    else:
        if returned_id != campaign_id:
            raise ValueError("Unexpected submission response. Retry with the same campaign ID.")
        campaign = load_campaign()
    if campaign.get("status") != "submitted" or not campaign.get("submitted_at"):
        raise ValueError("Submission could not be confirmed. Retry with the same campaign ID.")
    expected = campaign.get("expected_recipient_count")
    if type(expected) is not int or not 1 <= expected <= 1000:
        raise ValueError("Unexpected saved recipient count.")
    recipients = []
    while True:
        page = database.select("campaign_recipients", {
            "select": "id,campaign_id,status,due_at,recipient_email,rendered_subject,rendered_body",
            "campaign_id": f"eq.{campaign_id}", "order": "id.asc",
            "limit": "50", "offset": str(len(recipients)),
        })
        if not page:
            break
        recipients.extend(page)
        if len(recipients) > expected:
            raise ValueError("Unexpected stored recipient count after submission.")
    if len(recipients) != expected or len({row["id"] for row in recipients}) != expected:
        raise ValueError("Submitted recipient reload is incomplete. Retry with the same campaign ID.")
    return {
        "operation": "submitted_campaign", "user_id": user_id, "campaign_id": campaign_id,
        "request_id": campaign["request_id"], "campaign_status": campaign["status"],
        "submitted_at": campaign["submitted_at"], "already_submitted": before["status"] == "submitted",
        "recovered_after_error": recovered, "recipient_count": len(recipients),
        "recipient_status_counts": dict(Counter(row["status"] for row in recipients)),
        "recipients": recipients,
    }


class OutreachCampaignSubmission(Component):
    display_name = "Submit Outreach Campaign"
    description = "Explicitly queue a saved campaign through the atomic submission RPC."
    icon = "CheckCheck"
    name = "OutreachCampaignSubmission"
    inputs = [
        DataInput(name="persisted_preview", display_name="Persisted Preview", required=True),
        StrInput(name="trusted_user_id", display_name="Trusted User ID", required=True,
                 info="Production: supplied by the backend from a verified Supabase session."),
        StrInput(name="confirmed_campaign_id", display_name="Confirmed Campaign ID", value="",
                 info="After reviewing the saved preview, enter its campaign ID to explicitly submit it."),
        StrInput(name="supabase_url", display_name="Supabase Project URL", required=True),
        SecretStrInput(name="supabase_key", display_name="Supabase Backend Key", required=True),
    ]
    outputs = [Output(name="submitted_campaign", display_name="Submitted Campaign", method="submit_saved_campaign")]

    def submit_saved_campaign(self) -> Data:
        preview = self.persisted_preview
        if isinstance(preview, Data):
            preview = preview.data
        result = submit_campaign(preview, self.trusted_user_id, self.confirmed_campaign_id,
                                 SubmissionDatabase(self.supabase_url, self.supabase_key))
        self.status = f"Campaign submitted: {result['recipient_count']} recipient(s)"
        return Data(data=result)
