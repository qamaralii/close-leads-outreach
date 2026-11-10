import { test } from "node:test";
import assert from "node:assert/strict";
import { OutlookProvider } from "../lib/outlook-provider";

const account = { id: "ca_one", user_id: "entity-one", status: "ACTIVE", is_disabled: false, toolkit: { slug: "outlook" }, auth_config: { id: "ac_outlook" } };
function provider(responses: unknown[]) {
  const calls: { url: string; options?: RequestInit }[] = [];
  const api = new OutlookProvider("test-secret", async (url, options) => {
    calls.push({ url: String(url), options });
    const data = responses.shift();
    return data instanceof Response ? data : Response.json(data);
  });
  return { api, calls };
}
test("verifies exact account ownership and resolves a different sender email", async () => {
  const { api, calls } = provider([account, { status: 200, data: { mail: " Work@Example.com " } }]);
  assert.equal(await api.verify("ca_one", "entity-one", "ac_outlook"), "work@example.com");
  assert.deepEqual(JSON.parse(String(calls[1].options?.body)), { connected_account_id: "ca_one", user_id: "entity-one", method: "GET", endpoint: "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName" });
  assert.ok(calls.every(c => c.url.startsWith("https://backend.composio.dev/api/v3.1/")));
});
test("rejects another owner, toolkit, config, account ID, or inactive account before Graph", async () => {
  for (const change of [{ user_id: "other" }, { toolkit: { slug: "gmail" } }, { auth_config: { id: "other" } }, { id: "ca_other" }, { is_disabled: true }, { status: "EXPIRED" }]) {
    const { api, calls } = provider([{ ...account, ...change }]);
    await assert.rejects(api.verify("ca_one", "entity-one", "ac_outlook"));
    assert.equal(calls.length, 1);
  }
});
test("rejects malformed mailbox and outer-only HTTP success", async () => {
  for (const profile of [{ status: 401, data: { mail: "work@example.com" } }, { status: "200", data: { mail: "work@example.com" } }, { status: 200, data: { mail: "invalid" } }]) {
    await assert.rejects(provider([account, profile]).api.verify("ca_one", "entity-one", "ac_outlook"));
  }
});
test("Connect Link pins user, config, and callback and returns exact new account", async () => {
  const { api, calls } = provider([{ toolkit: { slug: "outlook" }, auth_scheme: "OAUTH2" }, { connected_account_id: "ca_new", redirect_url: "https://connect.composio.dev/link/test", expires_at: "2027-01-01T00:00:00Z" }]);
  assert.equal((await api.link("entity", "ac_outlook", "https://app.example/settings/outlook/callback?state=test")).connected_account_id, "ca_new");
  assert.deepEqual(JSON.parse(String(calls[1].options?.body)), { user_id: "entity", auth_config_id: "ac_outlook", callback_url: "https://app.example/settings/outlook/callback?state=test" });
});
test("rejects untrusted authorization redirects", async () => {
  for (const redirect_url of ["https://composio.dev.attacker.test/x", "http://connect.composio.dev/x", "https://user:pass@connect.composio.dev/x"]) {
    const { api } = provider([{ toolkit: { slug: "outlook" }, auth_scheme: "OAUTH2" }, { connected_account_id: "ca_new", redirect_url, expires_at: "2027-01-01T00:00:00Z" }]);
    await assert.rejects(api.link("entity", "ac_outlook", "https://app.example/callback"));
  }
});
test("provider errors do not expose credentials or response bodies", async () => {
  const { api } = provider([new Response("secret-token-or-provider-message", { status: 500 })]);
  await assert.rejects(api.verify("ca_one", "entity-one"), e => e instanceof Error && !e.message.includes("secret-token"));
});
test("disable targets only the supplied validated account", async () => {
  const { api, calls } = provider([{ success: true }, { id: "ca_one", is_disabled: true }]);
  await api.disable("ca_one");
  assert.equal(calls[0].options?.method, "PATCH");
  assert.equal(calls[0].options?.body, '{"enabled":false}');
  await assert.rejects(api.disable("../other"));
  assert.equal(calls.length, 2);
});
test("cleanup is not marked successful without confirming provider disable", async () => {
  await assert.rejects(provider([{ success: false }, { id: "ca_one", is_disabled: false }]).api.disable("ca_one"));
});
