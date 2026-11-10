# Close Outreach — Railway MVP

Next.js App Router + TypeScript, invite-only Supabase Auth, existing Supabase
campaign lifecycle RPCs, and a separate sequential Python worker on Railway.

## Implemented milestone

- Invited users accept an email link, set a password, sign in, reset passwords, and sign out.
- Protected reads use the authenticated Supabase client and existing RLS. Every
  write action verifies the session with `getUser()` and checks active membership.
- Shared contact search, pagination, and selections up to 1,000 recipients.
- Server-side personalization, suppression checks, deduplication and immutable
  saved previews. Exclusions are persisted alongside the app request.
- A distinct confirmation action submits the saved campaign. No caller-controlled
  identity, sender, rendered messages, or Langflow inputs are accepted.
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

The third migration adds app request/exclusion snapshots and a service-only RPC
that serializes duplicate preview requests. Unchanged retries return the same
campaign even if source contacts have since changed. Edited input gets a new UUID.
The existing lifecycle functions still perform all submit/claim/gate/finish writes.

Keep the existing `close_leads` table and Flow 1 ingestion. No recurring ingestion
or scheduled campaigns are introduced.

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

### Outlook connections

The MVP uses the existing administrator-registered `outlook_connections` mapping.
Each active user needs their own verified mailbox/entity/account mapping. The
dashboard displays connection status. Self-service OAuth onboarding/reconnection
is a follow-up, not implemented in this milestone. Use the **correct Composio
project API key** and retain the existing verified connection for the first test.

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

Connect this repository to a Railway project with **two separate services**.
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

Before starting the worker, inspect any queued campaigns left by manual tests.
Only submitted/queued recipients are eligible, but they may be processed immediately.
Do not run the old manual sender for the same queued workload.

### Local worker

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
- A crash after claiming, after consuming the gate, or after provider execution
  but before the local journal commit can leave `sending`. The worker never
  reclaims that attempt. The UI shows its unresolved state; operator reconciliation
  is required. There is no cross-system exactly-once guarantee.
- Lost/malformed/5xx send responses become `unknown`; no automatic resend.
- A definite Graph 4xx rejection except 408 becomes `failed`.
- Gate rejection (suppression, inactive sender/member) stops execution; inspect the
  affected row. No automatic reset to queued is performed.
- Never clear a dispatch marker, delete an attempt, or reset a claimed row merely
  to retry a send. After a proven pre-send failure, create a new reviewed campaign.

Worker logs contain recipient/attempt IDs and statuses, not secrets, claim tokens,
or message bodies. SQLite contains sensitive claim tokens for pending finishes;
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

### Required live acceptance checks

1. Invite a test user, set password, sign in, log out, and reset password.
2. Confirm a non-member is blocked and public signup is disabled.
3. Create one preview; verify exact personalized message, sender, and exclusions.
4. Retry unchanged creation; confirm no duplicate campaign. Test changed input.
5. Confirm the saved preview; verify worker sends once and records Graph 202.
6. Restart the worker with a pending recording receipt; verify finish-only recovery.
7. Repeat confirmation and test a second worker against the same queued recipient
   in a controlled integration environment; verify one successful claim/send gate.
8. Test suppression and sender revocation after preview.
9. Sign in as a second member; verify shared contact access but no cross-user
   campaign reads/actions. Verify different Outlook mailboxes are isolated.
10. Run a small controlled multi-recipient campaign and inspect recipient states.

Railway deployment, credentials, live Auth redirects, live RLS/concurrency, and
two-user mailbox isolation must be verified in your environment. The next feature
milestone is self-service Outlook onboarding and richer reconciliation/reporting.
