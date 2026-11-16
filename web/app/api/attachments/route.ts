import { z } from "zod";
import { requireMember } from "@/lib/auth";
import { adminClient } from "@/lib/supabase";
import { ATTACHMENT_BUCKET, attachmentIdsSchema, MAX_ATTACHMENT_BYTES } from "@/lib/delivery";
import { loadAttachments } from "@/lib/attachments";

export const dynamic = "force-dynamic";
const uploadSchema = z.object({ id: z.uuid(), name: z.string().min(1).max(200).regex(/^[^\x00-\x1f\x7f/\\]+$/), size: z.number().int().positive().max(MAX_ATTACHMENT_BYTES), contentType: z.string().min(1).max(150).regex(/^[^\x00-\x1f\x7f]+$/) }).strict();
export async function POST(request: Request) {
  const { user } = await requireMember();
  const parsed = uploadSchema.safeParse(await request.json().catch(() => null));
  if (!parsed.success) return Response.json({ error: "Choose a file up to 25 MB with a valid filename." }, { status: 400 });
  const input = parsed.data, db = adminClient();
  const { error } = await db.rpc("register_outreach_attachment", { p_user_id: user.id, p_id: input.id, p_name: input.name, p_size: input.size, p_type: input.contentType });
  if (error) return Response.json({ error: "Could not register upload. Retry the same file." }, { status: 400 });
  const { data: existing } = await db.from("outreach_attachments").select("status").eq("id", input.id).eq("created_by", user.id).single();
  if (existing?.status === "ready") return Response.json({ ready: true });
  const { data, error: signError } = await db.storage.from(ATTACHMENT_BUCKET).createSignedUploadUrl(`staging/${user.id}/${input.id}`);
  if (signError || !data) return Response.json({ error: "Upload storage is unavailable. Retry shortly." }, { status: 503 });
  return Response.json({ uploadUrl: data.signedUrl, uploadHeaders: { apikey: process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY } }, { headers: { "Cache-Control": "private, no-store" } });
}
export async function GET(request: Request) {
  const { user } = await requireMember();
  const parsed = attachmentIdsSchema.safeParse(new URL(request.url).searchParams.get("ids")?.split(",").filter(Boolean) ?? []);
  if (!parsed.success) return Response.json({ error: "Invalid attachment selection." }, { status: 400 });
  try { return Response.json({ files: await loadAttachments(user.id, parsed.data) }, { headers: { "Cache-Control": "private, no-store" } }); }
  catch { return Response.json({ error: "Attachments unavailable." }, { status: 404 }); }
}
