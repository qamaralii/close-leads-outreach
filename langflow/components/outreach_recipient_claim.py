"""Standalone Langflow 1.13.1 atomic claim. Never sends or retries a claim."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from lfx.custom import Component
from lfx.io import DataInput, Output, SecretStrInput, StrInput
from lfx.schema import Data


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ClaimDatabase:
    def __init__(self, url, key):
        url = str(url).strip()
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Supabase Project URL must be an HTTPS origin without /rest/v1.")
        self.url = url.rstrip("/")
        self.key = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or "")
        if not self.key.strip():
            raise ValueError("Supabase Backend Key is required.")
        self.opener = build_opener(NoRedirects())

    def claim(self, user_id, campaign_id, recipient_id):
        headers = {"apikey": self.key, "Accept": "application/json", "Content-Type": "application/json"}
        if not self.key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {self.key}"
        payload = {"p_user_id": user_id, "p_campaign_id": campaign_id, "p_recipient_id": recipient_id}
        request = Request(self.url + "/rest/v1/rpc/claim_outreach_recipient",
                          data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with self.opener.open(request, timeout=120) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code >= 500:
                raise ValueError("Claim outcome is uncertain after a server error. Do not retry automatically or send; inspect database state.") from None
            raise ValueError(f"Supabase claim rejected (HTTP {exc.code}).") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise ValueError("Claim response was lost or invalid. Do not retry automatically or send; inspect database state.") from None


def uuid_value(value, label):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{label} must be a UUID.") from None


def claim_recipient(submitted, trusted_user_id, recipient_id, database):
    if not isinstance(submitted, dict) or submitted.get("operation") != "submitted_campaign":
        raise ValueError("Connect the Submitted Campaign output.")
    user_id = uuid_value(trusted_user_id, "Trusted User ID")
    if uuid_value(submitted.get("user_id"), "Campaign user ID") != user_id:
        raise ValueError("Campaign owner differs from the trusted user.")
    campaign_id = uuid_value(submitted.get("campaign_id"), "Campaign ID")
    recipients = submitted.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        raise ValueError("Submitted campaign must contain recipient IDs.")
    ids = []
    for row in recipients:
        if not isinstance(row, dict) or row.get("campaign_id") != campaign_id:
            raise ValueError("Recipient does not belong to the submitted campaign.")
        ids.append(uuid_value(row.get("id"), "Recipient ID"))
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate recipient IDs in submission input.")
    if not recipient_id:
        if len(ids) != 1:
            raise ValueError("For multiple recipients, select one Recipient ID per claim execution.")
        recipient_id = ids[0]
    else:
        recipient_id = uuid_value(recipient_id, "Selected Recipient ID")
    if recipient_id not in ids:
        raise ValueError("Selected recipient is not in the submitted campaign output.")
    # Input statuses can be stale. Only the atomic RPC decides whether this job is claimable.
    rows = database.claim(user_id, campaign_id, recipient_id)
    base = {"operation": "recipient_claim", "user_id": user_id,
            "campaign_id": campaign_id, "recipient_id": recipient_id}
    if rows == []:
        return {**base, "claimed": False, "reason": "not_claimable",
                "detail": "No job returned: already claimed, not due, suppressed, or otherwise ineligible."}
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("Unexpected claim response. Do not send or retry automatically; inspect database state.")
    row = rows[0]
    try:
        if uuid_value(row.get("recipient_id"), "Returned recipient ID") != recipient_id:
            raise ValueError("Returned recipient mismatch.")
        attempt_id = uuid_value(row.get("attempt_id"), "Attempt ID")
        token = uuid_value(row.get("claim_token"), "Claim token")
        for key in ("entity_id", "connected_account_id", "sender_email", "recipient_email", "subject", "body"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ValueError("Incomplete claim response.")
        if row.get("is_html") is not False:
            raise ValueError("Only plain-text claims are supported.")
    except ValueError:
        raise ValueError("Invalid claim payload. Do not send or retry automatically; inspect database state.") from None
    return {**base, "claimed": True, "attempt_id": attempt_id, "claim_token": token,
            "entity_id": row["entity_id"], "connected_account_id": row["connected_account_id"],
            "sender_email": row["sender_email"], "recipient_email": row["recipient_email"],
            "subject": row["subject"], "body": row["body"], "is_html": row["is_html"]}


class OutreachRecipientClaim(Component):
    display_name = "Claim Outreach Recipient"
    description = "Atomically claim one queued recipient and return the saved message and attempt identifiers."
    icon = "Lock"
    name = "OutreachRecipientClaim"
    inputs = [
        DataInput(name="submitted_campaign", display_name="Submitted Campaign", required=True),
        StrInput(name="trusted_user_id", display_name="Trusted User ID", required=True,
                 info="Backend-supplied verified Supabase user; same trusted identity as submission."),
        StrInput(name="recipient_id", display_name="Recipient ID", value="",
                 info="Blank auto-selects only when the input contains exactly one recipient."),
        StrInput(name="supabase_url", display_name="Supabase Project URL", required=True),
        SecretStrInput(name="supabase_key", display_name="Supabase Backend Key", required=True),
    ]
    outputs = [Output(name="recipient_claim", display_name="Recipient Claim", method="claim_saved_recipient")]

    def claim_saved_recipient(self) -> Data:
        submitted = self.submitted_campaign
        if isinstance(submitted, Data):
            submitted = submitted.data
        result = claim_recipient(submitted, self.trusted_user_id, self.recipient_id,
                                 ClaimDatabase(self.supabase_url, self.supabase_key))
        self.status = "Recipient claimed; attempt created" if result["claimed"] else "No claimable job returned"
        return Data(data=result)
