import { z } from "zod";
import { requireMember } from "@/lib/auth";
import { adminClient } from "@/lib/supabase";
import { syncJobFields } from "@/lib/lead-sync";

export const dynamic = "force-dynamic";
const schema = z.object({ requestId: z.uuid(), retryJobId: z.uuid().nullable().default(null) }).strict();
export async function POST(request: Request) {
  const { user } = await requireMember();
  const origin = request.headers.get("origin");
  const expectedOrigin = process.env.APP_URL ? new URL(process.env.APP_URL).origin : new URL(request.url).origin;
  if (origin && origin !== expectedOrigin) return Response.json({ error: "Invalid request origin." }, { status: 403 });
  if (!request.headers.get("content-type")?.startsWith("application/json")) return Response.json({ error: "JSON request required." }, { status: 400 });
  const parsed = schema.safeParse(await request.json().catch(() => null));
  if (!parsed.success) return Response.json({ error: "Invalid sync request." }, { status: 400 });
  const { data, error } = await adminClient().rpc("start_close_lead_sync", { p_user_id: user.id, p_request_id: parsed.data.requestId, p_retry_job_id: parsed.data.retryJobId });
  if (error || typeof data !== "string") return Response.json({ error: "Could not start sync. Refresh its status before retrying." }, { status: 503 });
  return Response.json({ jobId: data }, { status: 202, headers: { "Cache-Control": "private, no-store" } });
}
export async function GET() {
  const { db } = await requireMember();
  const [latest, success, active] = await Promise.all([
    db.from("close_lead_sync_jobs").select(syncJobFields).order("updated_at", { ascending: false }).limit(1).maybeSingle(),
    db.from("close_lead_sync_jobs").select(syncJobFields).eq("status", "completed").order("finished_at", { ascending: false }).limit(1).maybeSingle(),
    db.from("close_lead_sync_jobs").select(syncJobFields).in("status", ["queued", "exporting", "downloading", "importing", "retrying"]).limit(1).maybeSingle(),
  ]);
  if (latest.error || success.error || active.error) return Response.json({ error: "Sync status unavailable. Check that migration 010 is applied." }, { status: 503 });
  return Response.json({ job: active.data ?? latest.data, lastSuccess: success.data }, { headers: { "Cache-Control": "private, no-store" } });
}
