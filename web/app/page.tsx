import { requireMember } from "@/lib/auth";
import { logout } from "./auth/actions";
export const dynamic = "force-dynamic";
export default async function Page() {
  const { user, db } = await requireMember();
  const [{ data: campaigns, error }, { data: connection }] = await Promise.all([
    db.from("campaigns").select("id,request_id,status,subject_template,expected_recipient_count,created_at").eq("created_by", user.id).order("created_at", { ascending: false }).limit(100),
    db.from("outlook_connections").select("mailbox_email,status").eq("user_id", user.id).maybeSingle(),
  ]);
  return <><nav><a href="/compose">New campaign</a><span>{user.email}</span><form action={logout}><button>Sign out</button></form></nav>
    <section><h1>Your campaigns</h1><p>Outlook: {connection ? `${connection.mailbox_email} (${connection.status})` : "Not connected — ask your administrator to register your mailbox."}</p>
    {error && <p className="error">Could not load campaigns.</p>}
    <table><thead><tr><th>Subject</th><th>Status</th><th>Recipients</th></tr></thead><tbody>{campaigns?.map(c => <tr key={c.id}><td><a href={`/campaigns/${c.id}`}>{c.subject_template}</a></td><td>{c.status}</td><td>{c.expected_recipient_count}</td></tr>)}</tbody></table>
    {!campaigns?.length && !error && <p>No campaigns yet.</p>}<small>Latest 100 campaigns. Submitted means queued for execution; open a campaign for recipient outcomes.</small></section></>;
}
