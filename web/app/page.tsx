import { requireMember } from "@/lib/auth";
import { logout } from "./auth/actions";
import { Navigation } from "./navigation";
import { formatSchedule } from "@/lib/scheduling";
export const dynamic = "force-dynamic";
export default async function Page() {
  const { user, db } = await requireMember();
  const [{ data: campaigns, error }, { data: connection }] = await Promise.all([
    db.from("campaigns").select("id,request_id,status,subject_template,expected_recipient_count,created_at,scheduled_at,timezone").eq("created_by", user.id).order("created_at", { ascending: false }).limit(100),
    db.from("outlook_connections").select("mailbox_email,status").eq("user_id", user.id).maybeSingle(),
  ]);
  const drafts = campaigns?.filter(c => c.status === "draft").length ?? 0;
  const active = campaigns?.filter(c => c.status === "scheduled" || c.status === "submitted").length ?? 0;
  return <><Navigation current="campaigns"><div className="nav-account"><span>{user.email}</span><form action={logout}><button className="quiet">Sign out</button></form></div></Navigation>
    <div className="page-heading"><div><div className="eyebrow">Outreach workspace</div><h1>Campaigns</h1><p>Create thoughtful outreach and follow every recipient from draft to send.</p></div><a className="button primary-action" href="/compose"><span aria-hidden="true">＋</span> New campaign</a></div>
    <div className="dashboard-grid"><div className="stat-card"><span>Total campaigns</span><strong>{campaigns?.length ?? 0}</strong><small>Latest 100</small></div><div className="stat-card"><span>Drafts to review</span><strong>{drafts}</strong><small>Not yet queued</small></div><div className="stat-card"><span>Active sends</span><strong>{active}</strong><small>Scheduled or submitted</small></div><div className="connection-card"><div><span className="card-label">Sending account</span><strong>{connection?.mailbox_email || "Outlook not connected"}</strong></div><span className={`badge badge-${connection?.status || "inactive"}`}>{connection?.status || "Not connected"}</span><a href="/settings/outlook">{connection ? "Manage" : "Connect"}<span aria-hidden="true"> →</span></a></div></div>
    <section className="campaign-panel" aria-label="Your campaigns">
    <div className="panel-title"><div><h2>Recent campaigns</h2><p>Open a campaign to review content, manage its schedule, or see outcomes.</p></div></div>
    {error && <p className="error">Could not load campaigns.</p>}
    {!!campaigns?.length && <><p className="table-hint">Scroll to see all campaign details.</p><div className="table-scroll" tabIndex={0} role="region" aria-label="Recent campaigns"><table><thead><tr><th>Campaign subject</th><th>Status</th><th>Recipients</th><th>Scheduled start</th></tr></thead><tbody>{campaigns.map(c => <tr key={c.id}><td className="campaign-subject"><a href={`/campaigns/${c.id}`}>{c.subject_template}</a><small>Created {new Date(c.created_at).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}</small></td><td><span className={`badge badge-${c.status}`}>{c.status}</span></td><td><strong>{c.expected_recipient_count.toLocaleString()}</strong></td><td>{c.scheduled_at && c.timezone ? <>{formatSchedule(c.scheduled_at, c.timezone)}<small className="contact-email">{c.timezone.replaceAll("_", " ")}</small></> : <span className="muted">Not scheduled</span>}</td></tr>)}</tbody></table></div></>}
    {!campaigns?.length && !error && <div className="empty-state"><h2>Your first conversation starts here</h2><p>Choose contacts, personalize your message, and review it before sending.</p><a className="button secondary" href="/compose">Create a campaign</a></div>}<div className="panel-footer">Latest 100 campaigns. Submitted means queued for execution; open a campaign for recipient outcomes.</div></section></>;
}
