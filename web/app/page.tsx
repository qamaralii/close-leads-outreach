import { requireMember } from "@/lib/auth";
import { logout } from "./auth/actions";
import { Navigation } from "./navigation";
export const dynamic = "force-dynamic";
export default async function Page() {
  const { user, db } = await requireMember();
  const [{ data: campaigns, error }, { data: connection }] = await Promise.all([
    db.from("campaigns").select("id,request_id,status,subject_template,expected_recipient_count,created_at").eq("created_by", user.id).order("created_at", { ascending: false }).limit(100),
    db.from("outlook_connections").select("mailbox_email,status").eq("user_id", user.id).maybeSingle(),
  ]);
  return <><Navigation current="campaigns"><div className="nav-account"><span>{user.email}</span><form action={logout}><button className="quiet">Sign out</button></form></div></Navigation>
    <div className="page-heading"><div><div className="eyebrow">Your workspace</div><h1>Campaigns</h1><p>Create personal outreach and follow every recipient’s progress.</p></div><a className="button" href="/compose">+ New campaign</a></div>
    <div className="connection-summary"><span className={`badge badge-${connection?.status || "inactive"}`}>{connection?.status || "Not connected"}</span><span>{connection?.mailbox_email || "Connect Outlook to start sending."}</span><a href="/settings/outlook">{connection ? "Manage Outlook" : "Connect Outlook"} →</a></div>
    <section className="campaign-panel" aria-label="Your campaigns">
    {error && <p className="error">Could not load campaigns.</p>}
    {!!campaigns?.length && <div className="table-scroll"><table><thead><tr><th>Campaign subject</th><th>Status</th><th>Recipients</th></tr></thead><tbody>{campaigns.map(c => <tr key={c.id}><td><a href={`/campaigns/${c.id}`}>{c.subject_template}</a></td><td><span className={`badge badge-${c.status}`}>{c.status}</span></td><td>{c.expected_recipient_count}</td></tr>)}</tbody></table></div>}
    {!campaigns?.length && !error && <div className="empty-state"><h2>Your first conversation starts here</h2><p>Choose contacts, personalize your message, and review it before sending.</p><a className="button secondary" href="/compose">Create a campaign</a></div>}<div className="panel-footer">Latest 100 campaigns. Submitted means queued for execution; open a campaign for recipient outcomes.</div></section></>;
}
