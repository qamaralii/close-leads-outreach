"use client";
import { useEffect, useId, useState } from "react";
import { formatSchedule, scheduleCandidates } from "@/lib/scheduling";

export type ScheduleInput = { localTime: string; timezone: string; instant: string };
export function ScheduleFields({ value, onChange, disabled }: { value: ScheduleInput; onChange: (value: ScheduleInput) => void; disabled: boolean }) {
  const id = useId();
  const [zones, setZones] = useState<string[]>([]);
  useEffect(() => { setZones([...new Set([...Intl.supportedValuesOf("timeZone"), "Asia/Kolkata"])].sort()); }, []);
  let candidates: string[] = [], error = "";
  if (value.localTime && value.timezone) {
    try { candidates = scheduleCandidates(value.localTime, value.timezone); if (!candidates.length) error = "That time does not exist in this time zone because of a clock change."; }
    catch (e) { error = e instanceof Error ? e.message : "Invalid date or time zone."; }
  }
  const instant = candidates.length === 1 ? candidates[0] : candidates.includes(value.instant) ? value.instant : "";
  return <div className="schedule-fields"><div className="schedule-grid"><div><label htmlFor={`${id}-time`}>Send date and time</label><input id={`${id}-time`} name="localTime" type="datetime-local" required step="60" value={value.localTime} disabled={disabled} onChange={e => onChange({ ...value, localTime: e.target.value, instant: "" })} /></div><div><label htmlFor={`${id}-zone`}>Time zone</label><select id={`${id}-zone`} name="timezone" required disabled={disabled} value={value.timezone} onChange={e => onChange({ ...value, timezone: e.target.value, instant: "" })}><option value="" disabled>Choose time zone</option>{[...new Set(["UTC", value.timezone, ...zones].filter(Boolean))].map(zone => <option key={zone} value={zone}>{zone.replaceAll("_", " ")}</option>)}</select></div></div>
    {candidates.length > 1 && <><label htmlFor={`${id}-occurrence`}>This time occurs twice. Choose which occurrence</label><select id={`${id}-occurrence`} required disabled={disabled} value={instant} onChange={e => onChange({ ...value, instant: e.target.value })}><option value="">Choose occurrence</option>{candidates.map((candidate, index) => <option key={candidate} value={candidate}>{index === 0 ? "Earlier" : "Later"}: {formatSchedule(candidate, value.timezone)}</option>)}</select></>}
    <input type="hidden" name="instant" value={instant} />
    {error && <p className="error" role="alert">{error}</p>}{instant && <p className="notice">Sending will start on <strong>{formatSchedule(instant, value.timezone)}</strong> ({value.timezone}).</p>}
    <p className="field-help">Recipients send sequentially from this time. If the worker is offline, sending starts when it resumes.</p>
  </div>;
}

export function completeSchedule(value: ScheduleInput): ScheduleInput {
  try { const candidates = scheduleCandidates(value.localTime, value.timezone); return { ...value, instant: candidates.length === 1 ? candidates[0] : value.instant }; }
  catch { return value; }
}
