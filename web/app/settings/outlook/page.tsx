import { requireMember } from "@/lib/auth";
import { adminClient } from "@/lib/supabase";
import { cancel, checkConnection, connect, disconnect, retryCleanup } from "./actions";
import { Navigation } from "../../navigation";

const notices: Record<string, string> = {
  connected: "Outlook connected. New previews will use the mailbox shown below.",
  disconnected: "Disconnected locally. Future sends are blocked; an email already being dispatched may still complete.",
  start_failed: "Could not start authorization. Wait 30 seconds and retry. If this continues, ask your administrator to check the Outlook configuration.",
  callback_failed: "Authorization was cancelled, expired, superseded, or could not be verified. Retry the callback after a temporary error, or start a new connection.",
  save_failed: "Could not save your change. Refresh to check the connection before retrying.",
  verified: "The saved Outlook account and sender address were verified successfully.",
  check_failed: "Could not verify the saved connection. It may need reconnecting, or the provider may be temporarily unavailable.",
  cleanup_done: "Cleanup processed. Any remaining accounts are listed below.",
  cleanup_failed: "Some provider accounts could not be disabled. Retry cleanup; they cannot be selected for new sends in this app.",
};
export default async function Page({ searchParams }: { searchParams: Promise<{ notice?: string }> }) {
  const { user, db } = await requireMember();
  const admin = adminClient();
  const [connection, attempt, cleanup] = await Promise.all([
    db.from("outlook_connections").select("mailbox_email,status,verified_at").eq("user_id", user.id).maybeSingle(),
    admin.from("outlook_auth_attempts").select("id,expires_at").eq("user_id", user.id).eq("status", "pending").maybeSingle(),
    admin.from("outlook_account_cleanup").select("account_id", { count: "exact", head: true }).eq("user_id", user.id).is("completed_at", null),
  ]);
  const { notice } = await searchParams;
  const failed = connection.error || attempt.error || cleanup.error;
  const c = connection.data;
  return <><Navigation current="outlook" /><div className="page-heading"><div><div className="eyebrow">Settings</div><h1>Outlook connection</h1><p>Manage the mailbox you use for personal outreach.</p></div></div><section className="settings-panel"><h2>Your sending account</h2>
    <p>Signed in as: <strong>{user.email}</strong></p>
    <p>Your Outlook address can be different from your app login email. Microsoft verifies the mailbox; the sender address is read-only.</p>
    {notice && notices[notice] && <p className="notice" role="status">{notices[notice]}</p>}
    {failed ? <p className="error">Could not load connection settings. Check that the self-service database migration has been applied.</p> : <>
      <div className="connection-summary"><strong>{c?.mailbox_email || "No mailbox connected"}</strong><span className={`badge badge-${c?.status || "inactive"}`}>{c?.status || "Not connected"}</span></div>
      {c && <small className="verification-time">Last connection verification: {new Date(c.verified_at).toLocaleString()}</small>}
      {attempt.data && <p>Authorization {Date.parse(attempt.data.expires_at) > Date.now() ? "in progress" : "expired"}. Complete it in the original tab, cancel it, or start again.</p>}
      <div className="row"><form action={connect}><button>{c ? "Reconnect Outlook" : "Connect Outlook"}</button></form>
      {c?.status === "active" && <form action={checkConnection}><button className="secondary">Check connection</button></form>}
      {attempt.data && <form action={cancel}><button className="secondary">Cancel authorization</button></form>}</div>
      {c?.status === "active" && <form className="settings-block" action={disconnect}><h2>Disconnect mailbox</h2><p className="muted">Disconnect blocks future sends. Already-dispatching emails may still complete.</p><button className="danger">Disconnect Outlook</button></form>}
      {!!cleanup.count && <aside><p>{cleanup.count} old or cancelled provider account(s) need cleanup. Local sending through these accounts is blocked.</p><form action={retryCleanup}><button>Retry provider cleanup</button></form></aside>}
      <p className="field-help">Reconnecting keeps campaign history. If your account changes, create a new preview for any unsent messages; existing campaigns never switch senders automatically.</p>
    </>}
  </section></>;
}
