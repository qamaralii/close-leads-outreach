"""Standalone, read-only Langflow 1.13.1 component; paste the entire file."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from lfx.custom import Component
from lfx.io import DataInput, Output, SecretStrInput, StrInput
from lfx.schema import Data


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Supabase backend key to a redirect destination.
        return None


class SupabaseReader:
    def __init__(self, url, key):
        url = str(url).strip()
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError("Supabase Project URL must be an HTTPS origin, without /rest/v1.")
        self.url = url.rstrip("/")
        self.key = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key or "")
        if not self.key.strip():
            raise ValueError("Supabase Backend Key is required.")
        self.opener = build_opener(NoRedirects())

    def select(self, table, params):
        headers = {"apikey": self.key, "Accept": "application/json"}
        if not self.key.startswith("sb_secret_"):
            headers["Authorization"] = f"Bearer {self.key}"
        request = Request(f"{self.url}/rest/v1/{table}?{urlencode(params)}", headers=headers)
        try:
            with self.opener.open(request, timeout=60) as response:
                rows = json.load(response)
        except HTTPError as exc:
            # Response bodies and request headers can contain sensitive details.
            raise ValueError(f"Supabase read failed for {table} (HTTP {exc.code}).") from None
        except (URLError, TimeoutError, OSError):
            raise ValueError(f"Supabase network/timeout error reading {table}; retry this read-only run.") from None
        except (ValueError, UnicodeError):
            raise ValueError(f"Supabase returned invalid JSON for {table}.") from None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Unexpected Supabase response for {table}.")
        return rows


def read_all(reader, table, params):
    """Page even small IN queries: project PostgREST row caps may be customized."""
    rows = []
    offset = 0
    while True:
        page = reader.select(table, {**params, "limit": "50", "offset": str(offset)})
        if not page:
            return rows
        rows.extend(page)
        offset += len(page)
        if offset > 10_000:
            raise ValueError("Unexpectedly large Supabase result; resolution stopped.")


def quoted_in(values):
    # PostgREST string literals: JSON escaping handles quotes and backslashes.
    return "in.(" + ",".join(json.dumps(value) for value in values) + ")"


def valid_email(value):
    """Conservative ASCII dot-atom/domain validator; no display names or lists."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if len(value) > 254 or value.count("@") != 1 or not value.isascii():
        return False
    local, domain = value.rsplit("@", 1)
    if not 1 <= len(local) <= 64 or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+", local):
        return False
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    labels = domain.split(".")
    return len(labels) >= 2 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels
    )


def resolve_contacts(request, reader):
    if not isinstance(request, dict) or request.get("operation") != "prepare_preview":
        raise ValueError("Connect the Request Validator's Validated Request output.")
    try:
        user_id = str(UUID(request["user_id"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("Validated Request must contain a Supabase user UUID.") from None
    ids = request.get("selected_lead_ids")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 1000
            or any(not isinstance(i, str) or not re.fullmatch(r"lead_[A-Za-z0-9_-]{1,200}", i) for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("Validated Request must contain unique Close lead IDs.")
    if not isinstance(request.get("request_id"), str) or not request["request_id"].strip():
        raise ValueError("Validated Request must contain a request ID.")
    if (request.get("is_html") is not False or request.get("send_mode") != "now"
            or request.get("scheduled_at") is not None or request.get("timezone") is not None):
        raise ValueError("Only immediate, plain-text previews are supported.")
    pattern = re.compile(r"(?<!\{)\{\{\s*([a-z_]+)\s*\}\}(?!\})")
    needed = set()
    for field, limit in (("subject", 998), ("body", 100_000)):
        text = request.get(field)
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            raise ValueError(f"Invalid {field} template; run the Request Validator first.")
        names = set(pattern.findall(text))
        remainder = pattern.sub("", text)
        if names - {"first_name", "company"} or "{{" in remainder or "}}" in remainder:
            raise ValueError("Unsupported or malformed personalization placeholder.")
        needed.update(names)

    members = reader.select("outreach_members", {
        "select": "user_id,is_active", "user_id": f"eq.{user_id}", "limit": "2"
    })
    if len(members) != 1 or members[0].get("is_active") is not True:
        raise ValueError("User is not an active outreach member.")
    connections = reader.select("outlook_connections", {
        "select": "id,user_id,composio_entity_id,composio_connected_account_id,mailbox_email,status",
        "user_id": f"eq.{user_id}", "limit": "2"
    })
    if len(connections) != 1 or connections[0].get("status") != "active":
        raise ValueError("User must have one active Outlook connection.")
    sender = connections[0]
    if (any(not isinstance(sender.get(key), str) or not sender[key].strip()
            for key in ("id", "composio_entity_id", "composio_connected_account_id"))
            or not valid_email(sender.get("mailbox_email"))):
        raise ValueError("Stored Outlook connection metadata is incomplete or invalid.")

    leads = {}
    for start in range(0, len(ids), 50):
        rows = read_all(reader, "close_leads", {
            "select": "close_lead_id,company_name,contact_name,first_name,email",
            "close_lead_id": quoted_in(ids[start:start + 50]), "order": "close_lead_id.asc"
        })
        for row in rows:
            key = row.get("close_lead_id")
            if key not in ids or key in leads:
                raise ValueError("Unexpected or duplicate lead returned by Supabase.")
            leads[key] = row

    emails = sorted({row["email"].strip().lower() for row in leads.values() if valid_email(row.get("email"))})
    suppressed = set()
    for start in range(0, len(emails), 50):
        rows = read_all(reader, "email_suppressions", {
            "select": "normalized_email", "normalized_email": quoted_in(emails[start:start + 50]),
            "order": "normalized_email.asc"
        })
        suppressed.update(row["normalized_email"] for row in rows)

    recipients, excluded, errors = [], [], []
    seen = {}
    for lead_id in ids:
        row = leads.get(lead_id)
        if row is None:
            excluded.append({"close_lead_id": lead_id, "reason": "lead_not_found"})
            continue
        email = row.get("email")
        if not valid_email(email):
            excluded.append({"close_lead_id": lead_id, "reason": "missing_or_invalid_email"})
            continue
        email = email.strip()
        normalized = email.lower()
        if normalized in suppressed:
            excluded.append({"close_lead_id": lead_id, "reason": "suppressed"})
            continue
        if normalized in seen:
            excluded.append({"close_lead_id": lead_id, "reason": "duplicate_email",
                             "duplicate_of_lead_id": seen[normalized]})
            continue
        seen[normalized] = lead_id
        values = {"first_name": row.get("first_name"), "company": row.get("company_name")}
        missing = sorted(name for name in needed if not isinstance(values[name], str) or not values[name].strip())
        if missing:
            errors.append({"close_lead_id": lead_id, "reason": "missing_personalization", "fields": missing})
            continue
        rendered = {
            field: pattern.sub(lambda match: values[match.group(1)].strip(), request[field])
            for field in ("subject", "body")
        }
        invalid = []
        for field, limit in (("subject", 998), ("body", 100_000)):
            value = rendered[field]
            if (len(value) > limit or "{{" in value or "}}" in value
                    or any(ord(c) < 32 and c not in "\r\n\t" for c in value) or "\x7f" in value
                    or (field == "subject" and any(c in value for c in "\r\n\t"))):
                invalid.append(field)
        if invalid:
            errors.append({"close_lead_id": lead_id, "reason": "invalid_rendered_content", "fields": invalid})
            continue
        recipients.append({
            "close_lead_id": lead_id, "recipient_email": email,
            "recipient_name": row.get("contact_name"), "company_name": row.get("company_name"),
            "rendered_subject": rendered["subject"], "rendered_body": rendered["body"],
        })

    can_prepare = bool(recipients) and not errors
    return {
        "operation": "resolved_preview", "can_prepare": can_prepare,
        "user_id": user_id, "request_id": request["request_id"],
        "sender_connection_id": sender["id"],
        "sender": {key: sender[key] for key in (
            "mailbox_email", "composio_entity_id", "composio_connected_account_id"
        )},
        "subject_template": request["subject"], "body_template": request["body"],
        "is_html": False, "send_mode": "now",
        "recipients": recipients, "excluded": excluded, "errors": errors,
        "counts": {"selected": len(ids), "eligible": len(recipients),
                   "excluded": len(excluded), "blocked": len(errors)},
    }


class OutreachContactResolver(Component):
    display_name = "Contact Resolver & Personalization"
    description = "Read contacts and sender metadata from Supabase; render a preview without saving or sending."
    icon = "Users"
    name = "OutreachContactResolver"
    inputs = [
        DataInput(name="validated_request", display_name="Validated Request", required=True),
        StrInput(name="supabase_url", display_name="Supabase Project URL", required=True),
        SecretStrInput(name="supabase_key", display_name="Supabase Backend Key", required=True),
    ]
    outputs = [Output(name="resolved_preview", display_name="Resolved Preview", method="resolve_preview")]

    def resolve_preview(self) -> Data:
        request = self.validated_request
        if isinstance(request, Data):
            request = request.data
        result = resolve_contacts(request, SupabaseReader(self.supabase_url, self.supabase_key))
        counts = result["counts"]
        self.status = (
            f"{counts['eligible']} eligible, {counts['excluded']} excluded, "
            f"{counts['blocked']} blocked; can_prepare={result['can_prepare']}"
        )
        return Data(data=result)
