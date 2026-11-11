import { requireMember } from "@/lib/auth";
import { adminClient } from "@/lib/supabase";
import { cancel, checkConnection, connect, disconnect, retryCleanup } from "./actions";
import { Navigation } from "../../navigation";
import { SubmitButton } from "../../submit-button";

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
  const negativeNotice = notice?.includes("failed");
  return <><Navigation current="outlook" /><div className="page-heading"><div><div className="eyebrow">Workspace settings</div><h1>Outlook connection</h1><p>Connect and verify the mailbox used to send your campaigns.</p></div></div><div className="settings-layout"><aside className="settings-aside"><span className="settings-icon" aria-hidden="true">O</span><h2>One verified sender</h2><p>Every campaign keeps a snapshot of the mailbox you reviewed. Changing accounts never silently changes an existing campaign.</p><dl><div><dt>App account</dt><dd>{user.email}</dd></div><div><dt>Provider</dt><dd>Microsoft Outlook</dd></div></dl></aside><section className="settings-panel"><div className="panel-heading"><div><span className="card-label">Sending account</span><h2>{c ? "Connected mailbox" : "Connect your mailbox"}</h2></div><span className={`badge badge-${c?.status || "inactive"}`}>{c?.status || "Not connected"}</span></div>
    <p>Your Outlook address may differ from your app login. Microsoft verifies the mailbox and the sender address remains read-only.</p>
    {notice && notices[notice] && <p className={negativeNotice ? "error" : "notice notice-success"} role={negativeNotice ? "alert" : "status"}>{notices[notice]}</p>}
    {failed ? <p className="error" role="alert">Could not load connection settings. Check that the self-service database migration has been applied.</p> : <>
      <div className="mailbox-card"><span>Mailbox</span><strong>{c?.mailbox_email || "No mailbox connected"}</strong><small>{c ? "Used for new campaign previews" : "Connect Outlook before preparing a campaign"}</small></div>
      {c && <small className="verification-time">Last connection verification: {new Date(c.verified_at).toLocaleString()}</small>}
      {attempt.data && <p>Authorization {Date.parse(attempt.data.expires_at) > Date.now() ? "in progress" : "expired"}. Complete it in the original tab, cancel it, or start again.</p>}
      <div className="row"><form action={connect}><SubmitButton pendingLabel="Opening Microsoft…">{c ? "Reconnect Outlook" : "Connect Outlook"}</SubmitButton></form>
      {c?.status === "active" && <form action={checkConnection}><SubmitButton className="secondary" pendingLabel="Checking…">Check connection</SubmitButton></form>}
      {attempt.data && <form action={cancel}><SubmitButton className="secondary" pendingLabel="Cancelling…">Cancel authorization</SubmitButton></form>}</div>
      {c?.status === "active" && <form className="settings-block" action={disconnect}><h2>Disconnect mailbox</h2><p className="muted">Disconnect blocks future sends. Already-dispatching emails may still complete.</p><SubmitButton className="danger" pendingLabel="Disconnecting…">Disconnect Outlook</SubmitButton></form>}
      {!!cleanup.count && <aside className="warning"><strong>Provider cleanup needed</strong><p>{cleanup.count} old or cancelled provider account(s) need cleanup. Local sending through these accounts is already blocked.</p><form action={retryCleanup}><SubmitButton className="secondary" pendingLabel="Cleaning up…">Retry provider cleanup</SubmitButton></form></aside>}
      <p className="field-help">Reconnecting keeps campaign history. If your account changes, create a new preview for any unsent messages; existing campaigns never switch senders automatically.</p>
    </>}
  </section></div></>;
}
