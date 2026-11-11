import { notFound } from "next/navigation";
import { z } from "zod";
import { requireMember } from "@/lib/auth";
import { Controls } from "./controls";
import { Navigation } from "../../navigation";
import { MessageCard, ReviewProvider } from "./message";
import { formatSchedule } from "@/lib/scheduling";
export default async function Page({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ page?: string }> }) {
  const { db, user } = await requireMember();
  const { id } = await params;
  if (!z.uuid().safeParse(id).success) notFound();
  const { data: campaign, error } = await db.from("campaigns").select("id,status,revision,app_request,send_mode,scheduled_at,timezone,sender_connection_id,sender_entity_snapshot,sender_account_snapshot,sender_mailbox_snapshot,expected_recipient_count,preview_exclusions,created_at").eq("id", id).eq("created_by", user.id).single();
  if (error || !campaign) notFound();
  const { data: connection } = await db.from("outlook_connections").select("id,status,composio_entity_id,composio_connected_account_id,mailbox_email").eq("user_id", user.id).maybeSingle();
  const senderMatches = connection?.status === "active" && connection.id === campaign.sender_connection_id && connection.composio_entity_id === campaign.sender_entity_snapshot && connection.composio_connected_account_id === campaign.sender_account_snapshot && connection.mailbox_email.trim().toLowerCase() === campaign.sender_mailbox_snapshot.trim().toLowerCase();
  const raw = Number((await searchParams).page ?? 0), page = Number.isSafeInteger(raw) && raw >= 0 ? raw : 0;
  const { data: recipients, error: recipientsError } = await db.from("campaign_recipients").select("id,recipient_email,rendered_subject,rendered_body,status,error,customized").eq("campaign_id", id).order("id").range(page * 25, page * 25 + 24);
  return <ReviewProvider key={`${id}:${campaign.revision}:${campaign.status}`}><Navigation current="campaigns" /><div className="page-heading"><div><div className="eyebrow">{campaign.status === "draft" ? `Review & confirm · Version ${campaign.revision}` : "Campaign activity"}</div><h1>{campaign.status === "draft" ? "Review saved preview" : campaign.status === "scheduled" ? "Scheduled campaign" : "Campaign results"}</h1><p>{campaign.status === "draft" ? "Check your recipients and personalized messages before sending." : campaign.status === "scheduled" ? "Your campaign is confirmed for later. Manage its schedule below." : "Track the latest outcomes for each recipient."}</p></div>{campaign.status === "draft" && campaign.app_request && <a className="button secondary" href={`/campaigns/${id}/edit`}>Edit campaign</a>}</div><section className="campaign-summary"><div className="panel-heading"><h2>Campaign overview</h2><span className={`badge badge-${campaign.status}`}>{campaign.status}</span></div><p>Sender: <strong>{campaign.sender_mailbox_snapshot}</strong></p><p>{campaign.expected_recipient_count} recipients</p>{campaign.scheduled_at && campaign.timezone && <p>Scheduled start: <strong>{formatSchedule(campaign.scheduled_at, campaign.timezone)}</strong> ({campaign.timezone})</p>}<small>Campaign ID: {id}</small>
    {!senderMatches && <p className="error">The saved sender is disconnected or has changed. Any queued recipients are blocked from new send authorization. <a href="/settings/outlook">Check Outlook</a> and create a new preview for unsent messages. Already dispatched messages are not recalled.</p>}
    {!recipientsError && recipients?.length ? <Controls id={id} revision={campaign.revision} draft={campaign.status === "draft"} scheduled={campaign.status === "scheduled"} scheduledAt={campaign.scheduled_at} timezone={campaign.timezone} locked={!!campaign.scheduled_at && Date.parse(campaign.scheduled_at) <= Date.now()} blocked={!senderMatches} /> : <p className="error">Load the saved messages before confirming.</p>}</section>
    {Array.isArray(campaign.preview_exclusions) && campaign.preview_exclusions.length > 0 && <section><h2>Excluded contacts</h2><ul>{campaign.preview_exclusions.map((e: {close_lead_id:string;reason:string}) => <li key={e.close_lead_id}>{e.close_lead_id}: {e.reason}</li>)}</ul></section>}
    {recipientsError && <p className="error">Could not load saved recipients. Refresh before confirming.</p>}
    {recipients?.map(r => <MessageCard key={`${r.id}:${campaign.revision}`} recipient={r} campaignId={id} revision={campaign.revision} draft={campaign.status === "draft"} />)}
    <div className="row">{page > 0 && <a href={`?page=${page - 1}`}>Previous</a>}<span>Page {page + 1}</span>{(page + 1) * 25 < campaign.expected_recipient_count && <a href={`?page=${page + 1}`}>Next</a>}</div></ReviewProvider>;
}
