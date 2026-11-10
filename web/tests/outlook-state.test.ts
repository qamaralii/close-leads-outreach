import { test } from "node:test";
import assert from "node:assert/strict";
import { matchesOAuthState, canCompleteAuthorization } from "../lib/outlook-state";

test("OAuth state requires the exact initiating browser cookie", () => {
  assert.equal(matchesOAuthState("a".repeat(64), "a".repeat(64)), true);
  for (const cookie of ["", "b".repeat(64), "é".repeat(64), "a".repeat(63)]) {
    assert.equal(matchesOAuthState("a".repeat(64), cookie), false);
  }
});
test("callback cannot substitute an account, replay, complete after expiry, or complete a cancellation", () => {
  const attempt = { status: "pending", expires_at: "2027-01-01T00:00:00Z", account_id: "ca_expected" };
  const now = Date.parse("2026-01-01T00:00:00Z");
  assert.equal(canCompleteAuthorization(attempt, "success", "ca_expected", now), true);
  for (const changed of [{ ...attempt, status: "completed" }, { ...attempt, status: "cancelled" }, { ...attempt, account_id: null }, { ...attempt, expires_at: "2025-01-01T00:00:00Z" }]) {
    assert.equal(canCompleteAuthorization(changed, "success", "ca_expected", now), false);
  }
  assert.equal(canCompleteAuthorization(attempt, "success", "ca_attacker", now), false);
  assert.equal(canCompleteAuthorization(attempt, "failed", "ca_expected", now), false);
});
