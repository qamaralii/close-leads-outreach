import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { ccSchema, attachmentIdsSchema, effectiveDelivery } from "../lib/delivery";
import { requestSchema } from "../lib/preview";

test("CC normalization rejects malformed headers and preserves explicit empty overrides", () => {
  assert.deepEqual(ccSchema.parse([" Manager@Example.com ", "manager@example.com"]), ["manager@example.com"]);
  for (const bad of ["bad", "a@example.com\r\nBcc: other@example.com", "a@example.com,b@example.com", "a..b@example.com"]) assert.equal(ccSchema.safeParse([bad]).success, false);
  const file = randomUUID(), defaults = { cc: ["owner@example.com", "alex@example.com"], attachmentIds: [file] };
  assert.deepEqual(effectiveDelivery(defaults, null, [], "Alex@example.com"), { cc: ["owner@example.com"], attachmentIds: [] });
  assert.deepEqual(effectiveDelivery(defaults, [], null, "alex@example.com"), { cc: [], attachmentIds: [file] });
  assert.equal(attachmentIdsSchema.safeParse([file, file]).success, false);
  const legacy = requestSchema.parse({ requestId: randomUUID(), leadIds: ["lead_A"], subject: "Hello", body: "Body" });
  assert.deepEqual(legacy.cc, []); assert.deepEqual(legacy.attachmentIds, []);
});
