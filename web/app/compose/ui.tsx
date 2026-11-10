"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { prepare } from "../actions";
type Contact = { close_lead_id: string; contact_name: string | null; company_name: string | null; email: string | null };
export function Composer() {
  const router = useRouter();
  const [contacts, setContacts] = useState<Contact[]>([]), [selected, setSelected] = useState<string[]>([]);
  const [q, setQ] = useState(""), [page, setPage] = useState(0), [count, setCount] = useState(0);
  const [subject, setSubject] = useState("Hello {{company}}"), [body, setBody] = useState("Hi {{first_name}},\n\n");
  const [requestId, setRequestId] = useState(""), [error, setError] = useState(""), [pending, setPending] = useState(false);
  useEffect(() => { setRequestId(crypto.randomUUID()); }, []);
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const response = await fetch(`/api/contacts?q=${encodeURIComponent(q)}&page=${page}`, { signal: controller.signal });
        if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw Error();
        const data = await response.json(); setContacts(data.contacts); setCount(data.count); setError("");
      } catch { if (!controller.signal.aborted) setError("Could not load contacts. Refresh or sign in again."); }
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [q, page]);
  function edited() { setRequestId(crypto.randomUUID()); }
  async function save() {
    setPending(true); setError("");
    try { const result = await prepare({ requestId, leadIds: selected, subject, body });
      if (result.campaignId) router.push(`/campaigns/${result.campaignId}`);
      else setError(result.error ?? "Preview failed.");
    } catch { setError("Response lost. Retry unchanged input to reuse the same request ID."); }
    finally { setPending(false); }
  }
  return <><section><h2>Select contacts</h2><label>Search<input value={q} onChange={e => { setQ(e.target.value); setPage(0); }} /></label>
    <p>{selected.length} selected (maximum 1,000)</p><button disabled={pending} onClick={() => { setSelected([]); edited(); }}>Clear selection</button>
    <table><thead><tr><th>Select</th><th>Name</th><th>Company</th><th>Email</th></tr></thead><tbody>{contacts.map(c => <tr key={c.close_lead_id}><td><input aria-label={`Select ${c.contact_name ?? c.company_name}`} type="checkbox" checked={selected.includes(c.close_lead_id)} disabled={pending || (!selected.includes(c.close_lead_id) && selected.length >= 1000)} onChange={e => { setSelected(e.target.checked ? [...selected, c.close_lead_id] : selected.filter(id => id !== c.close_lead_id)); edited(); }} /></td><td>{c.contact_name}</td><td>{c.company_name}</td><td>{c.email}</td></tr>)}</tbody></table>
    <div className="row"><button disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button><p>Page {page + 1} · {count} contacts</p><button disabled={(page + 1) * 25 >= count} onClick={() => setPage(page + 1)}>Next</button></div></section>
    <section><h2>Compose</h2><p>Plain text. Supported placeholders: {"{{first_name}}"} and {"{{company}}"}.</p>
    <label>Subject<input maxLength={998} disabled={pending} value={subject} onChange={e => { setSubject(e.target.value); edited(); }} /></label>
    <label>Body<textarea maxLength={100000} disabled={pending} value={body} onChange={e => { setBody(e.target.value); edited(); }} /></label>
    <p className="error" role="alert">{error}</p><button disabled={pending || !selected.length || !requestId} onClick={save}>{pending ? "Saving preview…" : "Create saved preview"}</button><p><small>This saves a draft. Sending requires confirmation on the next screen.</small></p></section></>;
}
