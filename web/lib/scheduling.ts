import { z } from "zod";

export const scheduleSchema = z.object({ localTime: z.string(), timezone: z.string().min(1).max(100), instant: z.iso.datetime() }).strict();

/** Resolve wall-clock time without silently shifting DST gaps or choosing a DST overlap. */
export function scheduleCandidates(localTime: string, timezone: string): string[] {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(localTime);
  if (!match) throw Error("Choose a valid date and time.");
  const [year, month, day, hour, minute] = match.slice(1).map(Number);
  const wall = Date.UTC(year, month - 1, day, hour, minute);
  if (year < 2000 || new Date(wall).toISOString().slice(0, 16) !== localTime) throw Error("Choose a valid date and time.");
  let formatter: Intl.DateTimeFormat;
  try { formatter = new Intl.DateTimeFormat("en-GB", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }); }
  catch { throw Error("Choose a valid time zone."); }
  function localEpoch(instant: number) {
    const parts = Object.fromEntries(formatter.formatToParts(instant).map(p => [p.type, p.value]));
    return Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute), Number(parts.second));
  }
  const offsets = new Set<number>();
  for (let hours = -36; hours <= 36; hours += 6) {
    const sample = wall + hours * 3600000;
    offsets.add(localEpoch(sample) - sample);
  }
  return [...offsets].map(offset => wall - offset).filter(instant => localEpoch(instant) === wall).sort((a, b) => a - b).map(instant => new Date(instant).toISOString());
}

export function resolveSchedule(input: unknown, now = Date.now()) {
  const parsed = scheduleSchema.safeParse(input);
  if (!parsed.success) throw Error("Choose a date, time, and time zone.");
  const schedule = parsed.data;
  const candidates = scheduleCandidates(schedule.localTime, schedule.timezone);
  if (!candidates.length) throw Error("That local time does not exist because of a clock change. Choose another time.");
  if (!candidates.includes(schedule.instant)) throw Error("Confirm the exact send time, including the clock-change occurrence.");
  if (Date.parse(schedule.instant) <= now) throw Error("Choose a future send time.");
  return schedule;
}

export function formatSchedule(instant: string, timezone: string) {
  return new Intl.DateTimeFormat("en-GB", { timeZone: timezone, year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "longOffset", hourCycle: "h23" }).format(new Date(instant));
}

export function scheduleLocalTime(instant: string, timezone: string) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(instant)).map(p => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}
