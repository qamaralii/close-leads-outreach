import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { PGlite } from "@electric-sql/pglite";

type Job = { id: string; lease_token: string | null; status: string; rows_processed: number; total_rows: number; export_id: string | null; synced_at: string };
test("Close sync jobs enforce shared starts, worker fencing and atomic import checkpoints", async t => {
  const db = new PGlite(), user = randomUUID(), other = randomUUID(), inactive = randomUUID();
  try {
    await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
      CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid primary key);
      CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS $$ SELECT nullif(current_setting('test.user_id',true),'')::uuid $$;
      GRANT USAGE ON SCHEMA auth TO authenticated;
      CREATE TABLE close_leads(close_lead_id text primary key,company_name text,company_url text,contact_name text,first_name text,last_name text,contact_title text,email text CHECK(email IS DISTINCT FROM 'broken'),lead_status text,industry text,source_data jsonb NOT NULL DEFAULT '{}',last_synced_at timestamptz,local_note text DEFAULT 'keep');`);
    await db.query("INSERT INTO auth.users VALUES($1),($2),($3)", [user, other, inactive]);
    for (const file of ["202611100001_outreach_foundation.sql", "202611160010_close_lead_sync.sql"]) await db.exec(await readFile(new URL(`../../supabase/migrations/${file}`, import.meta.url), "utf8"));
    await db.query("INSERT INTO outreach_members(user_id,is_active) VALUES($1,true),($2,true),($3,false)", [user, other, inactive]);
    const start = async (owner = user, request = randomUUID(), retry: string | null = null) => (await db.query<{ id: string }>("SELECT start_close_lead_sync($1,$2,$3) AS id", [owner, request, retry])).rows[0].id;
    const claim = async () => (await db.query<{ job: Job | null }>("SELECT claim_close_lead_sync() AS job")).rows[0].job;
    const update = async (j: Job, op: string, data: object = {}) => (await db.query<{ job: Job }>("SELECT update_close_lead_sync($1,$2,$3,$4) AS job", [j.id, j.lease_token, op, JSON.stringify(data)])).rows[0].job;
    const batch = async (j: Job, offset: number, rows: object[]) => (await db.query<{ job: Job }>("SELECT import_close_lead_batch($1,$2,$3,$4) AS job", [j.id, j.lease_token, offset, JSON.stringify(rows)])).rows[0].job;
    const record = (id: string, email = `${id}@example.com`) => ({ close_lead_id: id, company_name: "Updated Company", contact_name: "Alex", first_name: "Alex", email, source_data: { id, date_created: "2026-11-01T10:00:00Z", "custom.Example": "original" }, last_synced_at: "2000-01-01" });
    async function fresh() { await db.exec("UPDATE close_lead_sync_jobs SET status='failed',lease_token=NULL,lease_until=NULL WHERE status NOT IN ('completed','failed')"); await start(); return (await claim())!; }
    async function prepared(j: Job, total = 2) { j = await update(j, "export", { export_id: "expo_Test" }); j = await update(j, "download"); return update(j, "prepared", { total_rows: total, csv_sha256: "a".repeat(64) }); }
    await t.test("active members share one job and exact start retries never create another", async () => {
      const request = randomUUID(), peerRequest = randomUUID(), id = await start(user, request);
      assert.equal(await start(user, request), id); assert.equal(await start(other, peerRequest), id);
      assert.equal((await db.query("SELECT id FROM close_lead_sync_jobs")).rows.length, 1);
      await assert.rejects(start(inactive), /membership/);
      const worker = (await claim())!; assert.equal(worker.id, id); assert.equal(await claim(), null);
      await assert.rejects(db.query("INSERT INTO close_lead_sync_jobs(requested_by,request_id) VALUES($1,$2)", [user, randomUUID()]), /unique/);
      let completed = await prepared(worker, 1); completed = await batch(completed, 0, [record("lead_Shared")]); await update(completed, "complete");
      assert.equal(await start(other, peerRequest), id); // Lost shared-start response retried after completion.
      assert.equal(await start(user, request), id);
      await assert.rejects(start(user, request, id), /conflict/);
    });
    await t.test("expired workers cannot advance or write after a replacement claims the job", async () => {
      const j = await fresh();
      await update(j, "export", { export_id: "expo_Test" });
      await db.query("UPDATE close_lead_sync_jobs SET lease_until=now()-interval '1 second' WHERE id=$1", [j.id]);
      const replacement = (await claim())!; assert.notEqual(replacement.lease_token, j.lease_token); assert.equal(replacement.export_id, "expo_Test");
      await assert.rejects(update(j, "heartbeat"), /lease lost/);
      const active = await prepared(replacement);
      await assert.rejects(batch(j, 0, [record("lead_Stale")]), /lease lost/);
      assert.equal((await db.query("SELECT id FROM close_lead_sync_jobs WHERE id=$1", [active.id])).rows.length, 1);
      assert.equal((await db.query("SELECT close_lead_id FROM close_leads WHERE close_lead_id='lead_Stale'")).rows.length, 0);
    });
    await t.test("upserts preserve absent leads, extra columns, campaign snapshots and a single sync timestamp", async () => {
      await db.exec("INSERT INTO close_leads(close_lead_id,company_name,email,local_note) VALUES('lead_A','Old','old@example.com','custom local note'),('lead_Absent','Retained','absent@example.com','keep')");
      await db.query("INSERT INTO outlook_connections(user_id,composio_entity_id,composio_connected_account_id,mailbox_email) VALUES($1,'entity','account','sender@example.com')", [user]);
      const preview = (await db.query<{ id: string }>("SELECT prepare_outreach_preview($1,'sync-preview',(SELECT id FROM outlook_connections WHERE user_id=$1),'Subject','Body',false,$2) AS id", [user, JSON.stringify([{ close_lead_id: "lead_A", recipient_email: "old@example.com", rendered_subject: "Hello Old", rendered_body: "Original message" }])])).rows[0].id;
      let j = await prepared(await fresh());
      j = await batch(j, 0, [record("lead_A")]);
      assert.equal(j.rows_processed, 1);
      const replay = await batch(j, 0, [record("lead_A")]); assert.equal(replay.rows_processed, 1);
      await assert.rejects(batch(j, 0, [record("lead_A", "different@example.com")]), /checkpoint changed/);
      j = await batch(j, 1, [record("lead_B")]); j = await update(j, "complete");
      assert.equal(j.status, "completed"); assert.equal(j.rows_processed, 2);
      const rows = (await db.query<{ company_name: string; local_note: string; last_synced_at: string }>("SELECT * FROM close_leads WHERE close_lead_id IN ('lead_A','lead_B') ORDER BY close_lead_id")).rows;
      assert.equal(rows[0].company_name, "Updated Company"); assert.equal(rows[0].local_note, "custom local note");
      assert.equal(new Date(rows[0].last_synced_at).getTime(), new Date(j.synced_at).getTime());
      assert.equal(new Date(rows[1].last_synced_at).getTime(), new Date(j.synced_at).getTime());
      assert.equal((await db.query("SELECT close_lead_id FROM close_leads WHERE close_lead_id='lead_Absent'")).rows.length, 1);
      const saved = (await db.query<{ recipient_email: string; rendered_subject: string }>("SELECT recipient_email,rendered_subject FROM campaign_recipients WHERE campaign_id=$1", [preview])).rows[0];
      assert.deepEqual(saved, { recipient_email: "old@example.com", rendered_subject: "Hello Old" });
    });
    await t.test("a rejected batch rolls back all its writes and leaves progress unchanged", async () => {
      const j = await prepared(await fresh());
      await assert.rejects(batch(j, 0, [record("lead_Rollback"), record("lead_Invalid", "broken")]), /check constraint/);
      assert.equal((await db.query("SELECT close_lead_id FROM close_leads WHERE close_lead_id='lead_Rollback'")).rows.length, 0);
      assert.equal((await db.query<{ rows_processed: number }>("SELECT rows_processed FROM close_lead_sync_jobs WHERE id=$1", [j.id])).rows[0].rows_processed, 0);
      await assert.rejects(batch(j, 0, [record("lead_Dupe"), record("lead_Dupe")]), /duplicate/);
      await assert.rejects(update(j, "complete"), /incomplete/);
      await assert.rejects(update(j, "prepared", { total_rows: 3, csv_sha256: "b".repeat(64) }), /Export changed/);
    });
    await t.test("retry resumes the original export/checkpoint with a new lease and bounded backoff", async () => {
      let j = await prepared(await fresh()); j = await batch(j, 0, [record("lead_Retry")]);
      const oldToken = j.lease_token;
      j = await update(j, "failure", { retryable: true, error: "Response lost", delay_seconds: 3000 });
      assert.equal(j.status, "retrying"); assert.equal(await claim(), null);
      await db.query("UPDATE close_lead_sync_jobs SET retry_at=now()-interval '1 second' WHERE id=$1", [j.id]);
      j = (await claim())!; assert.notEqual(j.lease_token, oldToken); assert.equal(j.rows_processed, 1); assert.equal(j.export_id, "expo_Test");
      j = await update(j, "failure", { retryable: false, error: "Stopped" });
      const request = randomUUID(); assert.equal(await start(other, request, j.id), j.id); assert.equal(await start(other, request, j.id), j.id);
      j = (await claim())!; assert.equal(j.rows_processed, 1); assert.equal(j.export_id, "expo_Test");
      await assert.rejects(update({ ...j, lease_token: oldToken }, "heartbeat"), /lease lost/);
    });
    await t.test("browser roles can read shared status but cannot read lease tokens or import directly", async () => {
      await db.query("SELECT set_config('test.user_id',$1,false)", [other]);
      await db.exec("SET ROLE authenticated");
      assert.ok((await db.query("SELECT id,status,rows_processed FROM close_lead_sync_jobs")).rows.length > 0);
      await assert.rejects(db.query("SELECT lease_token FROM close_lead_sync_jobs"), /permission denied/);
      await assert.rejects(db.query("SELECT request_id FROM close_lead_sync_requests"), /permission denied/);
      await assert.rejects(start(), /permission denied/);
      await assert.rejects(claim(), /permission denied/);
      await db.exec("RESET ROLE; SET ROLE service_role");
      await assert.rejects(db.query("UPDATE close_lead_sync_jobs SET rows_processed=100"), /permission denied/);
      await assert.rejects(db.query("SELECT check_close_sync_lease($1,$2)", [randomUUID(), randomUUID()]), /permission denied/);
      await db.exec("RESET ROLE");
      await db.query("SELECT set_config('test.user_id',$1,false)", [inactive]); await db.exec("SET ROLE authenticated");
      assert.equal((await db.query("SELECT id FROM close_lead_sync_jobs")).rows.length, 0);
      await db.exec("RESET ROLE");
    });
    await t.test("automatic retry is bounded and retains the original export", async () => {
      let j = await prepared(await fresh());
      for (let attempt = 0; attempt < 5; attempt++) {
        j = await update(j, "failure", { retryable: true, error: "Rate limited", delay_seconds: 3000 });
        assert.equal(j.status, attempt < 4 ? "retrying" : "failed");
        assert.equal(j.export_id, "expo_Test");
        if (attempt < 4) {
          const retry = (await db.query<{ retry_at: string }>("SELECT retry_at FROM close_lead_sync_jobs WHERE id=$1", [j.id])).rows[0].retry_at;
          assert.ok(new Date(retry).getTime() - Date.now() <= 301000);
          assert.equal(await claim(), null);
          await db.query("UPDATE close_lead_sync_jobs SET retry_at=now()-interval '1 second' WHERE id=$1", [j.id]);
          j = (await claim())!;
        }
      }
      assert.equal(await claim(), null);
    });
  } finally { await db.close(); }
});
