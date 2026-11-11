import { notFound, redirect } from "next/navigation";
import { z } from "zod";
import { requireMember } from "@/lib/auth";
import { requestSchema } from "@/lib/preview";
import { Composer } from "../../../compose/ui";
import { Navigation } from "../../../navigation";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { db, user } = await requireMember();
  const { id } = await params;
  if (!z.uuid().safeParse(id).success) notFound();
  const { data: campaign } = await db.from("campaigns").select("id,status,revision,app_request,sender_mailbox_snapshot").eq("id", id).eq("created_by", user.id).single();
  if (!campaign) notFound();
  if (campaign.status !== "draft") redirect(`/campaigns/${id}`);
  const request = requestSchema.safeParse(campaign.app_request);
  if (!request.success) return <><Navigation current="campaigns" /><p className="error">This legacy draft has no saved composer selection. Individual messages can still be edited on the review page.</p><a href={`/campaigns/${id}`}>Back to review</a></>;
  const { data: savedContacts } = await db.from("campaign_recipients").select("close_lead_id,recipient_name,recipient_email").eq("campaign_id", id).order("id").range(0, 999);
  return <><Navigation current="campaigns" /><div className="page-heading"><div><div className="eyebrow">Edit draft · Version {campaign.revision}</div><h1>Edit campaign</h1><p>Update contacts and templates, then review again. Sender: {campaign.sender_mailbox_snapshot}</p></div></div><Composer key={`${id}:${campaign.revision}`} draft={{ id, revision: campaign.revision, ...request.data, savedContacts: savedContacts ?? [] }} /></>;
}
