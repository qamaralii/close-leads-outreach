"""Paste this entire file into a custom component in Langflow 1.13.1."""

import json
import re
from uuid import UUID

from lfx.custom import Component
from lfx.io import MessageTextInput, MultilineInput, Output
from lfx.schema import Data


def validate_outreach_request(request_json: str, trusted_user_id: str) -> dict:
    """Validate a preview request without sending or accessing external systems.

    trusted_user_id must be supplied by an authenticated backend, never extracted
    from browser JSON. Syntax validation here does not authenticate that user.
    """
    try:
        user_id = str(UUID(str(trusted_user_id).strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Trusted User ID must be a Supabase Auth UUID.") from exc
    if UUID(user_id).int == 0:
        raise ValueError("Trusted User ID cannot be the nil UUID.")
    if not isinstance(request_json, str) or not request_json.strip():
        raise ValueError("Request JSON is required.")
    if len(request_json.encode("utf-8")) > 256_000:
        raise ValueError("Request JSON exceeds the 256 KB limit.")

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Request JSON contains a duplicate field.")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Request JSON contains a non-JSON numeric constant.")

    try:
        request = json.loads(
            request_json, object_pairs_hook=unique_keys, parse_constant=reject_constant
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Request JSON must be valid JSON.") from exc
    if not isinstance(request, dict):
        raise ValueError("Request JSON must be an object.")

    required = {"request_id", "selected_lead_ids", "subject", "body"}
    allowed = required | {"send_mode", "scheduled_at", "timezone", "is_html"}
    if set(request) - allowed:
        raise ValueError(
            "Request contains unsupported fields. Sender, user identity, recipient "
            "addresses, and campaign IDs must not be supplied in preview JSON."
        )
    if required - set(request):
        raise ValueError("Missing required fields: " + ", ".join(sorted(required - set(request))))

    request_id = request["request_id"]
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id):
        raise ValueError("Request ID must be 1–128 letters, digits, underscores, dots, colons, or hyphens.")

    lead_ids = request["selected_lead_ids"]
    if not isinstance(lead_ids, list) or not 1 <= len(lead_ids) <= 1000:
        raise ValueError("Select between 1 and 1000 Close lead IDs.")
    for lead_id in lead_ids:
        if not isinstance(lead_id, str) or not re.fullmatch(r"lead_[A-Za-z0-9_-]{1,200}", lead_id):
            raise ValueError("Every selected lead ID must be a valid lead_-prefixed identifier.")
    if len(set(lead_ids)) != len(lead_ids):
        raise ValueError("Selected lead IDs contain duplicates.")

    for field, limit in (("subject", 998), ("body", 100_000)):
        value = request[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field.title()} must be nonempty text.")
        if len(value) > limit:
            raise ValueError(f"{field.title()} exceeds the {limit}-character input limit.")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in value) or "\x7f" in value:
            raise ValueError(f"{field.title()} contains unsupported control characters.")
    if any(char in request["subject"] for char in "\r\n\t"):
        raise ValueError("Subject must be a single line without tabs.")

    if request.get("send_mode", "now") != "now" or request.get("scheduled_at") is not None:
        raise ValueError("Scheduling is deferred. Use send_mode 'now' and scheduled_at null.")
    if request.get("is_html", False) is not False:
        raise ValueError("This initial preview supports plain text only; is_html must be false.")
    if request.get("timezone") is not None:
        raise ValueError("Timezone must be omitted or null while scheduling is deferred.")

    placeholders = set()
    placeholder_pattern = r"(?<!\{)\{\{\s*([a-z_]+)\s*\}\}(?!\})"
    for field in ("subject", "body"):
        value = request[field]
        for match in re.finditer(placeholder_pattern, value):
            name = match.group(1)
            if name not in {"first_name", "company"}:
                raise ValueError("Supported placeholders are {{first_name}} and {{company}} only.")
            placeholders.add(name)
        remainder = re.sub(placeholder_pattern, "", value)
        if "{{" in remainder or "}}" in remainder:
            raise ValueError(f"{field.title()} contains a malformed personalization placeholder.")

    return {
        "operation": "prepare_preview",
        "user_id": user_id,
        "request_id": request_id,
        "selected_lead_ids": list(lead_ids),
        "subject": request["subject"],
        "body": request["body"],
        "is_html": False,
        "send_mode": "now",
        "scheduled_at": None,
        "timezone": None,
        "placeholders": sorted(placeholders),
    }


class OutreachRequestValidator(Component):
    display_name = "Outreach Request Validator"
    description = "Validate a plain-text outreach preview request without sending email."
    icon = "CheckCheck"
    name = "OutreachRequestValidator"

    inputs = [
        MultilineInput(
            name="request_json",
            display_name="Request JSON",
            required=True,
            info="Preview request containing request_id, selected_lead_ids, subject, and body.",
        ),
        MessageTextInput(
            name="trusted_user_id",
            display_name="Trusted User ID",
            required=True,
            info="Supabase Auth UUID from the verified backend session. Manual test UUID during development only.",
        ),
    ]
    outputs = [
        Output(display_name="Validated Request", name="validated_request", method="validate_request"),
    ]

    def validate_request(self) -> Data:
        result = validate_outreach_request(self.request_json, self.trusted_user_id)
        self.status = f"Validated preview request for {len(result['selected_lead_ids'])} selected lead(s)."
        return Data(data=result)
