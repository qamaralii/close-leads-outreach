import { createHash } from "node:crypto";
import { z } from "zod";
import { requireMember } from "@/lib/auth";
import { adminClient } from "@/lib/supabase";
import { verifiedFile } from "@/lib/attachments";
import { ATTACHMENT_BUCKET } from "@/lib/delivery";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ id: string }> };
export async function POST(_: Request, context: Context) {
  const { user } = await requireMember(), { id } = await context.params;
  if (!z.uuid().safeParse(id).success) return Response.json({ error: "Invalid upload." }, { status: 400 });
  const db = adminClient(), bucket = db.storage.from(ATTACHMENT_BUCKET);
  const { data: file, error } = await db.from("outreach_attachments").select("id,name,size_bytes,content_type,sha256,status").eq("id", id).eq("created_by", user.id).single();
  if (error || !file || file.status === "deleting") return Response.json({ error: "Upload unavailable." }, { status: 404 });
  try {
    if (file.status !== "ready") {
      const destination = `files/${user.id}/${id}`;
      // Move first: previously issued staging tokens cannot overwrite final bytes.
      await bucket.move(`staging/${user.id}/${id}`, destination);
      const { data: blob, error: downloadError } = await bucket.download(destination);
      if (downloadError || !blob || blob.size !== file.size_bytes) throw Error();
      const hash = createHash("sha256").update(Buffer.from(await blob.arrayBuffer())).digest("hex");
      const { error: finalizeError } = await db.rpc("register_outreach_attachment", { p_user_id: user.id, p_id: id, p_name: file.name, p_size: file.size_bytes, p_type: file.content_type, p_sha256: hash });
      if (finalizeError) throw Error();
    }
    // A retry may have recreated a staging object after the first move succeeded.
    await bucket.remove([`staging/${user.id}/${id}`]);
    return Response.json({ file: { id, name: file.name, size_bytes: file.size_bytes, content_type: file.content_type } });
  } catch { return Response.json({ error: "Could not verify the uploaded file. Retry to finish the upload." }, { status: 409 }); }
}
export async function GET(_: Request, context: Context) {
  const { user } = await requireMember(), { id } = await context.params;
  if (!z.uuid().safeParse(id).success) return new Response("Not found", { status: 404 });
  const { data: file } = await adminClient().from("outreach_attachments").select("id,name,size_bytes,content_type,sha256").eq("id", id).eq("created_by", user.id).eq("status", "ready").single();
  if (!file) return new Response("Not found", { status: 404 });
  try {
    const bytes = await verifiedFile(user.id, file);
    return new Response(new Uint8Array(bytes), { headers: { "Content-Type": "application/octet-stream", "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(file.name)}`, "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff" } });
  } catch { return new Response("Attachment unavailable", { status: 409 }); }
}
