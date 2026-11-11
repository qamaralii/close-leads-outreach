import { test } from "node:test";
import assert from "node:assert/strict";
import { scheduleCandidates, resolveSchedule, scheduleLocalTime } from "../lib/scheduling";

test("wall-clock scheduling supports UTC, fractional offsets and date rollover", () => {
  assert.deepEqual(scheduleCandidates("2027-01-01T00:15", "Asia/Kolkata"), ["2026-12-31T18:45:00.000Z"]);
  assert.deepEqual(scheduleCandidates("2027-01-01T00:15", "Asia/Kathmandu"), ["2026-12-31T18:30:00.000Z"]);
  assert.deepEqual(scheduleCandidates("2027-01-01T00:15", "UTC"), ["2027-01-01T00:15:00.000Z"]);
  assert.equal(scheduleLocalTime("2026-12-31T18:45:00.000Z", "Asia/Kolkata"), "2027-01-01T00:15");
});
test("DST gaps are rejected and overlapping times require an explicit valid instant", () => {
  assert.deepEqual(scheduleCandidates("2027-03-14T02:30", "America/New_York"), []);
  assert.deepEqual(scheduleCandidates("2027-11-07T01:30", "America/New_York"), ["2027-11-07T05:30:00.000Z", "2027-11-07T06:30:00.000Z"]);
  assert.deepEqual(scheduleCandidates("2027-10-03T02:15", "Australia/Lord_Howe"), []);
  assert.equal(scheduleCandidates("2027-04-04T01:45", "Australia/Lord_Howe").length, 2);
  const schedule = { localTime: "2027-11-07T01:30", timezone: "America/New_York", instant: "2027-11-07T06:30:00.000Z" };
  assert.equal(resolveSchedule(schedule, 0).instant, schedule.instant);
  assert.throws(() => resolveSchedule({ ...schedule, instant: "2027-11-07T07:30:00.000Z" }, 0), /exact send time/);
});
test("invalid calendar dates, zones, past instants and caller overrides are rejected", () => {
  for (const date of ["2027-02-29T12:00", "2027-01-32T12:00", "2027-13-01T12:00", "2027-01-01T24:00", "bad"]) assert.throws(() => scheduleCandidates(date, "UTC"));
  assert.throws(() => scheduleCandidates("2027-01-01T12:00", "Unknown/Zone"), /time zone/);
  const schedule = { localTime: "2027-01-01T12:00", timezone: "UTC", instant: "2027-01-01T12:00:00.000Z" };
  assert.throws(() => resolveSchedule(schedule, Date.parse(schedule.instant)), /future/);
  assert.throws(() => resolveSchedule({ ...schedule, userId: "another user" }, 0));
});
