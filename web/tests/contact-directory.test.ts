import { test } from "node:test";
import assert from "node:assert/strict";
import { createClient } from "@supabase/supabase-js";
import { PGlite } from "@electric-sql/pglite";
import { readFile } from "node:fs/promises";
import { contactDirectoryQuery } from "../lib/contact-directory";

test("contact API sends allowlisted server ordering, search and pagination to PostgREST", async () => {
  let requested: URL | undefined;
  const db = createClient("https://example.supabase.co", "test-key", {
    auth: { persistSession: false, autoRefreshToken: false },
    global: { fetch: async input => { requested = new URL(String(input)); return Response.json([], { headers: { "Content-Range": "*/0" } }); } },
  });
  for (const [sort, column] of [["name", "name_sort"], ["company", "company_sort"], ["created", "close_created_at"], ["__proto__", "name_sort"], ["source_data", "name_sort"]]) {
    for (const direction of ["asc", "desc"]) {
      const { error } = await contactDirectoryQuery(db, new URLSearchParams({ q: "Acme", page: "2", sort, direction }));
      assert.equal(error, null);
      assert.equal(requested?.pathname, "/rest/v1/close_contact_directory");
      assert.equal(requested?.searchParams.get("order"), `${column}.${direction}.nullslast,close_lead_id.asc`);
      assert.equal(requested?.searchParams.get("offset"), "50");
      assert.equal(requested?.searchParams.get("limit"), "25");
      assert.match(requested!.searchParams.get("or")!, /company_name.ilike.%Acme%/);
      assert.ok(!requested?.searchParams.get("select")?.includes("source_data"));
    }
  }
  await contactDirectoryQuery(db, new URLSearchParams({ page: "-1", direction: "invalid" }));
  assert.equal(requested?.searchParams.get("order"), "name_sort.asc.nullslast,close_lead_id.asc");
  assert.equal(requested?.searchParams.get("offset"), "0");
});

test("contact view sorts timestamp instants and retains underlying membership RLS", async () => {
  const db = new PGlite();
  const member = "11111111-1111-4111-8111-111111111111";
  try {
    await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
      CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid primary key);
      CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS $$ SELECT nullif(current_setting('request.jwt.claim.sub', true),'')::uuid $$;
      CREATE TABLE public.close_leads(close_lead_id text primary key, contact_name text, company_name text, email text, source_data jsonb DEFAULT '{}');
      INSERT INTO auth.users VALUES ('${member}');`);
    for (const file of ["202611100001_outreach_foundation.sql", "202611100002_outreach_send_gate.sql", "202611100003_app_preview.sql", "202611100004_outlook_self_service.sql", "202611100005_contact_sorting.sql"]) {
      await db.exec(await readFile(new URL(`../../supabase/migrations/${file}`, import.meta.url), "utf8"));
    }
    await db.query("INSERT INTO public.outreach_members(user_id) VALUES ($1)", [member]);
    const rows = [
      ["a", " Zoe ", "Acme", "2025-02-24T07:00:00Z"],
      ["b", "alice", "beta", "2025-02-24T08:00:00+02:00"],
      ["c", "ALICE", "acme", "2025-02-24T07:00:00+00:00"],
      ["d", "", "", null], ["e", null, null, "not-a-date"],
      ["f", "Bob", "Gamma", "2025-02-30T00:00:00Z"],
    ];
    for (const [id, name, company, date] of rows) await db.query("INSERT INTO public.close_leads VALUES ($1,$2,$3,'test@example.com',$4)", [id, name, company, JSON.stringify({ date_created: date })]);
    await db.exec(`SET ROLE authenticated; SET request.jwt.claim.sub = '${member}';`);
    async function ids(sql: string) { return (await db.query<{ close_lead_id: string }>(sql)).rows.map(r => r.close_lead_id); }
    assert.deepEqual(await ids("SELECT * FROM public.close_contact_directory ORDER BY close_created_at ASC NULLS LAST,close_lead_id"), ["b", "a", "c", "d", "e", "f"]);
    assert.deepEqual(await ids("SELECT * FROM public.close_contact_directory ORDER BY close_created_at DESC NULLS LAST,close_lead_id"), ["a", "c", "b", "d", "e", "f"]);
    assert.deepEqual(await ids("SELECT * FROM public.close_contact_directory ORDER BY name_sort ASC NULLS LAST,close_lead_id"), ["b", "c", "f", "a", "d", "e"]);
    assert.deepEqual(await ids("SELECT * FROM public.close_contact_directory ORDER BY company_sort DESC NULLS LAST,close_lead_id"), ["f", "b", "a", "c", "d", "e"]);
    assert.deepEqual(await ids("SELECT * FROM public.close_contact_directory WHERE company_name ILIKE '%acme%' ORDER BY close_created_at DESC NULLS LAST,close_lead_id LIMIT 1 OFFSET 1"), ["c"]);
    await db.exec("SET request.jwt.claim.sub = '22222222-2222-4222-8222-222222222222'");
    assert.deepEqual(await ids("SELECT * FROM public.close_contact_directory"), []);
    await db.exec(`RESET ROLE; UPDATE public.outreach_members SET is_active=false; SET ROLE authenticated; SET request.jwt.claim.sub='${member}'`);
    assert.deepEqual(await ids("SELECT * FROM public.close_contact_directory"), []);
    await db.exec("RESET ROLE; SET ROLE anon");
    await assert.rejects(db.query("SELECT * FROM public.close_contact_directory"), /permission denied/);
  } finally { await db.close(); }
});
