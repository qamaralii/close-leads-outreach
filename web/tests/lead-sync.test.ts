import { test } from "node:test";
import assert from "node:assert/strict";
import { syncLabel, syncRunning, type SyncJob } from "../lib/lead-sync";

test("sync state distinguishes an active retry from a terminal failure and reports refreshed rows", () => {
  const job = { status: "retrying", rows_processed: 200, total_rows: 868 } as SyncJob;
  assert.equal(syncRunning(job), true);
  assert.equal(syncRunning({ ...job, status: "failed" }), false);
  assert.equal(syncRunning({ ...job, status: "completed" }), false);
  assert.equal(syncRunning(null), false);
  assert.equal(syncLabel({ ...job, status: "importing" }), "200 of 868 leads refreshed");
  assert.equal(syncLabel({ ...job, status: "completed", rows_processed: 868 }), "868 leads refreshed");
});
