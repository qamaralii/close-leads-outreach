import "server-only";
import { adminClient } from "./supabase";
import { ATTACHMENT_BUCKET, MAX_ATTACHMENT_BYTES, type Attachment } from "./delivery";
import { createHash } from "node:crypto";

export async function loadAttachments(userId: string, ids: string[]): Promise<Attachment[]> {
  if (!ids.length) return [];
  const files: Attachment[] = [];
  for (let offset = 0; offset < ids.length; offset += 50) {
    const batch = ids.slice(offset, offset + 50);
    const { data, error } = await adminClient().from("outreach_attachments").select("id,name,size_bytes,content_type").eq("created_by", userId).eq("status", "ready").in("id", batch);
    if (error || data?.length !== batch.length) throw Error("One or more attachments are unavailable. Remove them or upload again.");
    files.push(...data);
  }
  return ids.map(id => files.find(file => file.id === id)!);
}
export async function validateAttachments(userId: string, ids: string[]) {
  const files = await loadAttachments(userId, ids);
  if (files.reduce((sum, f) => sum + f.size_bytes, 0) > MAX_ATTACHMENT_BYTES) throw Error("Attachments exceed 25 MB per email.");
  if (new Set(files.map(f => f.name.toLowerCase())).size !== files.length) throw Error("Use distinct attachment filenames in each email.");
  return files;
}
export async function verifiedFile(userId: string, file: Attachment & { sha256: string | null }) {
  const { data, error } = await adminClient().storage.from(ATTACHMENT_BUCKET).download(`files/${userId}/${file.id}`);
  if (error || !data || data.size !== file.size_bytes) throw Error("Attachment is missing or changed.");
  const bytes = Buffer.from(await data.arrayBuffer());
  if (createHash("sha256").update(bytes).digest("hex") !== file.sha256) throw Error("Attachment is missing or changed.");
  return bytes;
}
