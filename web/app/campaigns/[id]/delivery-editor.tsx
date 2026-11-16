"use client";
import { useId, useState } from "react";
import { useRouter } from "next/navigation";
import { editDelivery } from "../../actions";
import { DeliveryFields } from "../../delivery-fields";
import { useReview } from "./message";
import { effectiveDelivery, formatFileSize, type Attachment, type Delivery } from "@/lib/delivery";

export function RecipientDelivery({ campaignId, revision, recipientId, to, defaults, ccOverride, attachmentOverride, files, draft }: { campaignId: string; revision: number; recipientId: string; to: string; defaults: Delivery; ccOverride: string[] | null; attachmentOverride: string[] | null; files: Attachment[]; draft: boolean }) {
  const router = useRouter(), { editing, setEditing } = useReview();
  const fieldId = useId();
  const editorId = `delivery:${recipientId}`, open = editing === editorId;
  const [cc, setCc] = useState(ccOverride), [ids, setIds] = useState(attachmentOverride);
  const [pending, setPending] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(""), [editId, setEditId] = useState("");
  const actual = effectiveDelivery(defaults, ccOverride, attachmentOverride, to);
  function changed() { setEditId(crypto.randomUUID()); }
  async function save() {
    setPending(true); setError("");
    try {
      const result = await editDelivery({ campaignId, revision, recipientId, editId, cc, attachmentIds: ids });
      if (result.error) setError(result.error); else { setEditing(null); router.refresh(); }
    } catch { setError("Response lost. Retry unchanged settings or refresh to check the saved version."); }
    finally { setPending(false); }
  }
  return <div className="recipient-delivery">
    {open ? <div><h3>Delivery settings for this recipient</h3><label htmlFor={`${fieldId}-cc`}>CC source</label><select id={`${fieldId}-cc`} disabled={pending || busy} value={cc === null ? "default" : "custom"} onChange={e => { setCc(e.target.value === "default" ? null : [...actual.cc]); changed(); }}><option value="default">Use campaign defaults</option><option value="custom">Customize CC</option></select>
      <label htmlFor={`${fieldId}-attachments`}>Attachment source</label><select id={`${fieldId}-attachments`} disabled={pending || busy} value={ids === null ? "default" : "custom"} onChange={e => { setIds(e.target.value === "default" ? null : [...actual.attachmentIds]); changed(); }}><option value="default">Use campaign defaults</option><option value="custom">Customize attachments</option></select>
      <DeliveryFields value={{ cc: cc ?? defaults.cc, attachmentIds: ids ?? defaults.attachmentIds }} onChange={value => { if (JSON.stringify(value.cc) !== JSON.stringify(cc ?? defaults.cc)) setCc(value.cc); if (JSON.stringify(value.attachmentIds) !== JSON.stringify(ids ?? defaults.attachmentIds)) setIds(value.attachmentIds); changed(); }} disabled={pending} onBusy={setBusy} />
      <p className="field-help">Editing a list creates an override. An empty customized list sends without CC or attachments. Select “Use campaign defaults” to reset either setting independently.</p>
      <div className="row"><button disabled={pending || busy || !editId} onClick={save}>{pending ? "Saving…" : "Save delivery settings"}</button><button className="secondary" disabled={pending || busy} onClick={() => setEditing(null)}>Cancel</button></div></div> : <>
      <div className="delivery-summary"><div><span className="card-label">CC · {ccOverride === null ? "Campaign defaults" : "Customized"}</span><p>{actual.cc.join(", ") || "None"}</p></div><div><span className="card-label">Attachments · {attachmentOverride === null ? "Campaign defaults" : "Customized"}</span>{actual.attachmentIds.length ? <ul>{actual.attachmentIds.map(id => { const file = files.find(f => f.id === id); return <li key={id}><a href={`/api/attachments/${id}`} target="_blank" rel="noreferrer">{file?.name || "Unavailable attachment"}</a>{file && <small> · {formatFileSize(file.size_bytes)}</small>}</li>; })}</ul> : <p>None</p>}</div></div>
      {draft && <button className="quiet" disabled={!!editing} onClick={() => { setCc(ccOverride); setIds(attachmentOverride); setError(""); changed(); setEditing(editorId); }}>Edit CC & attachments</button>}
    </>}{error && <p className="error" role="alert">{error}</p>}
  </div>;
}
