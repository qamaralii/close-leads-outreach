import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { PGlite } from "@electric-sql/pglite";

test("Outlook lifecycle uses PostgreSQL ownership, replay, expiry and snapshot guards", async t => {
  const db = new PGlite();
  const u = "11111111-1111-4111-8111-111111111111", other = "22222222-2222-4222-8222-222222222222";
  try {
    await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
      CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid primary key);
      CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS 'SELECT NULL::uuid';
      CREATE TABLE public.close_leads(close_lead_id text primary key);
      INSERT INTO auth.users VALUES ('${u}'), ('${other}');`);
    for (const file of ["202611100001_outreach_foundation.sql", "202611100002_outreach_send_gate.sql", "202611100003_app_preview.sql"]) {
      await db.exec(await readFile(new URL(`../../supabase/migrations/${file}`, import.meta.url), "utf8"));
    }
    await db.exec(`INSERT INTO public.outreach_members(user_id) VALUES ('${u}'),('${other}');
      INSERT INTO public.outlook_connections(user_id,composio_entity_id,composio_connected_account_id,mailbox_email)
      VALUES ('${u}','existing-entity','ca_old','old@example.com');`);
    await db.exec(await readFile(new URL("../../supabase/migrations/202611100004_outlook_self_service.sql", import.meta.url), "utf8"));
    async function op(user: string, action: string, data: object = {}) {
      const result = await db.query<{ result: Record<string, unknown> }>("SELECT public.manage_outlook_connection($1,$2,$3) AS result", [user, action, JSON.stringify(data)]);
      return result.rows[0].result;
    }
    async function begin(user: string, char: string) {
      await db.query("UPDATE public.outlook_auth_attempts SET created_at = now() - interval '1 minute' WHERE user_id = $1", [user]);
      const a = await op(user, "begin", { state_hash: char.repeat(64), auth_config_id: "ac_outlook" });
      return { attempt_id: a.id, state_hash: char.repeat(64), account_id: `ca_${char}`, mailbox: "work@example.com" };
    }
    await t.test("preserves manually registered identities and campaign snapshots", async () => {
      const a = await begin(u, "a");
      assert.equal((await db.query<{ entity_id: string }>("SELECT entity_id FROM public.outlook_identities WHERE user_id=$1", [u])).rows[0].entity_id, "existing-entity");
      await db.exec("INSERT INTO public.close_leads VALUES ('lead_test')");
      await db.query(`SELECT public.prepare_outreach_preview($1,'preview-test',(SELECT id FROM public.outlook_connections WHERE user_id=$1),'Subject','Body',false,$2)`, [u, JSON.stringify([{ close_lead_id: "lead_test", recipient_email: "test@example.com", rendered_subject: "Subject", rendered_body: "Body" }])]);
      const queued = (await db.query<{ id: string }>(`SELECT public.prepare_outreach_preview($1,'queued-test',(SELECT id FROM public.outlook_connections WHERE user_id=$1),'Subject','Body',false,$2) AS id`, [u, JSON.stringify([{ close_lead_id: "lead_test", recipient_email: "test@example.com", rendered_subject: "Subject", rendered_body: "Body" }])])).rows[0].id;
      await db.query("SELECT public.submit_outreach_campaign($1,$2)", [u, queued]);
      await op(u, "bind", a);
      await assert.rejects(op(other, "complete", a), /Invalid authorization/);
      await assert.rejects(op(u, "complete", { ...a, account_id: "ca_wrong" }), /Account mismatch/);
      await assert.rejects(op(u, "complete", { ...a, state_hash: "f".repeat(64) }), /Invalid authorization/);
      await op(u, "complete", a);
      await op(u, "complete", a); // Same completed callback is a no-op.
      const snapshot = (await db.query<{ sender_account_snapshot: string }>("SELECT sender_account_snapshot FROM public.campaigns")).rows[0];
      assert.equal(snapshot.sender_account_snapshot, "ca_old");
      await assert.rejects(db.query("SELECT public.submit_outreach_campaign($1,(SELECT id FROM public.campaigns WHERE request_id='preview-test'))", [u]), /Reviewed sender/);
      await assert.rejects(db.query("SELECT * FROM public.claim_outreach_recipient($1,$2,(SELECT id FROM public.campaign_recipients WHERE campaign_id=$2))", [u, queued]), /Reviewed sender/);
      assert.equal((await db.query("SELECT * FROM public.outlook_account_cleanup WHERE account_id='ca_old'")).rows.length, 1);
    });
    await t.test("newer start supersedes previous callbacks and permits late-bind cleanup", async () => {
      const a = await begin(u, "b");
      const b = await begin(u, "c");
      assert.equal((await op(u, "bind", a)).bound, false);
      await op(u, "bind", b);
      await assert.rejects(op(u, "complete", a));
      await op(u, "complete", b);
      assert.equal((await db.query("SELECT * FROM public.outlook_account_cleanup WHERE account_id='ca_b'")).rows.length, 1);
    });
    await t.test("expired callbacks and cancelled attempts cannot activate a mailbox", async () => {
      const a = await begin(other, "d"); await op(other, "bind", a);
      await db.query("UPDATE public.outlook_auth_attempts SET expires_at=now()-interval '1 minute' WHERE id=$1", [a.attempt_id]);
      await assert.rejects(op(other, "complete", a), /expired/);
      await op(other, "cancel");
      assert.equal((await db.query("SELECT * FROM public.outlook_connections WHERE user_id=$1", [other])).rows.length, 0);
    });
    await t.test("disconnect retires current account and invalidates pending callbacks", async () => {
      const a = await begin(u, "e"); await op(u, "bind", a);
      await op(u, "disconnect");
      await assert.rejects(op(u, "complete", a), /expired or superseded/);
      assert.equal((await db.query<{ status: string }>("SELECT status FROM public.outlook_connections WHERE user_id=$1", [u])).rows[0].status, "disconnected");
      await op(u, "cleanup_done", { account_id: "ca_c" });
      const b = await begin(u, "f");
      await assert.rejects(op(u, "bind", { ...b, account_id: "ca_c" }), /already used/);
    });
    await t.test("first connection assigns a stable identity and accepts the verified work mailbox", async () => {
      const a = await begin(other, "0");
      await op(other, "bind", a);
      await op(other, "complete", { ...a, mailbox: "Different.Work@Example.com" });
      const c = (await db.query<{ composio_entity_id: string; mailbox_email: string }>("SELECT composio_entity_id,mailbox_email FROM public.outlook_connections WHERE user_id=$1", [other])).rows[0];
      assert.equal(c.composio_entity_id, `close-outreach-${other}`);
      assert.equal(c.mailbox_email, "different.work@example.com");
    });
    await t.test("browser roles cannot mutate connections or read authorization state", async () => {
      await db.exec("SET ROLE authenticated");
      await assert.rejects(op(u, "disconnect"), /permission denied/);
      await assert.rejects(db.query("SELECT * FROM public.outlook_auth_attempts"), /permission denied/);
      await db.exec("RESET ROLE");
      await db.query("UPDATE public.outreach_members SET is_active=false WHERE user_id=$1", [u]);
      await assert.rejects(op(u, "disconnect"), /Active membership/);
    });
  } finally { await db.close(); }
});
