# Close Outreach — Railway MVP

Next.js App Router + TypeScript, invite-only Supabase Auth, existing Supabase
campaign lifecycle RPCs, and a separate sequential Python worker on Railway.

## Implemented milestone

- Invited users accept an email link, set a password, sign in, reset passwords, and sign out.
- Protected reads use the authenticated Supabase client and existing RLS. Every
  write action verifies the session with `getUser()` and checks active membership.
- Shared contact search, pagination, and selections up to 1,000 recipients.
- Server-side personalization, suppression checks, deduplication and saved draft
  previews. Exclusions are persisted alongside the app request.
- Drafts support campaign-wide template/selection edits and individual personalized
  message overrides, with reset-to-template and revision-aware confirmation.
- One-time scheduling supports local-time defaults, selectable time zones,
  rescheduling, and returning scheduled campaigns to draft before their send time.
- CC and attachments have campaign defaults with independent per-recipient overrides.
  Private uploads support up to 20 files and 25 MB of combined raw bytes per email.
- Active members can request a shared Close lead sync from the composer, with
  progress, retries, and automatic contact-directory refresh on completion.
- A distinct confirmation action submits the reviewed campaign revision. No caller-controlled
  identity, sender, or Langflow inputs are accepted. Individual message edits are validated server-side.
- Results show recipient states and messages, refreshing every ten seconds.
- The Python worker polls confirmed queued recipients, atomically claims and gates
  them, checks the exact Outlook account/mailbox, sends plain text via Composio
  **v3.1**, and records Microsoft acceptance as `submitted`.
- A persistent SQLite outbox stores send outcomes before attempting to finish in
  Supabase. Outbox retries never call Outlook.

The previously live-tested Langflow flow remains in `langflow/`. Its local sender
now matches the working v3.1 integration: the earlier 404 was resolved by changing
the API key, not by changing API version.

## 1. Supabase preparation

Apply migrations in order. Do not rerun migrations already applied:

1. `supabase/migrations/202611100001_outreach_foundation.sql` (already used by live tests)
2. `supabase/migrations/202611100002_outreach_send_gate.sql` (already used by live tests)
3. **New:** `supabase/migrations/202611100003_app_preview.sql`
4. `supabase/migrations/202611100004_outlook_self_service.sql`
5. `supabase/migrations/202611100005_contact_sorting.sql`
6. `supabase/migrations/202611110006_draft_editing.sql`
7. `supabase/migrations/202611110007_campaign_scheduling.sql`
8. `supabase/migrations/202611110008_email_delivery.sql`
9. `supabase/migrations/202611110009_attachment_storage.sql`
10. `supabase/migrations/202611160010_close_lead_sync.sql`

The third migration adds app request/exclusion snapshots and a service-only RPC
that serializes duplicate preview requests. Unchanged retries return the same
campaign even if source contacts have since changed. Edited input gets a new UUID.
The existing lifecycle functions still perform all submit/claim/gate/finish writes.

### Draft editing

Apply migration **006** and deploy the matching web build together. The migration
revokes the service role's old revision-free `submit_outreach_campaign` entry point;
confirmation in an older web build will be unavailable until the new build is live.
The web action now calls `confirm_outreach_revision` with the verified user, campaign,
and reviewed version. The worker's claim/send-gate/finish contract is unchanged.

On a draft preview, **Edit campaign** reopens the saved selection and templates;
**Save changes & review** updates that same campaign, reruns personalization and
eligibility checks, and refreshes exclusions. Customized messages are preserved
for retained leads with the same recipient email. Removed/re-added recipients and
changed email addresses receive the current template. **Edit message** changes one
recipient's filled-in subject/body; **Reset to campaign template** uses the latest
rendered template for that recipient. Legacy previews without `app_request` support
individual edits but cannot recover a composer selection.

`edit_outreach_draft` checks ownership and active membership, serializes edits and
confirmation on the campaign lock, and increments the version transactionally.
Unchanged retries reuse an edit ID; stale versions and conflicting edit IDs fail.
Opening an individual editor clears and disables the review checkbox, and saved
changes require a fresh review. Submitted campaign content stays immutable.

### Campaign scheduling

Apply migration **007 before deploying the matching web and worker builds**.
The migration enables `scheduled` campaigns and extends the claim RPC with an
atomic due-time check and transition into the existing submitted/send lifecycle.
Existing immediate campaigns still work. The old worker does not pick up scheduled
campaigns, so deploy the updated worker before users confirm schedules. No new
environment variables or cron service are needed.

On the saved preview choose **Schedule for later**, enter a date/time, and choose an
IANA time zone. The zone defaults to the browser's zone. The UI shows the exact
date, zone and UTC offset before **Confirm schedule**. Invalid/past times and DST
gaps are rejected; overlapping DST times require choosing the earlier/later instant.
The server independently resolves the wall-clock time and validates the instant.
Supabase stores the UTC send instant and chosen zone; all recipient `due_at` values
are updated transactionally.

Before the scheduled instant, **Reschedule** changes the time, **Cancel schedule**
returns the campaign to draft, and **Edit campaign** withdraws the schedule before
reopening the composer. Withdrawal restores recipients to `ready`, clears their
due times, preserves individual overrides, and increments the campaign revision.
Sending/scheduling again requires explicit confirmation. At the scheduled instant,
management controls lock even if the worker has not yet claimed a recipient.

The worker includes both due scheduled and submitted campaigns in its existing
keyset-paginated queue query. Database campaign locks serialize schedule changes
with the first claim; future schedules cannot be claimed. On the first eligible
claim the campaign becomes `submitted`, and the existing suppression, sender,
membership, send-gate and finish-only outbox checks continue to apply. An older tab
or a stale queue candidate cannot undo a later withdrawal/reschedule.

The scheduled time is an **earliest start**, not a guaranteed delivery time or a
simultaneous send for all recipients. The sequential worker starts overdue campaigns
when it resumes after downtime. Scheduling is one-time; recurring campaigns are not
implemented.

### CC and attachments

Apply **008 and 009 before deploying the updated worker and web app**. Migration
009 creates the private `outreach-attachments` Storage bucket with a 25,000,000-byte
file limit. No extra environment variables or Python dependencies are needed.
Deploy the worker before enabling the updated composer. Older workers/manual
senders can still send plain messages, but their legacy send gate rejects messages
with CC or attachments rather than silently dropping the reviewed content.

The composer stores a default CC list and attachment set for the campaign. Every
selected contact still receives a separate personalized email, so default CC
addresses receive one copy per selected contact. The individual review editor has
independent CC and attachment source selectors: `NULL` inherits defaults, while an
empty customized list explicitly means none. Changing defaults affects inheriting
recipients only. Subject/body reset does not reset delivery overrides. Overrides
survive campaign edits for retained contacts with the same email, and survive
schedule withdrawal. All delivery changes are revision-aware and require review.

There are up to 50 unique CC addresses and 20 distinct filenames per email. Effective
CC is case-normalized and deduplicated, with the primary To address removed.
Suppressed CC addresses block affected delivery rather than being silently omitted.
Combined raw attachment content is limited to **25 MB (25,000,000 bytes)** for each
recipient's effective list. Base64/MIME increases the actual message size, so the
connected Exchange mailbox's total message limit must allow the resulting encoded
message. The UI limit does not override Microsoft/provider limits.

Uploads go directly to a signed, unique staging object. The server moves bytes to
an immutable final object, verifies the actual size and records SHA-256 before a
file becomes selectable. Previously issued staging tokens cannot replace final
objects. Browser payloads contain attachment IDs, never arbitrary source URLs or
backend keys. Downloads require an active owner and return verified bytes as an
attachment. Unlinked uploads older than 24 hours are collected hourly by the worker;
files ever linked into a reviewed preview are retained for history, including after
being removed from a later draft revision. There is no automatic historical-file
purge. Keep Storage service keys private and do not manually overwrite final files.

Emails without attachments use the existing `sendMail` flow with `ccRecipients`.
Emails with attachments use an Outlook draft: small files (under 3 MB) are attached
in one POST; larger files use upload sessions with bounded binary PUT chunks to
Microsoft's pre-authenticated upload URL. The worker verifies both source checksums
and the prepared draft's recipient lists, subject/body and attachment checksums
before consuming the one-time dispatch gate. Normal Graph operations use Composio;
chunk PUTs use the returned Outlook URL directly without service credentials.

**Outlook permission prerequisite:** configure `Mail.ReadWrite` as well as `Mail.Send`
on the Composio Outlook auth configuration; retain the existing profile permission.
Reconnect accounts if their prior consent lacks the new scope. Validate draft
creation, attachment retrieval and upload-session operations through the actual
Composio connection before production acceptance. A configuration/API-key lookup
alone cannot establish an individual account's granted permissions.

Preparation progress (draft ID, upload URL/ranges and dispatch intent) is durable in
the same private SQLite database as the finish-only journal on `/data`. The worker
finds claimed but not dispatched attempts through `pending_outreach_deliveries` and
resumes the original attempt/token. A unique Outlook extended property reconciles
lost draft-creation responses. Lost final chunk/attachment responses are reconciled
against the original draft before another upload. If draft creation stays uncertain
and no matching draft can be found, preparation remains pending for operator
reconciliation; it does not blindly create and send another message. Expired upload
sessions can be replaced after checking the draft for completed attachments.

### Manual Close lead sync

Apply **010 before deploying the sync controls**, then deploy a dedicated background
service using `sync/Dockerfile`. The web app only queues a job and reads its shared
status; exports/imports run in `sync/worker.py`, separately from email dispatch.
The worker uses Python 3.13's standard library, with no Langflow/pandas dependency.

**Sync from Close** creates or joins the workspace's active sync. All active members
can start one and see its progress. A global partial unique index and an advisory
lock enforce one active job. Per-user request IDs are recorded even when the user
joins somebody else's job, so a lost response retried after completion does not
accidentally create another export. Browser reads expose progress fields, not lease
tokens, source manifests, API keys or download URLs.

The sync worker preserves the supplied **Close Lead Dump → Supabase Lead Sync**
behavior: request a full CSV lead export, poll its `expo_...` ID, decode gzip/UTF-8 BOM,
and map the primary contact fields into the existing `close_leads` table. Company
names prefer `lead_name` over `display_name`; `custom.Industry` supplies industry;
the entire source row is retained as strings in `source_data`, including original
`date_created`. Leads absent from an export are **retained**, not deleted. Unrelated
lead-table columns and saved campaign/recipient snapshots are not overwritten.

The entire CSV is checked for headers, nonempty IDs, duplicates and syntax before
any imports. Raw and decoded exports are capped at 256 MiB, and individual CSV
fields at 4 MiB. Mapped records and the CSV are privately cached on the sync worker's
volume. A SHA-256 manifest prevents mixing a different export into a partial import.
Every imported row uses one fixed UTC timestamp for the run. Counts mean **leads
refreshed**, including unchanged/existing leads, rather than newly inserted leads.

`import_close_lead_batch` upserts a batch and advances its progress in the **same
transaction**. Exact batch retries do not double-count. The worker saves the export
ID and resumes the confirmed offset after restart or a lost batch response. Leases
last 180 seconds and are renewed between requests/chunks when 30 seconds have passed;
every checkpoint and import checks the current token. An expired worker cannot
write after another worker takes over.

Network errors, rate limits and export wait timeouts retry with bounded backoff
(5–300 seconds, up to five failure events per run before a terminal failure).
**Retry failed sync** resumes the existing export and offset; **Sync from Close**
starts a fresh export after a terminal failure. An expired/unavailable old export
or changed manifest requires a fresh sync. The default export wait is 300 seconds;
a timed-out poll retains its export ID instead of restarting the dump.

Close credentials authenticate API requests and initial downloads on the four
known Close hosts. They are not forwarded through redirects or attached to external
storage URLs. The UI polls status every five seconds and refreshes contacts once
on successful completion, preserving selection, filters, subject/body, CC and files.
Closing the browser does not stop the job. Imports are batched: searches/previews
made during an import can read an already-updated subset, and a failed job can leave
confirmed batches in place. Previously reviewed campaign content stays frozen.

Completed cache files are removed immediately, with hourly cleanup for leftovers
from a lost completion response. Failed/active caches are retained for recovery;
keep the volume private because source CSV/JSONL files contain contact data. Logs
contain job IDs/counts rather than API keys, URLs, or lead details. The existing
manual Langflow importer remains usable but must not run concurrently with the
built-in sync, since its direct upserts do not participate in this worker's fencing.
This is manual one-time sync, not a recurring ingestion schedule.

### Contact sorting

Apply migration **005 before deploying the contact sorting UI**. Its read-only
`close_contact_directory` view respects the caller's existing lead-table RLS
(`security_invoker`, PostgreSQL 15+). It reads `source_data.date_created` as a
timestamp with time zone; missing or invalid dates become null. It does not
rewrite lead records or use `last_synced_at`. Future imports appear automatically.

The composer supports Name and Company A–Z/Z–A, plus Date created in Close
newest/oldest first. Default is Name A–Z. Search, ordering, and pagination execute
in the database. Names/companies are trimmed and case-insensitive for sorting,
missing values stay last, and lead ID breaks ties. Changing search or ordering
resets the page, retaining selected contacts and composed messages. The displayed
creation date is UTC and refers to the Close lead, not its individual contact.

The existing `close_leads` table is shared with the manual sync worker and Flow 1.
Contact sorting itself does not introduce an ingestion schedule.

### Invite-only Auth

In Supabase Authentication settings:

1. **Disable new user signups.** Keep email/password authentication enabled.
2. Set Site URL to the deployed web origin, e.g. `https://your-app.up.railway.app`.
3. Allow your production origin and `http://localhost:3000` with the appropriate
   auth paths in Redirect URLs. Set `APP_URL` on the web service to the same origin.
4. Configure working SMTP/email delivery and appropriate Auth rate limits.
5. Customize **Invite user** email link to:

   ```html
   <a href="{{ .SiteURL }}/auth/confirm?token_hash={{ .TokenHash }}&type=invite">Accept invitation</a>
   ```

6. Customize **Reset password** email link to:

   ```html
   <a href="{{ .SiteURL }}/auth/confirm?token_hash={{ .TokenHash }}&type=recovery">Reset password</a>
   ```

These templates are required: this app exchanges a token hash server-side and
does not consume Supabase's default URL-fragment session links. For local testing,
temporarily use a local Site URL or a separate development project.

Invite users from the Supabase dashboard. Add an active `outreach_members` row for
each invited Auth UUID before they use the app. There is no public signup UI or
signup action. Existing users and memberships can sign in as-is.

### Self-service Outlook connections

Invited members open **Manage Outlook** on the dashboard or `/settings/outlook`
to connect, reconnect, check, or disconnect their mailbox. The Microsoft mailbox
may differ from the app login email; Graph `/me` supplies the read-only sender.

Apply migration **004 before deploying the self-service web changes**. It adds
stable Composio identities, short-lived authorization attempts, and a durable
disable-only cleanup queue. Existing manually registered identities and connection
row IDs are preserved, including the IDs referenced by saved campaigns.

Set these **server-only web-service variables** (the worker keeps its current key):

```dotenv
COMPOSIO_API_KEY=<same-working-Composio-project-key>
COMPOSIO_OUTLOOK_AUTH_CONFIG_ID=<Outlook-OAuth2-auth-config-ID>
```

Use an Outlook OAuth2 auth configuration that supports Graph `/me` and delegated
`Mail.Send`. Microsoft tenant consent requirements still apply. The application
return URL is `${APP_URL}/settings/outlook/callback`; Composio handles Microsoft's
OAuth exchange itself. If your Composio configuration restricts application return
URLs, allow this route for each environment. It is separate from Supabase Auth's
invitation/reset callback. For local use, set `APP_URL=http://localhost:3000`.

The server uses Composio v3.1 **Connect Link**, saves the returned exact connected
account ID, and binds the return to the member, auth config, random single-use
state, and initiating browser cookie. The pending attempt expires after ten
minutes. Starting a newer attempt supersedes the old one; starts are limited to
one per 30 seconds. The server verifies the provider owner, toolkit, active/enabled
state, auth config, and Graph mailbox before switching the mapping. No OAuth
tokens are persisted in Supabase or passed to application client components.

Reconnect creates a replacement account and switches only after verification.
Disconnect blocks local send authorization first. Replaced, disconnected, and
cancelled accounts enter the disable-only cleanup queue; successful callback,
cancel, and disconnect actions attempt cleanup. **Retry provider cleanup** handles
provider outages or interrupted cleanup from the settings page. A cancelled
in-flight link creation is queued for cleanup when its account ID returns. If the
provider created an account but the response was lost before its ID was saved,
an administrator may need to remove that unlinked account in Composio.

Existing campaigns retain their exact sender snapshots. A changed or disconnected
sender blocks draft confirmation and new claim/send-gate authorization. The
campaign page explains why queued recipients are blocked; create a new reviewed
preview for unsent messages. Already-dispatching sends may complete. History and
ambiguous outcomes are never reset automatically.

Live acceptance: connect a second invited user, verify a mailbox different from
their login email, submit one reviewed test message, then exercise cancellation,
reconnect, disconnect, and provider-cleanup retries. Confirm each user can manage
only their own connection and campaigns.

## 2. Local web development

Use Node.js 22 or newer. In `web/`:

```sh
npm ci
npm run dev
```

Create `web/.env.local` using `web/.env.example` as the field list. The public
Supabase URL/key go to the browser; `SUPABASE_SECRET_KEY` never does. The app uses
server actions for mutations, with Next.js's same-origin protections. Do not
expose backend keys or any arbitrary-RPC endpoint to the browser.

## 3. Railway deployment

Connect this repository to a Railway project with **three separate services**:
web, email worker, and Close sync worker.
Keep the repository root as the build context for both Dockerfiles.

The current Railway API rejects the legacy `railway.toml` config-file setting.
Configure the settings below directly on each service. The TOML files in this
repository are legacy references; this deployment does not load them.

### Web service

- Dockerfile: `web/Dockerfile`
- Source: this GitHub repository, branch `main`; root directory `/`.
- Restart policy: on failure; one replica.
- Generate a public domain; the process binds to Railway's `PORT`.
- Health check: `/api/health` (process liveness, not database readiness).
- Variables: all fields in `web/.env.example`.
- `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` must be
  available at **build time** as well as runtime. The Dockerfile declares them as
  build args. Changing these requires rebuilding the web service.
- `APP_URL` must be the exact HTTPS public web origin without a trailing slash.
- Set Supabase Site URL and email templates after assigning the public domain.

### Worker service

- Dockerfile: `worker/Dockerfile`
- Source: this GitHub repository, branch `main`; root directory `/`.
- Restart policy: on failure, maximum three retries; disable application sleeping.
- No public domain or HTTP health check; it is a background process.
- Attach a **persistent Railway volume mounted at `/data`**.
- Variables: all fields in `worker/.env.example`.
- Use `OUTCOME_DB_PATH=/data/outcomes.sqlite`. The directory must already exist.
- Start with **one replica** and no horizontal autoscaling. Keep the volume
  attached across deploys and restarts; do not delete it with pending outcomes.
- The worker polls every five seconds and sends sequentially with one second
  between candidates. It starts processing existing queued rows as soon as deployed.

Before starting the worker, inspect queued campaigns and undispatched claimed
attempts left by manual tests. Due queued recipients and pending preparations may
be processed immediately.
Do not run the old manual sender for the same queued workload.

### Close sync service

- Dockerfile: `sync/Dockerfile`; repository-root build context `/`, source `main`.
- Run as a dedicated background service, not inside the email worker or web process.
- Variables: all fields in `sync/.env.example`. `CLOSE_API_KEY` is a Close key with
  lead-export access; it stays on this service and is not needed by the web app.
- Attach its own persistent volume at `/data`; use `SYNC_CACHE_DIR=/data/close-sync`.
- One replica, application sleeping disabled, restart on failure. No public domain
  or HTTP health check is required. Startup logs contain `sync_worker_started`.
- Default batch size is 200 (configurable 1–1000); export polling waits up to 300
  seconds per attempt and retains the export ID for retries.
- Deployment order: apply migration 010, deploy the sync worker with the Close key
  and volume, then deploy the updated web app. The email worker does not need to
  change for this feature. Verify the global directory uses the intended Close
  organization before initiating a full sync.

To run locally, export `sync/.env.example`'s variables, point `SYNC_CACHE_DIR` at a
private persistent directory, and run `python -B sync/worker.py`.

### Local email worker

Python 3.13, standard library only; no pip dependencies. Export the fields from
`worker/.env.example`, set `OUTCOME_DB_PATH` to an existing persistent directory,
and run from the repository root:

```sh
python -B worker/worker.py
```

## Recovery semantics

- Queue state is in Supabase. HTTP requests never retry provider sends.
- Claim and send-gate RPCs prevent two executions from sending the same attempt.
- A provider response is written to the SQLite outbox before finish is attempted.
  A failed finish remains in the outbox and is retried after restart. The existing
  finish RPC accepts identical retries.
- A crash after claiming can resume the same pre-dispatch attempt; it is not
  reclaimed with a new token. Once local dispatch intent is recorded, a crash or
  lost gate response is recorded as `unknown` on recovery, without issuing a send.
  If the volume is lost, unresolved gated attempts require operator reconciliation.
  There is no cross-system exactly-once guarantee.
- Lost/malformed/5xx send responses become `unknown`; no automatic resend.
- A definite Graph 4xx rejection except 408 becomes `failed`.
- Gate rejection (suppression, inactive sender/member) stops execution; inspect the
  affected row. No automatic reset to queued is performed.
- Never clear a dispatch marker, delete an attempt, or reset a claimed row merely
  to retry a send. After a proven pre-send failure, create a new reviewed campaign.

Worker logs contain recipient/attempt IDs and statuses, not secrets, claim tokens,
or message bodies. SQLite contains sensitive claim tokens and upload-session URLs;
keep the volume private. RPC authorization and RLS remain essential even though
the frontend hides controls. Composio's deprecated account `user_id` response field
is currently required for the tested ownership check; omission fails closed.

## Verification

```sh
# In web/
npm test
npm run build

# In repository root
python -B -m unittest discover -s tests
```

SQL syntax checks use optional `pglast`; they do not verify deployed function bodies,
RLS, or concurrency. Worker tests use fake APIs and a real temporary SQLite file.
Web tests also run the migrations and the Outlook lifecycle functions in
PGlite (embedded PostgreSQL), exercising privileges, ownership, expiry, superseded
callbacks, cleanup, and campaign snapshots. This does not replace live Supabase
or concurrent multi-session acceptance testing.

### Required live acceptance checks

1. Invite a test user, set password, sign in, log out, and reset password.
2. Confirm a non-member is blocked and public signup is disabled.
3. Create one preview; verify exact personalized message, sender, and exclusions.
4. Retry unchanged creation; confirm no duplicate campaign. Test changed input.
   Before sending, edit the campaign selection/templates and an individual message;
   verify override preservation/reset, cancelled edits, and unchanged save retries.
   Open two tabs; save in one and confirm the older tab rejects stale edits/sending.
5. Confirm the saved preview; verify worker sends once and records Graph 202.
6. Restart the worker with a pending recording receipt; verify finish-only recovery.
7. Repeat confirmation and test a second worker against the same queued recipient
   in a controlled integration environment; verify one successful claim/send gate.
8. Test suppression and sender revocation after preview.
9. Sign in as a second member; verify shared contact access but no cross-user
   campaign reads/actions. Verify different Outlook mailboxes are isolated.
10. Run a small controlled multi-recipient campaign and inspect recipient states.
11. Schedule a reviewed test campaign a few minutes ahead. Verify zero claims before
    its time, then Microsoft acceptance afterwards. Before its time, exercise
    rescheduling and withdrawal/editing; confirm stale tabs/candidates cannot send
    or alter the newer version. In a controlled environment, stop and resume the
    worker across the due time to verify overdue pickup and test concurrent
    schedule management versus first claim.
12. Upload a small file and a large file close to the 25 MB combined limit. Verify
    private downloads, exact received bytes, effective CC and attachment overrides,
    and independent resets to defaults. Exercise scheduling and withdrawal with files.
    In a controlled mailbox, interrupt a chunk upload, restart the worker, and verify
    one draft is resumed and sent once. Verify Mail.ReadWrite consent and Exchange
    message limits, and test cross-user download/upload denials and suppressed CC.
13. Start Sync from Close as an active member and verify the same running job is
    shown to a second member. Create/update a controlled Close lead and verify its
    primary contact, company, source creation date and counts after completion.
    Confirm repeated syncs do not duplicate IDs, and that saved campaign messages
    are unchanged. Stop the sync worker during import, restart it, and verify
    checkpoint recovery, a single successful completion, and composer state retention.

Railway deployment, credentials, live Auth redirects, live RLS/concurrency, and
two-user mailbox isolation must be verified in your environment. Self-service
Outlook code is implemented; production OAuth acceptance requires migration 004,
the web-service Composio variables, and the live checks above. Richer
reconciliation/reporting remains a follow-up.
