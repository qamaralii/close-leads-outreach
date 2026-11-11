import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { PGlite } from "@electric-sql/pglite";

test("scheduled campaigns use durable due times, revision checks and serialized claim/withdraw gates", async t => {
  const db = new PGlite(), user = randomUUID(), other = randomUUID();
  try {
    await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
      CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid primary key);
      CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS 'SELECT NULL::uuid';
      CREATE TABLE public.close_leads(close_lead_id text primary key);`);
    await db.query("INSERT INTO auth.users VALUES ($1),($2)", [user, other]);
    for (const file of ["202611100001_outreach_foundation.sql", "202611100002_outreach_send_gate.sql", "202611100003_app_preview.sql", "202611110006_draft_editing.sql", "202611110007_campaign_scheduling.sql"]) {
      await db.exec(await readFile(new URL(`../../supabase/migrations/${file}`, import.meta.url), "utf8"));
    }
    await db.query("INSERT INTO outreach_members(user_id) VALUES ($1),($2)", [user, other]);
    await db.query("INSERT INTO outlook_connections(user_id,composio_entity_id,composio_connected_account_id,mailbox_email) VALUES ($1,'entity','account','sender@example.com')", [user]);
    await db.exec("INSERT INTO close_leads VALUES ('lead_A'),('lead_B')");
    async function create() {
      const id = (await db.query<{ id: string }>(`SELECT prepare_app_preview($1,$2,(SELECT id FROM outlook_connections WHERE user_id=$1),'Subject','Body',$3,'{}','[]') AS id`, [user, randomUUID(), JSON.stringify(["A", "B"].map(c => ({ close_lead_id: `lead_${c}`, recipient_email: `${c}@example.com`, rendered_subject: "Subject", rendered_body: "Body" })))])).rows[0].id;
      const recipient = (await db.query<{ id: string }>("SELECT id FROM campaign_recipients WHERE campaign_id=$1 ORDER BY id LIMIT 1", [id])).rows[0].id;
      return { id, recipient };
    }
    const future = () => new Date(Date.now() + 3600000).toISOString();
    const op = (id: string, revision: number, operation: string, at: string | null = null, zone: string | null = null, token = randomUUID(), owner = user) => db.query("SELECT manage_outreach_schedule($1,$2,$3,$4,$5,$6,$7)", [owner, id, revision, token, operation, at, zone]);
    const claim = (id: string, recipient: string) => db.query<{ attempt_id: string; claim_token: string }>("SELECT * FROM claim_outreach_recipient($1,$2,$3)", [user, id, recipient]);
    const state = async (id: string) => (await db.query<{ status: string; revision: number; scheduled_at: string | null; last_schedule: object }>("SELECT * FROM campaigns WHERE id=$1", [id])).rows[0];
    // Administrator-only fixture clock advancement, not an application operation.
    async function due(id: string) { await db.query("UPDATE campaigns SET scheduled_at=now()-interval '1 minute' WHERE id=$1", [id]); await db.query("UPDATE campaign_recipients SET due_at=now()-interval '1 minute' WHERE campaign_id=$1", [id]); }
    await t.test("schedules atomically, retries without duplicates, and cannot claim or authorize early", async () => {
      const { id, recipient } = await create(), at = future(), token = randomUUID();
      await assert.rejects(op(id, 1, "schedule", at, "UTC", token, other), /unavailable/);
      await assert.rejects(op(id, 1, "schedule", "2020-01-01T00:00:00Z", "UTC"), /future/);
      await assert.rejects(op(id, 1, "schedule", at, "Bad/Zone"), /time zone/);
      await op(id, 1, "schedule", at, "Asia/Kolkata", token); await op(id, 1, "schedule", at, "Asia/Kolkata", token);
      assert.equal((await state(id)).revision, 2); assert.equal((await state(id)).status, "scheduled");
      const recipients = (await db.query<{ due_at: string; status: string }>("SELECT due_at,status FROM campaign_recipients WHERE campaign_id=$1", [id])).rows;
      assert.ok(recipients.every(r => r.status === "queued" && new Date(r.due_at).getTime() === Date.parse(at)));
      assert.equal((await claim(id, recipient)).rows.length, 0);
      assert.equal((await db.query("SELECT * FROM outreach_attempts")).rows.length, 0);
      await assert.rejects(db.query("SELECT confirm_outreach_revision($1,$2,1)", [user, id]), /Draft changed/);
      await assert.rejects(db.query("UPDATE campaign_recipients SET rendered_body='Changed' WHERE id=$1", [recipient]), /immutable/);
      await assert.rejects(db.query("UPDATE campaigns SET subject_template='Changed' WHERE id=$1", [id]), /immutable/);
    });
    await t.test("rescheduling updates all due times; withdrawal restores draft and preserves overrides", async () => {
      const { id, recipient } = await create();
      await db.query("SELECT edit_outreach_draft($1,$2,1,$3)", [user, id, JSON.stringify({ operation: "message", editId: randomUUID(), recipientId: recipient, subject: "Personal", body: "Custom body" })]);
      await op(id, 2, "schedule", future(), "UTC");
      await assert.rejects(op(id, 2, "reschedule", future(), "UTC"), /changed/);
      const at = new Date(Date.now() + 7200000).toISOString();
      await op(id, 3, "reschedule", at, "America/New_York");
      assert.ok((await db.query<{ due_at: string }>("SELECT due_at FROM campaign_recipients WHERE campaign_id=$1", [id])).rows.every(r => new Date(r.due_at).getTime() === Date.parse(at)));
      const token = randomUUID(); await op(id, 4, "withdraw", null, null, token); await op(id, 4, "withdraw", null, null, token);
      assert.equal((await state(id)).status, "draft"); assert.equal((await state(id)).revision, 5);
      assert.ok((await db.query<{ status: string; due_at: string | null }>("SELECT status,due_at FROM campaign_recipients WHERE campaign_id=$1", [id])).rows.every(r => r.status === "ready" && r.due_at === null));
      const saved = (await db.query<{ customized: boolean; rendered_subject: string }>("SELECT * FROM campaign_recipients WHERE id=$1", [recipient])).rows[0];
      assert.equal(saved.customized, true); assert.equal(saved.rendered_subject, "Personal");
      assert.equal((await claim(id, recipient)).rows.length, 0); // Stale queue candidate after withdrawal.
      await assert.rejects(db.query("SELECT confirm_outreach_revision($1,$2,2)", [user, id]), /Draft changed/);
      await db.query("SELECT confirm_outreach_revision($1,$2,5)", [user, id]); // Send-now still works.
      assert.equal((await state(id)).status, "submitted");
    });
    await t.test("due-time cutoff and first claim lock the campaign; replayed candidates do not create attempts twice", async () => {
      const { id, recipient } = await create(); await op(id, 1, "schedule", future(), "UTC"); await due(id);
      await assert.rejects(op(id, 2, "withdraw"), /locked/);
      await assert.rejects(op(id, 2, "reschedule", future(), "UTC"), /locked/);
      const job = (await claim(id, recipient)).rows[0]; assert.ok(job);
      assert.equal((await state(id)).status, "submitted");
      assert.equal((await claim(id, recipient)).rows.length, 0);
      await assert.rejects(op(id, 2, "withdraw"), /already started/);
      const gate = (await db.query<{ job: { authorized: boolean } }>("SELECT begin_outreach_send($1,$2,$3) AS job", [user, job.attempt_id, job.claim_token])).rows[0].job;
      assert.equal(gate.authorized, true);
    });
    await t.test("sender revocation rolls back claim transition and suppression prevents a send", async () => {
      const { id, recipient } = await create(); await op(id, 1, "schedule", future(), "UTC"); await due(id);
      await db.query("UPDATE outlook_connections SET status='disconnected' WHERE user_id=$1", [user]);
      await assert.rejects(claim(id, recipient), /sender/); assert.equal((await state(id)).status, "scheduled");
      await db.query("UPDATE outlook_connections SET status='active' WHERE user_id=$1", [user]);
      await db.query("INSERT INTO email_suppressions(normalized_email,reason) SELECT normalized_email,'test' FROM campaign_recipients WHERE id=$1", [recipient]);
      assert.equal((await claim(id, recipient)).rows.length, 0);
      assert.equal((await db.query<{ status: string }>("SELECT status FROM campaign_recipients WHERE id=$1", [recipient])).rows[0].status, "cancelled");
    });
    await t.test("browser roles and service callers cannot bypass scheduling via the internal claim", async () => {
      const { id, recipient } = await create();
      await db.exec("SET ROLE authenticated"); await assert.rejects(op(id, 1, "schedule", future(), "UTC"), /permission denied/);
      await db.exec("RESET ROLE; SET ROLE service_role");
      await assert.rejects(db.query("SELECT * FROM claim_started_outreach_recipient($1,$2,$3)", [user, id, recipient]), /permission denied/);
      await db.exec("RESET ROLE");
    });
  } finally { await db.close(); }
});
