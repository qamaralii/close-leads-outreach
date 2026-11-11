"use client";
import { useActionState, useEffect, useId, useState } from "react";
import { useRouter } from "next/navigation";
import { confirm, scheduleCampaign } from "../../actions";
import { useReview } from "./message";
import { ScheduleFields, completeSchedule, type ScheduleInput } from "./schedule-fields";
import { scheduleLocalTime } from "@/lib/scheduling";
export function Controls({ id, revision, draft, blocked = false, scheduled = false, scheduledAt, timezone, locked = false }: { id: string; revision: number; draft: boolean; blocked?: boolean; scheduled?: boolean; scheduledAt?: string | null; timezone?: string | null; locked?: boolean }) {
  const router = useRouter();
  const deliveryId = useId();
  const [error, action, pending] = useActionState(confirm, "");
  const { editing } = useReview();
  const [reviewed, setReviewed] = useState(false);
  const [sendMode, setSendMode] = useState("now");
  const [schedule, setSchedule] = useState<ScheduleInput>({ localTime: scheduledAt && timezone ? scheduleLocalTime(scheduledAt, timezone) : "", timezone: timezone ?? "", instant: scheduledAt ?? "" });
  const [operationId, setOperationId] = useState("");
  useEffect(() => { setOperationId(crypto.randomUUID()); if (!timezone) setSchedule(value => ({ ...value, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" })); }, [timezone]);
  function changed(value: ScheduleInput) { setSchedule(value); setReviewed(false); setOperationId(crypto.randomUUID()); }
  useEffect(() => { setReviewed(false); }, [revision, editing]);
  useEffect(() => { if (error) setReviewed(false); }, [error]);
  useEffect(() => { if (!draft) { const timer = setInterval(() => router.refresh(), 10000); return () => clearInterval(timer); } }, [draft, router]);
  if (scheduled && scheduledAt && timezone) return <ScheduleManagement id={id} revision={revision} blocked={blocked} locked={locked} scheduledAt={scheduledAt} schedule={schedule} changed={changed} operationId={operationId} />;
  if (!draft) return <p>Results refresh every 10 seconds. Microsoft acceptance is shown as submitted; it is not a delivery receipt.</p>;
  if (blocked) return <p>Confirmation is unavailable because the reviewed sender is no longer active.</p>;
  return <form className="review-confirmation" action={action}><input type="hidden" name="campaignId" value={id} /><input type="hidden" name="revision" value={revision} /><input type="hidden" name="operationId" value={operationId} /><label htmlFor={deliveryId}>Delivery</label><select id={deliveryId} name="sendMode" value={sendMode} disabled={pending || !!editing} onChange={e => { setSendMode(e.target.value); setReviewed(false); setOperationId(crypto.randomUUID()); }}><option value="now">Send now</option><option value="scheduled">Schedule for later</option></select>{sendMode === "scheduled" && <ScheduleFields value={schedule} onChange={changed} disabled={pending || !!editing} />}<label><input type="checkbox" name="reviewed" value="yes" checked={reviewed} onChange={e => setReviewed(e.target.checked)} disabled={pending || !!editing} required /> I reviewed the saved recipients, sender, messages{sendMode === "scheduled" ? ", and scheduled send time" : ""} and confirm {sendMode === "scheduled" ? "this schedule" : "sending"}.</label>{editing && <p className="field-help">Save or cancel your message edit before confirming.</p>}<p className="error" role="alert">{error}</p><button disabled={pending || !!editing || !reviewed || !operationId}>{pending ? "Confirming…" : sendMode === "scheduled" ? "Confirm schedule" : "Confirm & send"}</button></form>;
}

function ScheduleManagement({ id, revision, blocked, locked, scheduledAt, schedule, changed, operationId }: { id: string; revision: number; blocked: boolean; locked: boolean; scheduledAt: string; schedule: ScheduleInput; changed: (value: ScheduleInput) => void; operationId: string }) {
  const router = useRouter();
  const [rescheduling, setRescheduling] = useState(false), [pending, setPending] = useState(false), [error, setError] = useState("");
  const [due, setDue] = useState(locked);
  useEffect(() => { const update = () => setDue(Date.now() >= Date.parse(scheduledAt)); update(); const timer = setInterval(update, 1000); return () => clearInterval(timer); }, [scheduledAt]);
  async function manage(operation: "withdraw" | "reschedule", edit = false) {
    setPending(true); setError("");
    try {
      const result = await scheduleCampaign({ campaignId: id, revision, operationId, operation, ...(operation === "reschedule" ? { schedule: completeSchedule(schedule) } : {}) });
      if (result.error) setError(result.error);
      else { setRescheduling(false); if (edit) router.push(`/campaigns/${id}/edit`); router.refresh(); }
    } catch { setError("Response lost. Retry unchanged input or refresh to check the schedule."); }
    finally { setPending(false); }
  }
  if (due) return <div className="review-confirmation"><p className="notice">The scheduled time has arrived. Sending is eligible to start; this campaign can no longer be rescheduled or returned to draft.</p><p className="field-help">Activity refreshes every 10 seconds. Check Outlook if the sender is blocked.</p></div>;
  return <div className="review-confirmation"><p className="field-help">You can change this schedule until its send time. Cancelling or editing returns it to draft and removes it from the send queue.</p>{rescheduling ? <form onSubmit={e => { e.preventDefault(); void manage("reschedule"); }}><ScheduleFields value={schedule} onChange={changed} disabled={pending} /><div className="row"><button disabled={pending || blocked || !operationId}>Confirm new schedule</button><button type="button" className="secondary" disabled={pending} onClick={() => { setRescheduling(false); setError(""); }}>Keep current schedule</button></div></form> : <div className="row"><button className="secondary" disabled={pending || blocked || !operationId} onClick={() => setRescheduling(true)}>Reschedule</button><button className="secondary" disabled={pending || !operationId} onClick={() => { void manage("withdraw", true); }}>Edit campaign</button><button className="danger" disabled={pending || !operationId} onClick={() => { void manage("withdraw"); }}>Cancel schedule</button></div>}{error && <p className="error" role="alert">{error}</p>}</div>;
}
