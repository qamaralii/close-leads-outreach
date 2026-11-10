"use server";
import { requireMember } from "@/lib/auth";
import { adminClient } from "@/lib/supabase";
import { requestSchema, resolvePreview, type Lead } from "@/lib/preview";
import { z } from "zod";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";

export async function prepare(input: unknown): Promise<{ error?: string; campaignId?: string }> {
  const { user, db } = await requireMember();
  const parsed = requestSchema.safeParse(input);
  if (!parsed.success) return { error: "Invalid preview request. Select up to 1,000 contacts and check message length." };
  const request = parsed.data;
  // A replay returns the immutable saved preview before resolving changing contact data.
  const { data: existing, error: lookupError } = await db.from("campaigns").select("id,app_request").eq("created_by", user.id).eq("request_id", request.requestId).maybeSingle();
  if (lookupError) return { error: "Could not check existing previews. Retry with the same request." };
  if (existing) {
    const old = requestSchema.safeParse(existing.app_request);
    if (!old.success || JSON.stringify(old.data) !== JSON.stringify(request)) return { error: "Request ID conflicts with another preview. Start a new preview." };
    return { campaignId: existing.id };
  }
  const { data: connection, error: connectionError } = await db.from("outlook_connections").select("id,status,mailbox_email").eq("user_id", user.id).single();
  if (connectionError || connection?.status !== "active") return { error: "An active Outlook connection is required. Ask your administrator to configure your mailbox." };
  const admin = adminClient();
  const leads: Lead[] = [];
  for (let start = 0; start < request.leadIds.length; start += 50) {
    const batch = request.leadIds.slice(start, start + 50);
    for (let offset = 0; ; ) {
      const { data, error } = await db.from("close_leads").select("close_lead_id,company_name,contact_name,first_name,email").in("close_lead_id", batch).order("close_lead_id").range(offset, offset + 49);
      if (error) return { error: "Contact lookup failed." };
      if (!data.length) break;
      leads.push(...data); offset += data.length;
    }
  }
  const emails = [...new Set(leads.map(l => l.email?.trim().toLowerCase()).filter((s): s is string => !!s))];
  const suppressed = new Set<string>();
  for (let start = 0; start < emails.length; start += 50) {
    for (let offset = 0; ; ) {
      const { data, error } = await admin.from("email_suppressions").select("normalized_email").in("normalized_email", emails.slice(start, start + 50)).order("normalized_email").range(offset, offset + 49);
      if (error) return { error: "Suppression check failed." };
      if (!data.length) break;
      data.forEach(r => suppressed.add(r.normalized_email)); offset += data.length;
    }
  }
  let resolved;
  try { resolved = resolvePreview(request, leads, suppressed); }
  catch (error) { return { error: error instanceof Error ? error.message : "Preview resolution failed." }; }
  const { data: id, error } = await admin.rpc("prepare_app_preview", {
    p_user_id: user.id, p_request_id: request.requestId, p_sender_connection_id: connection.id,
    p_subject_template: request.subject, p_body_template: request.body,
    p_recipients: resolved.recipients, p_app_request: request, p_exclusions: resolved.excluded,
  });
  if (error || typeof id !== "string") return { error: "Preview save could not be confirmed. Retry unchanged input; the same request ID will be retained." };
  revalidatePath("/");
  return { campaignId: id };
}

export async function confirm(_: string, form: FormData) {
  const { user } = await requireMember();
  const id = z.uuid().safeParse(form.get("campaignId"));
  if (!id.success || form.get("reviewed") !== "yes") return "Review the saved messages and check the confirmation box.";
  const { error } = await adminClient().rpc("submit_outreach_campaign", { p_user_id: user.id, p_campaign_id: id.data });
  if (error) return "Submission could not be confirmed. Refresh to check its status before retrying the same campaign.";
  revalidatePath(`/campaigns/${id.data}`);
  redirect(`/campaigns/${id.data}`);
}
