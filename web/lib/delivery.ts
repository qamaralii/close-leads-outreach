import { z } from "zod";

export const MAX_ATTACHMENT_BYTES = 25_000_000;
export const MAX_ATTACHMENTS = 20;
export const ATTACHMENT_BUCKET = "outreach-attachments";
export const ccSchema = z.array(z.string().trim().email().max(254).transform(value => value.toLowerCase())).max(50).transform(values => [...new Set(values)]);
export const attachmentIdsSchema = z.array(z.uuid()).max(MAX_ATTACHMENTS).refine(values => new Set(values).size === values.length, "Duplicate attachments.");
export const deliverySchema = z.object({ cc: ccSchema.default([]), attachmentIds: attachmentIdsSchema.default([]) });
export type Attachment = { id: string; name: string; size_bytes: number; content_type: string };
export type Delivery = { cc: string[]; attachmentIds: string[] };
export function effectiveDelivery(defaults: Delivery, cc: string[] | null, ids: string[] | null, to: string): Delivery {
  return { cc: (cc ?? defaults.cc).filter(value => value.toLowerCase() !== to.trim().toLowerCase()), attachmentIds: ids ?? defaults.attachmentIds };
}
export function formatFileSize(bytes: number) { return bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.ceil(bytes / 1000)} KB`; }
