import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { PGlite } from "@electric-sql/pglite";

test("CC and attachment snapshots enforce inheritance, ownership, scheduling, dispatch and cleanup", async t => {
  const db = new PGlite(), user = randomUUID(), other = randomUUID();
  try {
    await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
      CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid primary key);
      CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS 'SELECT NULL::uuid';
      CREATE TABLE public.close_leads(close_lead_id text primary key);`);
    await db.query("INSERT INTO auth.users VALUES ($1),($2)", [user, other]);
    for (const file of ["202611100001_outreach_foundation.sql", "202611100002_outreach_send_gate.sql", "202611100003_app_preview.sql", "202611110006_draft_editing.sql", "202611110007_campaign_scheduling.sql", "202611110008_email_delivery.sql"]) await db.exec(await readFile(new URL(`../../supabase/migrations/${file}`, import.meta.url), "utf8"));
    await db.query("INSERT INTO outreach_members(user_id) VALUES ($1),($2)", [user, other]);
    await db.query("INSERT INTO outlook_connections(user_id,composio_entity_id,composio_connected_account_id,mailbox_email) VALUES($1,'entity','account','sender@example.com')", [user]);
    await db.exec("INSERT INTO close_leads VALUES('lead_A'),('lead_B')");
    async function upload(owner = user, size = 8, name = "overview.pdf") {
      const id = randomUUID();
      const args = [owner, id, name, size, "application/pdf"];
      await db.query("SELECT register_outreach_attachment($1,$2,$3,$4,$5)", args);
      await db.query("SELECT register_outreach_attachment($1,$2,$3,$4,$5,$6)", [...args, "a".repeat(64)]);
      return id;
    }
    const a = await upload(), b = await upload(user, 8, "proposal.pdf"), foreign = await upload(other);
    const request = (ids = [a], cc = ["manager@example.com"]) => ({ requestId: randomUUID(), leadIds: ["lead_A", "lead_B"], subject: "Subject", body: "Body", cc, attachmentIds: ids });
    const recipients = ["A", "B"].map(c => ({ close_lead_id: `lead_${c}`, recipient_email: `${c.toLowerCase()}@example.com`, rendered_subject: "Subject", rendered_body: "Body" }));
    async function create(input = request()) {
      const id = (await db.query<{ id: string }>("SELECT prepare_app_preview($1,$2,(SELECT id FROM outlook_connections WHERE user_id=$1),'Subject','Body',$3,$4,'[]') AS id", [user, input.requestId, JSON.stringify(recipients), JSON.stringify(input)])).rows[0].id;
      const rows = (await db.query<{ id: string }>("SELECT id FROM campaign_recipients WHERE campaign_id=$1 ORDER BY recipient_email", [id])).rows;
      return { id, r: rows.map(r => r.id) };
    }
    const edit = (id: string, rev: number, payload: object) => db.query("SELECT edit_outreach_draft($1,$2,$3,$4)", [user, id, rev, JSON.stringify(payload)]);
    const override = (recipientId: string, cc: string[] | null, attachmentIds: string[] | null) => ({ operation: "delivery", editId: randomUUID(), recipientId, cc, attachmentIds });
    await t.test("defaults follow campaign edits while null and empty overrides remain distinct", async () => {
      const { id, r } = await create();
      const change = override(r[0], [], [b]);
      await edit(id, 1, change); await edit(id, 1, change);
      assert.equal((await db.query<{ revision: number }>("SELECT revision FROM campaigns WHERE id=$1", [id])).rows[0].revision, 2);
      await assert.rejects(edit(id, 1, override(r[1], null, [])), /Draft changed/);
      await edit(id, 2, { operation: "campaign", editId: randomUUID(), request: request([b], ["new@example.com"]), recipients, exclusions: [] });
      const rows = (await db.query<{ cc_override: string[] | null; attachment_ids_override: string[] | null }>("SELECT cc_override,attachment_ids_override FROM campaign_recipients WHERE campaign_id=$1 ORDER BY recipient_email", [id])).rows;
      assert.deepEqual(rows[0], { cc_override: [], attachment_ids_override: [b] });
      assert.deepEqual(rows[1], { cc_override: null, attachment_ids_override: null });
      assert.deepEqual((await db.query<{ default_cc: string[] }>("SELECT default_cc FROM campaigns WHERE id=$1", [id])).rows[0].default_cc, ["new@example.com"]);
      await edit(id, 3, override(r[0], null, []));
      await db.query("SELECT manage_outreach_schedule($1,$2,4,$3,'schedule',$4,'UTC')", [user, id, randomUUID(), new Date(Date.now() + 3600000).toISOString()]);
      await assert.rejects(edit(id, 5, override(r[0], [], [])), /not a draft/);
      await assert.rejects(db.query("UPDATE campaign_recipients SET cc_override='{}' WHERE id=$1", [r[0]]), /immutable/);
      await db.query("SELECT manage_outreach_schedule($1,$2,5,$3,'withdraw')", [user, id, randomUUID()]);
      assert.deepEqual((await db.query<{ attachment_ids_override: string[] }>("SELECT attachment_ids_override FROM campaign_recipients WHERE id=$1", [r[0]])).rows[0].attachment_ids_override, []);
    });
    await t.test("files must be finalized, owned, distinct and within the effective 25 MB limit", async () => {
      for (const cc of [["bad"], ["a..b@example.com"], ["a@example.com", "a@example.com"], ["a@-bad.com"], ["a@example.com\r\nBcc:b@example.com"]]) await assert.rejects(create(request([], cc)), /CC/);
      await assert.rejects(create(request([foreign])), /not owned/);
      const pending = randomUUID(); await db.query("SELECT register_outreach_attachment($1,$2,'pending.pdf',8,'application/pdf')", [user, pending]);
      await assert.rejects(create(request([pending])), /unavailable/);
      const big = await upload(user, 25_000_000, "big.pdf");
      await assert.rejects(create(request([big, a])), /25 MB/);
      const duplicate = await upload(user, 8, "OVERVIEW.PDF");
      await assert.rejects(create(request([a, duplicate])), /filenames/);
      const { id, r } = await create();
      await assert.rejects(edit(id, 1, override(r[0], null, [foreign])), /not owned/);
      await assert.rejects(db.query("SELECT register_outreach_attachment($1,$2,'overview.pdf',8,'application/pdf',$3)", [user, a, "b".repeat(64)]), /File changed/);
    });
    await t.test("preparation is repeatable, legacy senders are blocked, and final dispatch is one-time", async () => {
      const { id, r } = await create(request([a], ["a@example.com", "manager@example.com"]));
      await db.query("SELECT confirm_outreach_revision($1,$2,1)", [user, id]);
      const claim = (await db.query<{ attempt_id: string; claim_token: string }>("SELECT * FROM claim_outreach_recipient($1,$2,$3)", [user, id, r[0]])).rows[0];
      const args = [user, claim.attempt_id, claim.claim_token];
      const job = (await db.query<{ job: { cc: string[]; attachments: { sha256: string; id: string }[] } }>("SELECT prepare_outreach_delivery($1,$2,$3) AS job", args)).rows[0].job;
      assert.deepEqual(job.cc, ["manager@example.com"]); assert.equal(job.attachments[0].id, a); assert.equal(job.attachments[0].sha256, "a".repeat(64));
      await db.query("SELECT prepare_outreach_delivery($1,$2,$3)", args);
      await assert.rejects(db.query("SELECT begin_outreach_send($1,$2,$3)", args), /Updated delivery worker/);
      assert.equal((await db.query<{ dispatch_started_at: unknown }>("SELECT dispatch_started_at FROM outreach_attempts WHERE id=$1", [claim.attempt_id])).rows[0].dispatch_started_at, null);
      assert.equal((await db.query<{ job: { authorized: boolean } }>("SELECT dispatch_outreach_delivery($1,$2,$3) AS job", args)).rows[0].job.authorized, true);
      assert.equal((await db.query<{ job: { authorized: boolean } }>("SELECT dispatch_outreach_delivery($1,$2,$3) AS job", args)).rows[0].job.authorized, false);
      await db.exec("INSERT INTO email_suppressions(normalized_email,reason) VALUES('manager@example.com','test')");
      // Recording an already-dispatched outcome must still work after suppression.
      await db.query("SELECT finish_outreach_attempt($1,$2,$3,'submitted',202)", args);
      const second = (await db.query<{ attempt_id: string; claim_token: string }>("SELECT * FROM claim_outreach_recipient($1,$2,$3)", [user, id, r[1]])).rows[0];
      await assert.rejects(db.query("SELECT prepare_outreach_delivery($1,$2,$3)", [user, second.attempt_id, second.claim_token]), /suppressed/);
      await db.exec("DELETE FROM email_suppressions");
    });
    await t.test("cleanup protects linked files and browser roles cannot modify uploads or dispatch", async () => {
      const abandoned = await upload(user, 8, "abandoned.pdf");
      await db.exec("UPDATE outreach_attachments SET created_at=now()-interval '2 days'");
      const gc = (await db.query<{ id: string }>("SELECT id FROM collect_outreach_uploads()")).rows.map(r => r.id);
      assert.ok(gc.includes(abandoned)); assert.ok(!gc.includes(a));
      await assert.rejects(create(request([abandoned])), /unavailable/);
      await db.exec("SET ROLE authenticated");
      await assert.rejects(db.query("SELECT register_outreach_attachment($1,$2,'x.pdf',8,'application/pdf')", [user, randomUUID()]), /permission denied/);
      await assert.rejects(db.query("SELECT pending_outreach_deliveries()"), /permission denied/);
      await db.exec("RESET ROLE; SET ROLE service_role");
      await assert.rejects(db.query("UPDATE outreach_attachments SET name='changed'"), /permission denied/);
      await assert.rejects(db.query("SELECT begin_outreach_dispatch($1,$2,$3)", [user, randomUUID(), randomUUID()]), /permission denied/);
      await db.exec("RESET ROLE");
    });
    await t.test("a newly suppressed default CC blocks only affected scheduled deliveries", async () => {
      const { id, r } = await create();
      await edit(id, 1, override(r[0], [], null));
      await db.query("SELECT manage_outreach_schedule($1,$2,2,$3,'schedule',$4,'UTC')", [user, id, randomUUID(), new Date(Date.now() + 3600000).toISOString()]);
      await db.exec("INSERT INTO email_suppressions(normalized_email,reason) VALUES('manager@example.com','test')");
      await db.query("UPDATE campaigns SET scheduled_at=now()-interval '1 minute' WHERE id=$1", [id]);
      await db.query("UPDATE campaign_recipients SET due_at=now()-interval '1 minute' WHERE campaign_id=$1", [id]);
      const first = (await db.query<{ attempt_id: string; claim_token: string }>("SELECT * FROM claim_outreach_recipient($1,$2,$3)", [user, id, r[0]])).rows[0];
      assert.ok(first);
      const job = (await db.query<{ job: { authorized: boolean; cc: string[] } }>("SELECT prepare_outreach_delivery($1,$2,$3) AS job", [user, first.attempt_id, first.claim_token])).rows[0].job;
      assert.equal(job.authorized, true); assert.deepEqual(job.cc, []);
      const second = (await db.query<{ attempt_id: string; claim_token: string }>("SELECT * FROM claim_outreach_recipient($1,$2,$3)", [user, id, r[1]])).rows[0];
      await assert.rejects(db.query("SELECT dispatch_outreach_delivery($1,$2,$3)", [user, second.attempt_id, second.claim_token]), /suppressed/);
      await db.query("SELECT finish_outreach_attempt($1,$2,$3,'failed',NULL,NULL,'CC blocked')", [user, second.attempt_id, second.claim_token]);
      await db.exec("DELETE FROM email_suppressions");
    });
    await t.test("private storage exposes only finalized files owned by the active member", async () => {
      await db.exec(`CREATE SCHEMA storage;
        CREATE TABLE storage.buckets(id text primary key,name text,public boolean,file_size_limit integer);
        CREATE TABLE storage.objects(id uuid primary key default gen_random_uuid(),bucket_id text,name text);
        ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
        GRANT USAGE ON SCHEMA storage,auth TO authenticated;
        GRANT SELECT,INSERT,UPDATE,DELETE ON storage.objects TO authenticated;
        CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS $$ SELECT nullif(current_setting('test.user_id',true),'')::uuid $$;`);
      await db.exec(await readFile(new URL("../../supabase/migrations/202611110009_attachment_storage.sql", import.meta.url), "utf8"));
      const theirs = await upload(other, 8, "their-file.pdf");
      await db.query("INSERT INTO storage.objects(bucket_id,name) VALUES('outreach-attachments',$1),('outreach-attachments',$2),('outreach-attachments',$3)", [`files/${user}/${a}`, `files/${other}/${theirs}`, `staging/${user}/${a}`]);
      assert.equal((await db.query<{ public: boolean }>("SELECT public FROM storage.buckets")).rows[0].public, false);
      await db.query("SELECT set_config('test.user_id',$1,false)", [user]);
      await db.exec("SET ROLE authenticated");
      assert.deepEqual((await db.query<{ name: string }>("SELECT name FROM storage.objects")).rows.map(row => row.name), [`files/${user}/${a}`]);
      await assert.rejects(db.query("INSERT INTO storage.objects(bucket_id,name) VALUES('outreach-attachments','files/bypass')"), /row-level security/);
      await db.exec("RESET ROLE");
    });
  } finally { await db.close(); }
});
