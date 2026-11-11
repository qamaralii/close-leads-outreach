"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { prepare } from "../actions";
import type { ContactSort, SortDirection } from "@/lib/contact-directory";
type Contact = { close_lead_id: string; contact_name: string | null; company_name: string | null; email: string | null; close_created_at: string | null };
export function Composer() {
  const router = useRouter();
  const [contacts, setContacts] = useState<Contact[]>([]), [selected, setSelected] = useState<string[]>([]);
  const [q, setQ] = useState(""), [page, setPage] = useState(0), [count, setCount] = useState(0);
  const [sort, setSort] = useState<ContactSort>("name"), [direction, setDirection] = useState<SortDirection>("asc");
  const [loading, setLoading] = useState(true);
  const [contactError, setContactError] = useState("");
  const [retry, setRetry] = useState(0);
  const [subject, setSubject] = useState("Hello {{company}}"), [body, setBody] = useState("Hi {{first_name}},\n\n");
  const [requestId, setRequestId] = useState(""), [error, setError] = useState(""), [pending, setPending] = useState(false);
  useEffect(() => { setRequestId(crypto.randomUUID()); }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setContacts([]); setContactError("");
    const timer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({ q, page: String(page), sort, direction });
        const response = await fetch(`/api/contacts?${params}`, { signal: controller.signal });
        if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw Error();
        const data = await response.json();
        if (!controller.signal.aborted) { setContacts(data.contacts); setCount(data.count); }
      } catch { if (!controller.signal.aborted) setContactError("Could not load contacts. Try again or sign in again."); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [q, page, sort, direction, retry]);
  function edited() { setRequestId(crypto.randomUUID()); }
  async function save() {
    setPending(true); setError("");
    try { const result = await prepare({ requestId, leadIds: selected, subject, body });
      if (result.campaignId) router.push(`/campaigns/${result.campaignId}`);
      else setError(result.error ?? "Preview failed.");
    } catch { setError("Response lost. Retry unchanged input to reuse the same request ID."); }
    finally { setPending(false); }
  }
  return <div className="composer-grid">
    <section className="contact-panel" aria-labelledby="contacts-heading">
      <div className="panel-heading"><div><h2 id="contacts-heading"><span className="step-number">1</span>Select contacts</h2><p>Choose who you’d like to reach.</p></div><span className="badge">Close directory</span></div>
      <div className="contact-tools">
        <label>Search contacts<input type="search" placeholder="Name, company, or email" value={q} onChange={e => { setQ(e.target.value); setPage(0); }} /></label>
        <label>Sort by<select value={sort} onChange={e => { const next = e.target.value as ContactSort; setSort(next); setDirection(next === "created" ? "desc" : "asc"); setPage(0); }}><option value="name">Name</option><option value="company">Company</option><option value="created">Created in Close</option></select></label>
        <label>Order<select value={direction} onChange={e => { setDirection(e.target.value as SortDirection); setPage(0); }}><option value="asc">{sort === "created" ? "Oldest first" : "A–Z"}</option><option value="desc">{sort === "created" ? "Newest first" : "Z–A"}</option></select></label>
      </div>
      <div className="selection-bar"><span aria-live="polite"><strong>{selected.length} selected</strong> / 1,000 maximum</span><button className="quiet" disabled={pending || !selected.length} onClick={() => { setSelected([]); edited(); }}>Clear selection</button></div>
      <p className="table-hint">Scroll the table to see more contacts and columns.</p>
      <div className="table-scroll contact-table" tabIndex={0} role="region" aria-label="Contact directory" aria-busy={loading}>
        {loading ? <div className="empty-state" role="status">Loading contacts…</div> : contactError ? <div className="empty-state"><p className="error" role="alert">{contactError}</p><button className="secondary" onClick={() => setRetry(retry + 1)}>Try again</button></div> : !contacts.length ? <div className="empty-state"><h2>{q ? "No matching contacts" : "No contacts to show"}</h2><p>{q ? "Try another name, company, or email." : "Imported Close contacts will appear here."}</p>{q && <button className="secondary" onClick={() => { setQ(""); setPage(0); }}>Clear search</button>}</div> :
        <table><thead><tr><th><span aria-label="Selection">✓</span></th><th>Contact</th><th>Company</th><th title="Close lead creation date">Created in Close (UTC)</th></tr></thead><tbody>{contacts.map(c => <tr className={selected.includes(c.close_lead_id) ? "selected" : undefined} key={c.close_lead_id}>
          <td><input aria-label={`Select ${c.contact_name || c.email || c.company_name || c.close_lead_id}`} type="checkbox" checked={selected.includes(c.close_lead_id)} disabled={pending || (!selected.includes(c.close_lead_id) && selected.length >= 1000)} onChange={e => { setSelected(e.target.checked ? [...selected, c.close_lead_id] : selected.filter(id => id !== c.close_lead_id)); edited(); }} /></td>
          <td><span className="contact-name">{c.contact_name || "Unnamed contact"}</span><span className="contact-email">{c.email || "No email address"}</span></td><td className="contact-company">{c.company_name || "—"}</td>
          <td className="contact-date">{c.close_created_at ? <time dateTime={c.close_created_at} title={new Date(c.close_created_at).toISOString()}>{new Date(c.close_created_at).toLocaleDateString("en-GB", { timeZone: "UTC", year: "numeric", month: "short", day: "numeric" })}</time> : "—"}</td>
        </tr>)}</tbody></table>}
      </div>
      <div className="pagination"><span>{loading ? "Updating contacts…" : contactError ? "Contacts unavailable" : `${count.toLocaleString()} contacts · Page ${page + 1} of ${Math.max(1, Math.ceil(count / 25))}`}</span><div className="pagination-actions"><button className="secondary" disabled={loading || page === 0} onClick={() => setPage(page - 1)}>Previous</button><button className="secondary" disabled={loading || !!contactError || (page + 1) * 25 >= count} onClick={() => setPage(page + 1)}>Next</button></div></div>
    </section>
    <section className="compose-panel" aria-labelledby="compose-heading">
      <div className="panel-heading"><div><h2 id="compose-heading"><span className="step-number">2</span>Compose</h2><p>Make every message feel personal.</p></div><span className="badge">Plain text</span></div>
      <label>Subject<input maxLength={998} disabled={pending} value={subject} onChange={e => { setSubject(e.target.value); edited(); }} /></label>
      <label>Message<textarea aria-describedby="placeholder-help" maxLength={100000} disabled={pending} value={body} onChange={e => { setBody(e.target.value); edited(); }} /></label>
      <p className="field-help" id="placeholder-help">Personalize with <code>{"{{first_name}}"}</code> and <code>{"{{company}}"}</code>. You’ll review the filled-in messages next.</p>
      <div className="compose-footer"><p className="muted">{selected.length ? `${selected.length} contact${selected.length === 1 ? "" : "s"} selected for preview` : "Select contacts to create your preview."}</p>
        {error && <p className="error" role="alert">{error}</p>}<button disabled={pending || !selected.length || !requestId || !subject.trim() || !body.trim()} onClick={save}>{pending ? "Saving preview…" : "Create saved preview →"}</button><small>This saves a draft. Sending requires your confirmation.</small>
      </div>
    </section>
  </div>;
}
