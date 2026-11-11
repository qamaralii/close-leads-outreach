# Outreach database foundation

## Status

This document describes the original foundation contract. Migration 006 adds
in-place draft editing and replaces revision-free confirmation with
`confirm_outreach_revision`; see the root README's **Draft editing** section for
the current contract and deployment order. Submitted snapshots remain immutable.

`migrations/202611100001_outreach_foundation.sql` is a new, unapplied migration.
It extends the existing `public.close_leads` table without replacing Flow 1.
It does not create a webapp, Langflow Flow 2, Outlook onboarding, or a dispatcher.

## Apply in Supabase

1. Confirm you are in the Supabase project used by Flow 1 and that
   `public.close_leads` exists. Use a development project first if available. Before
   applying, inspect `pg_policies` using the query under **Verification** and remove or
   tighten any broad authenticated policy on `close_leads`; permissive policies combine
   with OR and could otherwise bypass member-only access.
2. Paste the migration into **SQL Editor** and run it as the project administrator.
   It is transactional and intended to run once; do not rerun after success. Prefer a
   Supabase migration runner with migration history over manual execution when available.
3. Invite each of the 2–3 users through **Authentication → Users**. Disable public
   signups for this invite-only app. Supabase Auth setup alone does not grant app access.
4. Register the invited Auth user UUIDs using SQL Editor:

   ```sql
   INSERT INTO public.outreach_members(user_id)
   VALUES ('REPLACE_WITH_AUTH_USER_UUID'::uuid);
   ```

5. Later, the authenticated backend will verify completed Composio connections
   and write `outlook_connections`. Do not mark a connection active just because
   a browser reports that authorization succeeded.

The existing Flow 1 backend writer keeps its grants. Browser access to leads is
read-only and limited to active members; campaigns and their results are owner-only.
The membership table and policies do not provision Supabase Auth users themselves.

## Data and access rules

- `outreach_members`: invite allowlist; deactivate a user with `is_active = false`.
- `outlook_connections`: one connection per user; globally unique Composio entity
  and connected-account ID. Owners can read their metadata; only the backend writes it.
- `campaigns`: unique `(created_by, request_id)`, an expected-recipient count and
  preview-finalization marker, plus a composite foreign key that requires the sender
  connection to belong to the campaign creator.
- `campaign_recipients`: frozen lead, recipient, and rendered-message snapshots;
  deduplicated by trimmed/lowercase email per campaign step. Only step 1 is supported.
- `outreach_attempts`: durable attempt and claim token, with owner-only reads.
- `email_suppressions`: backend-maintained, shared suppression list; no browser access.

All new tables have RLS. Authenticated browsers can read allowed rows but cannot
insert/update/delete them or execute sending RPCs. A campaign's existence, recipients,
and results are hidden from other users. No ownership can be inferred from a browser
parameter: the server must verify the Supabase session and supply its user UUID.

Check existing policies on `close_leads` before enabling browser access: PostgreSQL
permissive policies combine with OR, so older broad policies could widen access.
The supplied project context reports no existing frontend policies.

## Backend / Flow 2 contract to implement next

### A. Prepare a persisted preview

The server verifies the session and membership, resolves that user's active sender,
and passes trusted identity to Langflow. Langflow must:

1. Validate a nonempty request ID, selected lead IDs, subject/body, and `send_mode: now`.
2. Load contacts from `close_leads`; validate emails, deduplicate, check suppression,
   render supported placeholders, and report exclusions/missing fields.
   SQL nonempty checks do not implement email or template validation.
3. Call the service-only `prepare_outreach_preview` RPC with creator, request ID,
   sender connection, templates, and a JSON array containing exactly the eligible
   recipients and rendered messages.
4. Reload the returned campaign and all recipients after the RPC completes, verify the
   stored count, and return that persisted preview for explicit user review.

`prepare_outreach_preview` creates and finalizes the campaign plus all recipients in
one database transaction. Validation or constraint failure rolls the entire preview back.

The unique request constraint rejects concurrent duplicate creation. On conflict,
reload the owner's finalized campaign and all recipients, then compare the input with
the stored contents; return the freshly loaded persisted preview only for an equivalent
request, otherwise return a conflict. Never overwrite an existing campaign using an
upsert. Request IDs are scoped to the user. Separate
campaigns with different IDs can intentionally target the same email; this is not a
global "send only once ever" rule.

For the initial version, keep each finalized preview fixed; edits create a new preview
with a new request ID. Campaign and recipient tables are read-only even to `service_role`;
all lifecycle writes use the service-only RPCs. If a privileged administrative operation
deadlocks with submission, retry PostgreSQL SQLSTATE `40P01` without changing request ID.

### B. Submit only the reviewed campaign

Browser request: campaign ID, after explicit confirmation. Server verifies ownership,
then calls this service-only RPC using the verified user's ID:

```json
{
  "p_user_id": "verified-auth-user-uuid",
  "p_campaign_id": "reviewed-campaign-uuid"
}
```

RPC: `submit_outreach_campaign`. It atomically marks the draft submitted and queues
its recipients. Repeated submission of the same campaign is a no-op. Sender changes
since preview prevent submission; generate a new preview for a different sender.
Preview content and recipient sets become immutable after preview finalization.

### C. Claim and send one recipient at a time

Call `claim_outreach_recipient` with trusted `p_user_id`, `p_campaign_id`, and
`p_recipient_id`. Call it once for each queued recipient. It returns at most one row:

```text
recipient_id, attempt_id, claim_token,
entity_id, connected_account_id, sender_email,
recipient_email, subject, body, is_html
```

**Only a returned claim row authorizes a send.** An empty result means skip the job.
The RPC locks the campaign and recipient and records the attempt atomically. Competing
claims cannot receive the same job; different jobs can send concurrently after their
short claim transactions commit. No database transaction stays open during Outlook I/O.

Map the row to the existing Outlook node:

| Claim field | Outlook input |
| --- | --- |
| `entity_id` | Entity ID |
| `recipient_email` | To (one recipient) |
| `subject` | Subject |
| `body` | Body |
| `is_html` | Is Html |
| constant `me` | Outlook User Id |

Leave CC, BCC, and From Address empty. The connection determines the sender.
`connected_account_id` is retained for validation/auditing; Langflow v1.13.1's built-in
node executes using the entity, not an explicit connected-account ID. Verify that
Composio has exactly the intended active Outlook connection under that entity before
execution. Do not use `default` for multiple real users. Suggested entities are stable,
project-prefixed user identifiers, e.g. `close-outreach:<supabase-user-uuid>`.

Submission and claim verify the stored connection matches the reviewed sender. The
backend must also recheck Composio connection health before sending. Database locks
cannot prevent external account revocation or a suppression change after a claim.
The later execution layer should recheck suppression immediately before the call.

### D. Persist the result

Call `finish_outreach_attempt` with the returned attempt ID/token and verified owner:

```json
{
  "p_user_id": "verified-auth-user-uuid",
  "p_attempt_id": "returned-attempt-uuid",
  "p_claim_token": "returned-claim-token",
  "p_status": "submitted",
  "p_provider_status_code": 202,
  "p_provider_message_id": null,
  "p_error": null
}
```

- `submitted`: the observed Outlook response is `202`; it does not mean delivered.
- `failed`: a definitive rejection or a verified failure before submission.
- `unknown`: uncertain provider outcome, e.g. timeout after possible submission.

The identical completion request is safe to repeat if the database response is lost.
A different result for an already completed attempt is rejected. Retry **recording
the result**, not the Outlook send. The RPC intentionally allows completion after
membership deactivation so an in-flight attempt can still be recorded.

Jobs left `sending` by a crashed run are not reclaimed. Investigate and reconcile
them; no automatic retry, lease expiry, stale-job recovery, or exactly-once provider
delivery guarantee exists. Unknown/failed jobs are not automatically queued again.

## Lifecycle and progress

```text
campaign: draft → preview finalized → submitted
recipient: ready → queued → sending → submitted | failed | unknown
                         └→ cancelled (suppressed before claim)
```

User-facing campaign cancellation is not implemented. Campaign progress is computed
from its recipients' status counts;
campaign `submitted` means accepted for processing, not that all jobs are finished.
Provider delivery, replies, and bounces need separate integrations.

Scheduling is deliberately rejected (`send_mode` must be `now`, `scheduled_at` null).
The fields are reserved for a future migration; there is no recurring dispatcher.

## Verification

The migration has been parsed successfully as PostgreSQL using `pglast`. That checks
syntax structure only; it does not execute DDL, RLS, triggers, or concurrent claims.
Live integration verification is still required in a development Supabase project.

After applying in Supabase, also check:

```sql
SELECT count(*) FROM public.close_leads; -- Compare with your current Flow 1 dataset.

SELECT tablename, policyname, roles, cmd
FROM pg_policies
WHERE schemaname = 'public'
  AND tablename IN ('close_leads', 'outreach_members', 'outlook_connections',
                    'campaigns', 'campaign_recipients', 'outreach_attempts',
                    'email_suppressions')
ORDER BY tablename, policyname;
```

Log in as two actual invited users to verify browser isolation, then verify two
Composio entities send from two different mailboxes. Local PostgreSQL tests cannot
verify Supabase Auth configuration, Composio connections, or Langflow execution.

Historical records intentionally use restrictive foreign keys: do not hard-delete Auth
users or referenced Close leads. Deactivate app members and use future Close inactive
marking instead. If legal deletion is required, add an explicit archival/deletion design
rather than cascading away campaign history.
