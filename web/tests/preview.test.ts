import test from "node:test";
import assert from "node:assert/strict";
import { requestSchema, resolvePreview, validEmail, type Lead } from "../lib/preview";

const input = { requestId: "ad5eb73e-9e53-4f97-9e41-bc4d59d61c01", leadIds: ["lead_A"], subject: "Hello {{company}}", body: "Hi {{first_name}},\n\nHow are you?" };
const lead: Lead = { close_lead_id: "lead_A", company_name: "Example", contact_name: "Ada Smith", first_name: "Ada", email: "Ada@example.com" };

test("renders exact messages and preserves line breaks", () => {
  const result = resolvePreview(input, [lead], new Set());
  assert.equal(result.recipients[0].rendered_subject, "Hello Example");
  assert.equal(result.recipients[0].rendered_body, "Hi Ada,\n\nHow are you?");
});
test("selection order controls case-insensitive deduplication", () => {
  const result = resolvePreview({ ...input, leadIds: ["lead_B", "lead_A"] }, [lead, { ...lead, close_lead_id: "lead_B", email: "ADA@example.com" }], new Set());
  assert.equal(result.recipients[0].close_lead_id, "lead_B");
  assert.deepEqual(result.excluded, [{ close_lead_id: "lead_A", reason: "duplicate_email" }]);
});
test("suppression and invalid email are excluded", () => {
  const result = resolvePreview({ ...input, leadIds: ["lead_A", "lead_B", "lead_C"] }, [lead, { ...lead, close_lead_id: "lead_B", email: "bad address" }, { ...lead, close_lead_id: "lead_C", email: "ok@example.com" }], new Set(["ada@example.com"]));
  assert.equal(result.recipients.length, 1);
  assert.deepEqual(result.excluded.map(r => r.reason), ["suppressed", "missing_or_invalid_email"]);
});
test("missing personalization blocks the whole preview", () => {
  assert.throws(() => resolvePreview(input, [{ ...lead, first_name: null }], new Set()), /Missing first_name/);
});
test("subject injection and recursive placeholders are rejected", () => {
  assert.throws(() => resolvePreview(input, [{ ...lead, company_name: "Example\r\nBcc: someone" }], new Set()), /subject line breaks/);
  assert.throws(() => resolvePreview(input, [{ ...lead, company_name: "{{first_name}}" }], new Set()), /Invalid personalized/);
});
test("unsupported placeholders and malformed braces are rejected", () => {
  for (const subject of ["{{email}}", "{{{company}}}", "{{company", "company}}"])
    assert.throws(() => resolvePreview({ ...input, subject }, [lead], new Set()), /Supported placeholders/);
});
test("static messages do not require personalization data", () => {
  assert.equal(resolvePreview({ ...input, subject: "Hello", body: "A message." }, [{ ...lead, first_name: null, company_name: null }], new Set()).recipients.length, 1);
});
test("request schema rejects caller identity and sender overrides", () => {
  for (const field of ["user_id", "sender_connection_id", "is_html", "send_mode"])
    assert.equal(requestSchema.safeParse({ ...input, [field]: "override" }).success, false);
  assert.equal(requestSchema.safeParse({ ...input, leadIds: [] }).success, false);
  assert.equal(requestSchema.safeParse({ ...input, requestId: "not-a-uuid" }).success, false);
});
test("email validation rejects address lists and malformed domains", () => {
  for (const email of ["a@example.com,b@example.com", "a..b@example.com", "a@-example.com", "a@example..com", "x\n@example.com"])
    assert.equal(validEmail(email), false);
  assert.equal(validEmail("person+test@example.com"), true);
});
