"use server";
import { requireMember } from "@/lib/auth";
import { adminClient } from "@/lib/supabase";
import { requestSchema, resolvePreview, validateMessage, type Lead } from "@/lib/preview";
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
  if (connectionError || connection?.status !== "active") return { error: "Connect Outlook from the dashboard before creating a preview." };
  const resolved = await resolveRequest(request, db);
  if ("error" in resolved) return resolved;
  const { data: id, error } = await adminClient().rpc("prepare_app_preview", {
    p_user_id: user.id, p_request_id: request.requestId, p_sender_connection_id: connection.id,
    p_subject_template: request.subject, p_body_template: request.body,
    p_recipients: resolved.recipients, p_app_request: request, p_exclusions: resolved.excluded,
  });
  if (error || typeof id !== "string") return { error: "Preview save could not be confirmed. Retry unchanged input; the same request ID will be retained." };
  revalidatePath("/");
  return { campaignId: id };
}

async function resolveRequest(request: z.infer<typeof requestSchema>, db: Awaited<ReturnType<typeof requireMember>>["db"]) {
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
  return resolved;
}

const editSchema = z.object({ campaignId: z.uuid(), revision: z.number().int().positive(), editId: z.uuid() }).strict();
const messageSchema = editSchema.extend({ recipientId: z.uuid(), reset: z.boolean(), subject: z.string().max(998), body: z.string().max(100000) });

export async function editCampaign(input: unknown, previewInput: unknown): Promise<{ error?: string; campaignId?: string }> {
  const { user, db } = await requireMember();
  const parsed = editSchema.safeParse(input), request = requestSchema.safeParse(previewInput);
  if (!parsed.success || !request.success) return { error: "Invalid draft changes. Check your selection and message." };
  const { data: campaign } = await db.from("campaigns").select("request_id,status,revision,last_edit").eq("id", parsed.data.campaignId).eq("created_by", user.id).single();
  if (!campaign || campaign.status !== "draft") return { error: "This campaign is no longer an editable draft." };
  request.data.requestId = campaign.request_id;
  if (campaign.last_edit?.editId === parsed.data.editId) {
    const previous = requestSchema.safeParse(campaign.last_edit.request);
    if (previous.success && JSON.stringify(previous.data) === JSON.stringify(request.data)) return { campaignId: parsed.data.campaignId };
    return { error: "Edit ID conflict. Refresh before saving." };
  }
  if (campaign.revision !== parsed.data.revision) return { error: "This draft changed in another tab. Refresh before editing." };
  const resolved = await resolveRequest(request.data, db);
  if ("error" in resolved) return resolved;
  return saveEdit(user.id, parsed.data, { operation: "campaign", editId: parsed.data.editId, request: request.data, recipients: resolved.recipients, exclusions: resolved.excluded });
}

export async function editMessage(input: unknown): Promise<{ error?: string; campaignId?: string }> {
  const { user } = await requireMember();
  const parsed = messageSchema.safeParse(input);
  if (!parsed.success) return { error: "Invalid message changes." };
  const edit = parsed.data;
  if (!edit.reset) {
    try { validateMessage(edit.subject, edit.body); }
    catch (error) { return { error: error instanceof Error ? error.message : "Invalid message." }; }
  }
  return saveEdit(user.id, edit, { operation: edit.reset ? "reset" : "message", editId: edit.editId, recipientId: edit.recipientId, subject: edit.subject, body: edit.body });
}

async function saveEdit(userId: string, edit: z.infer<typeof editSchema>, payload: object) {
  const { data, error } = await adminClient().rpc("edit_outreach_draft", { p_user_id: userId, p_campaign_id: edit.campaignId, p_revision: edit.revision, p_edit: payload });
  if (error || typeof data !== "string") return { error: error?.message.includes("Draft changed") ? "This draft changed. Refresh and review the latest version before saving." : "Changes could not be confirmed. Retry unchanged input or refresh to check the draft." };
  revalidatePath(`/campaigns/${edit.campaignId}`); revalidatePath(`/campaigns/${edit.campaignId}/edit`); revalidatePath("/");
  return { campaignId: data };
}

export async function confirm(_: string, form: FormData) {
  const { user } = await requireMember();
  const id = z.uuid().safeParse(form.get("campaignId"));
  const revision = z.coerce.number().int().positive().safeParse(form.get("revision"));
  if (!id.success || !revision.success || form.get("reviewed") !== "yes") return "Review the saved messages and check the confirmation box.";
  const { error } = await adminClient().rpc("confirm_outreach_revision", { p_user_id: user.id, p_campaign_id: id.data, p_revision: revision.data });
  if (error?.message.includes("Draft changed")) return "This draft changed. Refresh and review the updated messages before confirming.";
  if (error) return "Submission could not be confirmed. Refresh to check its status before retrying the same campaign.";
  revalidatePath(`/campaigns/${id.data}`);
  redirect(`/campaigns/${id.data}`);
}
