import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { PGlite } from "@electric-sql/pglite";
import { validateMessage } from "../lib/preview";

test("individual messages reject blank text, header injection and unresolved placeholders", () => {
  validateMessage("A personal subject", "Hi Alex,\n\nA personal message.");
  for (const [subject, body] of [[" ", "Body"], ["Hello\r\nBcc: other@example.com", "Body"], ["Hello", "{{first_name}}"], ["Hello", "Bad\0body"]]) {
    assert.throws(() => validateMessage(subject, body));
  }
});

test("draft revisions preserve overrides, support reset, and gate confirmation in PostgreSQL", async t => {
  const db = new PGlite();
  const user = "11111111-1111-4111-8111-111111111111", other = "22222222-2222-4222-8222-222222222222";
  try {
    await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
      CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid primary key);
      CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS 'SELECT NULL::uuid';
      CREATE TABLE public.close_leads(close_lead_id text primary key);
      INSERT INTO auth.users VALUES ('${user}'),('${other}');`);
    for (const file of ["202611100001_outreach_foundation.sql", "202611100002_outreach_send_gate.sql", "202611100003_app_preview.sql"]) {
      await db.exec(await readFile(new URL(`../../supabase/migrations/${file}`, import.meta.url), "utf8"));
    }
    await db.exec(`INSERT INTO outreach_members(user_id) VALUES ('${user}'),('${other}');
      INSERT INTO outlook_connections(user_id,composio_entity_id,composio_connected_account_id,mailbox_email)
      VALUES ('${user}','entity','account','sender@example.com');
      INSERT INTO close_leads VALUES ('lead_A'),('lead_B'),('lead_C');`);
    const recipient = (id: string, subject = "Subject", body = "Body") => ({ close_lead_id: `lead_${id}`, recipient_email: `${id.toLowerCase()}@example.com`, rendered_subject: subject, rendered_body: body });
    const result = await db.query<{ id: string }>(`SELECT prepare_app_preview($1,'request',(SELECT id FROM outlook_connections WHERE user_id=$1),'Subject','Body',$2,$3,'[]') AS id`, [user, JSON.stringify([recipient("A"), recipient("B")]), JSON.stringify({ subject: "Subject", body: "Body", leadIds: ["lead_A", "lead_B"] })]);
    const id = result.rows[0].id;
    // Apply on an existing preview to verify backfill, then test new preview inserts.
    await db.exec(await readFile(new URL("../../supabase/migrations/202611110006_draft_editing.sql", import.meta.url), "utf8"));
    const a = (await db.query<{ id: string }>("SELECT id FROM campaign_recipients WHERE campaign_id=$1 AND close_lead_id='lead_A'", [id])).rows[0].id;
    const edit = (revision: number, payload: object, owner = user) => db.query("SELECT edit_outreach_draft($1,$2,$3,$4)", [owner, id, revision, JSON.stringify(payload)]);
    const confirm = (revision: number) => db.query("SELECT confirm_outreach_revision($1,$2,$3)", [user, id, revision]);
    const snapshot = async () => (await db.query<{ revision: number; expected_recipient_count: number; status: string }>("SELECT revision,expected_recipient_count,status FROM campaigns WHERE id=$1", [id])).rows[0];
    const custom = { operation: "message", editId: "custom", recipientId: a, subject: "Personal", body: "Just for A" };
    await t.test("checks ownership, retries unchanged edits, and rejects stale tabs", async () => {
      await assert.rejects(edit(1, custom, other), /unavailable/);
      await edit(1, custom); await edit(1, custom);
      assert.equal((await snapshot()).revision, 2);
      await assert.rejects(edit(2, { ...custom, body: "Different content with the same edit ID" }), /Edit ID conflict/);
      await assert.rejects(edit(1, { ...custom, editId: "different" }), /Draft changed/);
      await assert.rejects(confirm(1), /Draft changed/);
      assert.equal((await snapshot()).status, "draft");
    });
    await t.test("campaign edits replace selection but preserve retained custom messages and update reset text", async () => {
      const campaignEdit = { operation: "campaign", editId: "campaign", request: { subject: "New subject", body: "New body" }, recipients: [recipient("A", "New A", "New A body"), recipient("C", "New C", "New C body")], exclusions: [{ close_lead_id: "lead_B", reason: "removed" }] };
      await edit(2, campaignEdit); await edit(2, campaignEdit);
      const rows = (await db.query<{ id: string; close_lead_id: string; customized: boolean; rendered_subject: string; template_subject: string }>("SELECT * FROM campaign_recipients WHERE campaign_id=$1 ORDER BY close_lead_id", [id])).rows;
      assert.equal(rows.length, 2); assert.equal(rows[0].id, a);
      assert.equal(rows[0].rendered_subject, "Personal"); assert.equal(rows[0].template_subject, "New A"); assert.equal(rows[0].customized, true);
      assert.equal(rows[1].close_lead_id, "lead_C"); assert.equal(rows[1].template_subject, "New C");
      assert.equal((await snapshot()).revision, 3);
    });
    await t.test("failed saves roll back recipient deletion and all campaign changes", async () => {
      await assert.rejects(edit(3, { operation: "campaign", editId: "bad", request: { subject: "Bad", body: "Bad" }, recipients: [recipient("C", "", "Body")], exclusions: [] }));
      assert.equal((await snapshot()).revision, 3);
      assert.equal((await db.query("SELECT * FROM campaign_recipients WHERE id=$1", [a])).rows.length, 1);
    });
    await t.test("reset uses latest template and confirmation locks submitted messages", async () => {
      await edit(3, { operation: "reset", editId: "reset", recipientId: a });
      const row = (await db.query<{ rendered_subject: string; customized: boolean }>("SELECT * FROM campaign_recipients WHERE id=$1", [a])).rows[0];
      assert.equal(row.rendered_subject, "New A"); assert.equal(row.customized, false);
      await assert.rejects(confirm(3), /Draft changed/);
      await confirm(4); await confirm(4);
      assert.equal((await snapshot()).status, "submitted");
      await assert.rejects(edit(4, custom), /already submitted/);
      await assert.rejects(db.query("UPDATE campaign_recipients SET rendered_body='Changed' WHERE id=$1", [a]), /immutable/);
      await assert.rejects(db.query("UPDATE campaigns SET revision=5 WHERE id=$1", [id]), /immutable/);
      assert.equal((await db.query("SELECT * FROM campaign_recipients WHERE campaign_id=$1 AND status='queued'", [id])).rows.length, 2);
    });
    await t.test("browser roles cannot edit and service role cannot bypass revision confirmation", async () => {
      await db.exec("SET ROLE authenticated");
      await assert.rejects(edit(4, custom), /permission denied/);
      await assert.rejects(confirm(4), /permission denied/);
      await db.exec("RESET ROLE; SET ROLE service_role");
      await assert.rejects(db.query("SELECT submit_outreach_campaign($1,$2)", [user, id]), /permission denied/);
      await db.exec("RESET ROLE");
    });
    await t.test("new previews populate reset snapshots and changed email addresses do not inherit overrides", async () => {
      const fresh = (await db.query<{ id: string }>(`SELECT prepare_app_preview($1,'fresh',(SELECT id FROM outlook_connections WHERE user_id=$1),'Subject','Body',$2,'{}','[]') AS id`, [user, JSON.stringify([recipient("A")])])).rows[0].id;
      const old = (await db.query<{ id: string; template_subject: string }>("SELECT id,template_subject FROM campaign_recipients WHERE campaign_id=$1", [fresh])).rows[0];
      assert.equal(old.template_subject, "Subject");
      await db.query("SELECT edit_outreach_draft($1,$2,1,$3)", [user, fresh, JSON.stringify({ ...custom, recipientId: old.id })]);
      await assert.rejects(db.query("UPDATE campaigns SET sender_mailbox_snapshot='other@example.com' WHERE id=$1", [fresh]), /sender are immutable/);
      await db.query("SELECT edit_outreach_draft($1,$2,2,$3)", [user, fresh, JSON.stringify({ operation: "campaign", editId: "email-change", request: { subject: "New", body: "New body" }, recipients: [{ ...recipient("A", "New", "New body"), recipient_email: "new-a@example.com" }], exclusions: [] })]);
      const row = (await db.query<{ id: string; customized: boolean; rendered_subject: string }>("SELECT * FROM campaign_recipients WHERE campaign_id=$1", [fresh])).rows[0];
      assert.notEqual(row.id, old.id); assert.equal(row.customized, false); assert.equal(row.rendered_subject, "New");
      await db.query("UPDATE outreach_members SET is_active=false WHERE user_id=$1", [user]);
      await assert.rejects(db.query("SELECT edit_outreach_draft($1,$2,3,$3)", [user, fresh, JSON.stringify({ ...custom, recipientId: row.id })]), /unavailable/);
    });
  } finally { await db.close(); }
});
