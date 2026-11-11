"use client";
import { createContext, useContext, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { editMessage } from "../../actions";

const ReviewContext = createContext<{ editing: string | null; setEditing: (id: string | null) => void }>({ editing: null, setEditing: () => {} });
export function ReviewProvider({ children }: { children: ReactNode }) {
  const [editing, setEditing] = useState<string | null>(null);
  return <ReviewContext.Provider value={{ editing, setEditing }}>{children}</ReviewContext.Provider>;
}
export function useReview() { return useContext(ReviewContext); }
type Recipient = { id: string; recipient_email: string; rendered_subject: string; rendered_body: string; status: string; error: string | null; customized: boolean };
export function MessageCard({ recipient: r, campaignId, revision, draft }: { recipient: Recipient; campaignId: string; revision: number; draft: boolean }) {
  const router = useRouter(), { editing, setEditing } = useReview();
  const [subject, setSubject] = useState(r.rendered_subject), [body, setBody] = useState(r.rendered_body);
  const [error, setError] = useState(""), [pending, setPending] = useState(false), [editId, setEditId] = useState("");
  const [resetId, setResetId] = useState(""), [resetting, setResetting] = useState(false);
  const open = editing === r.id && !resetting;
  async function save(reset: boolean) {
    const token = (reset ? resetId : editId) || crypto.randomUUID();
    if (reset) { setResetId(token); setResetting(true); setEditing(r.id); } else setEditId(token);
    setPending(true); setError("");
    try {
      const result = await editMessage({ campaignId, revision, recipientId: r.id, editId: token, reset, subject, body });
      if (result.error) setError(result.error);
      else { setEditing(null); router.refresh(); }
    } catch { setError("Response lost. Retry unchanged input or refresh to check the saved message."); }
    finally { setPending(false); if (reset) { setResetting(false); setEditing(null); } }
  }
  return <article className="message-card"><div className="panel-heading"><div><p>To</p><h2>{r.recipient_email}</h2></div><div className="row">{r.customized && <span className="badge badge-draft">Customized</span>}<span className={`badge badge-${r.status}`}>{r.status}</span></div></div>
    {open ? <form onSubmit={e => { e.preventDefault(); void save(false); }}><label>Subject<input autoFocus required maxLength={998} value={subject} disabled={pending} onChange={e => { setSubject(e.target.value); setEditId(crypto.randomUUID()); }} /></label><label>Message<textarea required maxLength={100000} value={body} disabled={pending} onChange={e => { setBody(e.target.value); setEditId(crypto.randomUUID()); }} /></label><p className="field-help">Edit the filled-in text for this recipient only.</p><div className="row"><button disabled={pending || !subject.trim() || !body.trim()}>{pending ? "Saving…" : "Save message"}</button><button type="button" className="secondary" disabled={pending} onClick={() => { setEditing(null); setError(""); }}>Cancel</button></div></form> : <><h3>{r.rendered_subject}</h3><pre>{r.rendered_body}</pre>{draft && <div className="row"><button className="secondary" disabled={pending || !!editing} onClick={() => { setSubject(r.rendered_subject); setBody(r.rendered_body); setEditId(crypto.randomUUID()); setError(""); setEditing(r.id); }}>Edit message</button>{r.customized && <button className="quiet" disabled={pending || !!editing} onClick={() => { void save(true); }}>Reset to campaign template</button>}</div>}</>}
    {error && <p className="error" role="alert">{error}</p>}{r.error && <p className="error">{r.error}</p>}
  </article>;
}
