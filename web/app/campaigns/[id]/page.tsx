import { notFound } from "next/navigation";
import { z } from "zod";
import { requireMember } from "@/lib/auth";
import { Controls } from "./controls";
export default async function Page({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ page?: string }> }) {
  const { db, user } = await requireMember();
  const { id } = await params;
  if (!z.uuid().safeParse(id).success) notFound();
  const { data: campaign, error } = await db.from("campaigns").select("id,status,sender_connection_id,sender_entity_snapshot,sender_account_snapshot,sender_mailbox_snapshot,expected_recipient_count,preview_exclusions,created_at").eq("id", id).eq("created_by", user.id).single();
  if (error || !campaign) notFound();
  const { data: connection } = await db.from("outlook_connections").select("id,status,composio_entity_id,composio_connected_account_id,mailbox_email").eq("user_id", user.id).maybeSingle();
  const senderMatches = connection?.status === "active" && connection.id === campaign.sender_connection_id && connection.composio_entity_id === campaign.sender_entity_snapshot && connection.composio_connected_account_id === campaign.sender_account_snapshot && connection.mailbox_email.trim().toLowerCase() === campaign.sender_mailbox_snapshot.trim().toLowerCase();
  const raw = Number((await searchParams).page ?? 0), page = Number.isSafeInteger(raw) && raw >= 0 ? raw : 0;
  const { data: recipients, error: recipientsError } = await db.from("campaign_recipients").select("id,recipient_email,rendered_subject,rendered_body,status,error").eq("campaign_id", id).order("id").range(page * 25, page * 25 + 24);
  return <><nav><a href="/">Campaigns</a><a href="/compose">New preview</a></nav><section><h1>{campaign.status === "draft" ? "Review saved preview" : "Campaign results"}</h1><p>Sender: <strong>{campaign.sender_mailbox_snapshot}</strong></p><p>{campaign.expected_recipient_count} recipients · {campaign.status}</p><small>{id}</small>
    {!senderMatches && <p className="error">The saved sender is disconnected or has changed. Any queued recipients are blocked from new send authorization. <a href="/settings/outlook">Check Outlook</a> and create a new preview for unsent messages. Already dispatched messages are not recalled.</p>}
    {!recipientsError && recipients?.length ? <Controls id={id} draft={campaign.status === "draft"} blocked={!senderMatches} /> : <p className="error">Load the saved messages before confirming.</p>}</section>
    {Array.isArray(campaign.preview_exclusions) && campaign.preview_exclusions.length > 0 && <section><h2>Excluded contacts</h2><ul>{campaign.preview_exclusions.map((e: {close_lead_id:string;reason:string}) => <li key={e.close_lead_id}>{e.close_lead_id}: {e.reason}</li>)}</ul></section>}
    {recipientsError && <p className="error">Could not load saved recipients. Refresh before confirming.</p>}
    {recipients?.map(r => <article key={r.id}><h2>{r.recipient_email}</h2><p>Status: <strong>{r.status}</strong></p><h3>{r.rendered_subject}</h3><pre>{r.rendered_body}</pre>{r.error && <p className="error">{r.error}</p>}</article>)}
    <div className="row">{page > 0 && <a href={`?page=${page - 1}`}>Previous</a>}<span>Page {page + 1}</span>{(page + 1) * 25 < campaign.expected_recipient_count && <a href={`?page=${page + 1}`}>Next</a>}</div></>;
}
