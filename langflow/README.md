# Flow 2 — step 1: request validation

## Install in Langflow 1.13.1

1. Create a new blank flow named **Outreach — Preview**.
2. Add a **Custom Component** (or open a component's code editor).
3. Replace its entire code with `components/outreach_request_validator.py`.
4. Save/build the component. It should display **Outreach Request Validator**.
5. Enter your Supabase Auth UUID in **Trusted User ID**. Do not enter the Composio
   `pg-test-...` identifier here.
6. Paste the following into **Request JSON**, replacing the sample lead ID with a
   real `close_lead_id` from Supabase:

```json
{
  "request_id": "preview-test-001",
  "selected_lead_ids": ["lead_REPLACE_WITH_REAL_ID"],
  "subject": "Hello {{company}}",
  "body": "Hi {{first_name}},\n\nThis is a preview test.",
  "send_mode": "now",
  "scheduled_at": null,
  "is_html": false
}
```

7. Run this component and inspect **Validated Request**. Expected: a Data object
   containing `operation: prepare_preview`, your user UUID, selected IDs, unchanged
   subject/body, and `placeholders: ["company", "first_name"]`.

At this step there are **no wires to connect yet**. The next component will accept
this Data output as its request input and resolve contacts and the sender from Supabase.
Do not connect this output directly to Outlook.

To find a test lead in SQL Editor:

```sql
SELECT close_lead_id, company_name, first_name, email
FROM public.close_leads
WHERE NULLIF(btrim(email), '') IS NOT NULL
  AND NULLIF(btrim(first_name), '') IS NOT NULL
  AND NULLIF(btrim(company_name), '') IS NOT NULL
LIMIT 5;
```

## Contract and boundaries

- This node performs syntax/shape validation only. It does not authenticate users,
  query Supabase, check membership, render messages, persist previews, or send email.
- The production server must validate the Supabase session and supply **Trusted User
  ID** separately. Never allow browser-provided identity or an arbitrary Langflow
  input override. The Langflow API stays behind the authenticated backend.
- Required fields: `request_id`, `selected_lead_ids`, `subject`, `body`.
- Optional fields: `send_mode` (only `now`), `scheduled_at` (only null), `timezone`
  (only null), `is_html` (only false). Unknown fields are rejected, including sender
  overrides, user IDs, addresses, and campaign IDs.
- Unique request IDs are caller-supplied: reuse for retries of the same preview;
  generate a new one when editing content/recipients. Database persistence will enforce
  uniqueness and the future backend must compare contents on request-ID conflicts.
- Between 1 and 1000 unique lead IDs; no silent dropping of duplicates.
- Plain-text content is preserved exactly. Supported placeholders are case-sensitive
  `{{first_name}}` and `{{company}}`, with optional whitespace inside the braces.
  The resolver must reject missing/blank values per recipient and enforce rendered
  message limits; this validator only checks template limits.
- Scheduling and HTML are not supported in this first component.
- Input limits are application limits, not claims about Outlook provider limits.
- Connection identifiers and recipient addresses come from the resolver, not this JSON.

## Local verification

Run from the project root:

```bash
python -m unittest discover -s tests -v
```

Tests use small Langflow import stubs to exercise validation without installing or
starting Langflow. Actual component loading and UI execution must also be checked in
your Langflow instance; these tests do not verify Supabase or Outlook.

## Step 2: Contact Resolver & Personalization

1. Add another custom component to **Outreach — Preview**.
2. Paste the entire contents of `components/outreach_contact_resolver.py` into its
   code editor and save.
3. Wire **Outreach Request Validator / Validated Request** to
   **Contact Resolver & Personalization / Validated Request**.
4. Set **Supabase Project URL** to the HTTPS project root, e.g.
   `https://YOUR_PROJECT.supabase.co` (no `/rest/v1`).
5. Set **Supabase Backend Key** to the same backend secret variable used by Flow 1.
   Use a backend `sb_secret_...` or legacy `service_role` key, not the browser key.
6. Run the resolver and inspect **Resolved Preview**.

Expected for your one-lead test:

```json
{
  "operation": "resolved_preview",
  "can_prepare": true,
  "counts": {"selected": 1, "eligible": 1, "excluded": 0, "blocked": 0}
}
```

The actual output also includes the verified database sender mapping, request/user IDs,
templates, rendered `recipients`, `excluded`, and `errors`. Confirm the recipient email,
rendered subject/body, and sender mailbox are correct. No campaign is saved and no
Composio/Outlook call is made. Connection status is checked in Supabase only; this does
not verify live Composio authorization or which mailbox Composio will select.

Eligibility rules in this version:

- Missing leads, missing/invalid email addresses, and suppressed addresses are excluded
  with explicit reasons. Email validation is deliberately conservative: ASCII local
  parts and dotted DNS domains; quoted local parts, display-name forms, and international
  addresses are excluded for now. It is syntax validation, not deliverability checking.
- Duplicate emails are compared after trimming and lowercasing. The first eligible
  occurrence in selection order supplies personalization; subsequent occurrences are
  reported with the retained lead ID. Suppression is checked before deduplication.
- No company/customer/status eligibility rules are inferred.
- A missing required personalization value blocks the **entire** preview, even if some
  recipients render successfully. Invalid rendered subjects/bodies also block it.
- `can_prepare` is false when there are blocking errors or no eligible recipients.
  The next persistence component must enforce this flag and show exclusions to the user.
- Values substituted into templates are trimmed. Other message text and line breaks
  are preserved. Substituted values are never recursively rendered.
- Supabase reads are paginated and batched; a read error aborts resolution rather than
  silently using incomplete suppression data. Reads across tables are not one snapshot;
  the persistence and send stages must recheck eligibility as appropriate.

The supplied `user_id` remains trusted backend input, not a login credential. Checking
membership does not authenticate the caller. Never expose this privileged flow directly
to browsers. Templates and resolved messages appear in Langflow outputs/traces, so restrict
Langflow access to the backend operators.

### Multiline body check

The original single-line input worked in the deployed validator. To test line breaks,
enter a literal backslash followed by `n` **inside the JSON body string**, e.g.
`"body": "Hi {{first_name}},\n\nThis is a preview test."`. Do not insert physical line
breaks inside a quoted JSON string. If this still fails, copy the complete trace Input
JSON so the exact received string can be diagnosed. The resolver's local tests verify
that decoded line breaks are preserved during personalization.

## Step 3: Persist Outreach Preview

Paste `components/outreach_preview_persistence.py` into a third custom component.
Wire **Contact Resolver & Personalization / Resolved Preview** to
**Persist Outreach Preview / Resolved Preview**. Set the same **Supabase Project URL**
and secret **Supabase Backend Key** as the resolver. Run the persistence component.

This step **writes a draft campaign and ready recipient jobs** through
`prepare_outreach_preview`; it never calls submission, claiming, or Outlook.

Expected output includes:

```json
{
  "operation": "persisted_preview",
  "campaign_id": "a-new-campaign-uuid",
  "campaign_status": "draft",
  "can_submit": true,
  "reused_existing": false,
  "recipient_count": 1
}
```

The full output contains the exact **reloaded** recipient snapshots and their database
IDs. `can_submit` means the saved state is ready for a later explicit confirmation;
it is not permission to automatically send. Recheck sender health/suppression before
execution. `resolution_exclusions` is the current resolver report, not stored history.

### Verify the draft

Use the returned campaign ID in this read-only SQL:

```sql
SELECT id, created_by, request_id, status, expected_recipient_count, preview_ready_at
FROM public.campaigns
WHERE id = 'YOUR_RETURNED_CAMPAIGN_ID'::uuid;

SELECT id, recipient_email, rendered_subject, rendered_body, status
FROM public.campaign_recipients
WHERE campaign_id = 'YOUR_RETURNED_CAMPAIGN_ID'::uuid;
```

Run the same flow again with unchanged input. Expect the **same campaign ID**,
`reused_existing: true`, and no additional campaign/recipient rows. An edited subject,
body, rendered recipient snapshot, or sender with the same request ID causes an explicit
conflict. Use a new request ID for an intentionally new preview. Re-running the resolver
after a Close update can also cause a conflict; that protects the stored preview.

Duplicate-request recovery is scoped by the supplied owner and request ID, and compares
all message/recipient snapshots without relying on list ordering. A timeout may follow
a committed database transaction: the component looks up the existing request rather
than issuing a second write immediately. If recovery cannot confirm a saved campaign,
retry later with the **same request ID**. A failed reload can likewise follow a successful
save. The database uniqueness constraint protects concurrent creation.

The component rechecks membership and, for new previews, sender metadata and suppression.
The RPC snapshots sender metadata again; if it changes concurrently, the reload comparison
blocks the result. These reads do not provide a transaction-wide eligibility snapshot;
the send stage must recheck. Previously saved previews remain immutable even if a contact
or suppression entry changes. Reusing an already submitted campaign returns its status
with `can_submit: false` and never queues recipients again.

Local tests use mocked database responses; live SQL/RLS/concurrent-RPC behavior still
requires verification in Supabase. This component must remain behind the authenticated
backend: a user UUID in a Data object is not proof of authentication.

## Step 4: Submit Outreach Campaign

Paste `components/outreach_campaign_submission.py` into a fourth custom component.
Wire **Persist Outreach Preview / Persisted Preview** to
**Submit Outreach Campaign / Persisted Preview**.

Set:

| Field | Value |
| --- | --- |
| Trusted User ID | Your Supabase Auth user UUID; use the same trusted identity as the validator |
| Confirmed Campaign ID | Leave blank until review, then paste the campaign UUID from the saved preview |
| Supabase Project URL | Same HTTPS project origin as the previous components |
| Supabase Backend Key | Same backend secret variable |

Entering the matching campaign ID and running this node is the explicit submission
action. Do not automatically connect a campaign-ID output to the confirmation field.
In the production app, a separate authenticated confirmation endpoint supplies the
trusted user ID and confirmed campaign ID after the user reviews the saved preview.
The browser must not have direct access to this Langflow component or backend secret.

This calls only `submit_outreach_campaign(p_user_id, p_campaign_id)` for writes.
The database locks the campaign and validates ownership, active membership, finalized
preview, recipient states, and the reviewed sender mapping. It changes the campaign
from `draft` to `submitted`, and its recipients from `ready` to `queued` with `due_at`
set to the submission time. It does not call Composio or claim any recipient.

For the current one-recipient test, expect:

```json
{
  "operation": "submitted_campaign",
  "campaign_status": "submitted",
  "already_submitted": false,
  "recovered_after_error": false,
  "recipient_count": 1,
  "recipient_status_counts": {"queued": 1}
}
```

The full result includes the campaign ID, submission timestamp, and reloaded recipients.
Run again with the same campaign ID: expect `already_submitted: true`, the same
`submitted_at`, and unchanged recipient states. The RPC's repeated-submission path is
a no-op; it never resets `sending`, `submitted`, `unknown`, or other recipient states.
Input `can_submit` may be stale, so the database state is authoritative.

For a timeout or server error, the component reloads the owner-scoped campaign to see
whether submission committed. It does not issue an immediate second write. If submission
or the subsequent reload cannot be confirmed, retry with the **same campaign ID**.
`recovered_after_error: true` means committed submission was confirmed after such an error.

Inspect the campaign's `status`/`submitted_at` and recipients' `status`/`due_at` in Supabase
to verify the transition. Status counts are a readback, not an atomic snapshot if workers
are running. Eligibility and sender authorization must be checked at the later claim/send
stages; a queued recipient is not a delivered email. Currently no send worker is connected.

Local submission tests mock the database and HTTP transport. Live submission/retry,
sender-change rejection, RLS, and concurrency behavior require Supabase verification.

## Step 5: Claim Outreach Recipient

Paste `components/outreach_recipient_claim.py` into a new custom component.
Wire **Submit Outreach Campaign / Submitted Campaign** to
**Claim Outreach Recipient / Submitted Campaign**. Set the same trusted Supabase
user ID, project URL, and backend secret. Leave **Recipient ID** blank for the
current one-recipient campaign: it is selected dynamically from the input.
For multiple recipients, an eventual iterator must supply one recipient ID per
execution; this node deliberately refuses to select an arbitrary recipient.

Run the claim node **once**. Expected output has `operation: recipient_claim`,
`claimed: true`, campaign/recipient IDs, `attempt_id`, `claim_token`, `entity_id`,
`connected_account_id`, `sender_email`, `recipient_email`, `subject`, `body`, and
`is_html: false`. Message and sender values come from the atomic RPC's saved
snapshots, not the input preview. Retain this first successful output for the
upcoming send/finish steps; keep the claim token within trusted backend execution.

This is a real state transition: the recipient becomes `sending` and an attempt
with status `sending` is inserted, although this component itself never calls Outlook. There is
no automatic expiry/reclaim. Do not reset the row to `queued` to repeat a test.

A second invocation returns `claimed: false` with no message or attempt token when
the RPC returns an empty list. That can mean already claimed, suppressed, not due,
or otherwise ineligible; an empty result does not identify the exact reason.
Do not rerun the successful claim simply to supply a downstream node. The send
stage needs the original claim payload and must execute only once for that attempt.
A claim token does not itself deduplicate provider sends or make replay safe.

If the HTTP response is lost or invalid, the claim may already have committed.
The component stops without retries or recovering a send payload from database
rows. Inspect the recipient/attempt state before proceeding. Likewise, route
`claimed: false` to a stop branch; never feed empty fields or a prior cached payload
to Outlook. Sender-account isolation and live OAuth validity still require separate
verification in the send stage.

Read-only live verification (substitute the returned recipient UUID):

```sql
SELECT id, campaign_id, status, claimed_at
FROM public.campaign_recipients
WHERE id = 'YOUR_RECIPIENT_ID'::uuid;

SELECT id, recipient_id, status
FROM public.outreach_attempts
WHERE recipient_id = 'YOUR_RECIPIENT_ID'::uuid;
```

Local tests mock RPC/HTTP responses, including empty claims, malformed responses,
and lost responses. Live concurrency, suppression, and sender-change checks are
enforced by the existing RPC and still need Supabase integration verification.

## Step 6: Send & Record Outreach Attempt (isolated live test)

First apply `../supabase/migrations/202611100002_outreach_send_gate.sql` in the
Supabase SQL editor, once, after the foundation migration. It adds
`dispatch_started_at` and the service-only `begin_outreach_send` RPC. Existing
claimed attempts remain usable. The RPC atomically consumes one dispatch permission,
rechecks membership, token, sender mapping and suppression, and returns DB snapshots.
This is needed because replaying a claim payload otherwise can resend an email.

Create **a separate Langflow flow** with one custom component using the complete
contents of `components/outreach_send_and_record.py`. Do not connect the earlier
claim node or the built-in Outlook node to this test flow.

| Field | Value |
| --- | --- |
| Saved Claim JSON | Original successful claim JSON, including the original claim token |
| Trusted User ID | Same Supabase Auth UUID used in the previous steps |
| Supabase Project URL | Same HTTPS project origin |
| Supabase Backend Key | Same backend secret variable |
| Composio API Key | Project API key that owns the mapped Outlook connection |
| Record Only | Off for the first send |

**Running this node performs a real email send.** It uses the gate's stored message,
not editable message fields in the pasted claim. It passes both Composio user/entity
ID and exact connected-account ID to Composio's v3.1 proxy API, verifies the account
is active and owned by that entity, reads Microsoft Graph `/me`, and checks the
mailbox matches the reviewed sender. It then performs one `/me/sendMail` call with
plain-text content, one recipient, and `saveToSentItems: true`.

This uses Composio managed OAuth directly rather than the built-in Outlook node,
whose execution path did not explicitly select a connected-account ID. API contract:
<https://docs.composio.dev/reference/api-reference/tools/postToolsExecuteProxy>.
The proxy returns Microsoft's status as `status`, not the earlier built-in node's
`status_code` field. The outer Composio HTTP 200 alone does not establish acceptance.
Graph reference: <https://learn.microsoft.com/en-us/graph/api/user-sendmail>.

Expected result: `operation: outreach_send_result`, the original `attempt_id`,
`outcome: submitted`, `provider_status_code: 202`, and `recorded: true`.
Verify the inbox, Sent Items, and database states. `submitted` means Microsoft
accepted the request, not delivery. Graph sendMail supplies no message ID, so
`provider_message_id` stays null.

### Retry and recovery behavior

For a recorded preflight failure, enable **Preflight Only (No Send)** with the
original saved claim and **Record Only** off. This read-only diagnostic checks the
current Composio account and Graph profile, returns a specific sanitized error,
and never accesses Supabase or calls sendMail. It cannot establish historical state
or reopen a finalized failed attempt. Disable it only when intentionally moving to
a new send test after diagnosing the failure.

- Repeating with the same claim returns `send_skipped: true`; the database gate
  prevents another provider call, including from concurrent workers.
- Preflight failure records `failed` without attempting sendMail. It includes wrong
  mailbox, unavailable profile, changed owner metadata, or inactive connection.
  Graph profile-read and Mail.Send permissions are needed. Composio's account
  `user_id` response field is deprecated; if omitted, this implementation fails
  closed until ownership verification is adapted.
- A definitive Graph 4xx rejection (except 408) is `failed`. Timeout, 5xx, malformed
  response, or other uncertain send results become `unknown`. There is no resend.
- Recording failure returns `recorded: false` and a `recovery_receipt`. Preserve
  that receipt in trusted storage. Turn **Record Only** on and paste the receipt
  object into advanced **Recovery Receipt JSON**. This invokes only the existing
  idempotent finish RPC; it never initializes Composio or sends again. Retry only
  record-only with the identical receipt if the finish response is lost.
- A gate response loss, crash, or abandoned execution can leave `sending` with a
  dispatch marker. Never clear the marker or reset to queued: inspect/reconcile the
  attempt. The marker alone cannot establish whether Microsoft accepted a send.
  There is no exactly-once guarantee across database and provider; the gate favors
  avoiding duplicates. Gate rejection (such as new suppression) stops before send;
  reconcile the already claimed attempt rather than automatically resetting it.

The saved claim and recovery receipt are **trusted operator inputs**, not browser
API contracts. The production backend must provide verified session identity,
persist execution evidence, and expose a separate confirm/send entry point.
Recovery receipts are not independently authenticated provider evidence; never
accept arbitrary receipts from end users. Eligibility changes after the gate cannot
be atomic with the external send. Local tests mock services; live SQL concurrency,
multi-user isolation, and OAuth/proxy behavior still need verification.

Read-only SQL after sending:

```sql
SELECT id, status, dispatch_started_at, finished_at, provider_status_code, error
FROM public.outreach_attempts WHERE id = 'YOUR_ATTEMPT_ID'::uuid;

SELECT id, status, submitted_at
FROM public.campaign_recipients WHERE id = 'YOUR_RECIPIENT_ID'::uuid;
```
