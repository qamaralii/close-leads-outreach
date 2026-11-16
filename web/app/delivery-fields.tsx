"use client";

import { useEffect, useId, useRef, useState } from "react";
import { ccSchema, MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS, formatFileSize, type Attachment, type Delivery } from "@/lib/delivery";

type Upload = { id: string; file: File; progress: number; error: string; uploading: boolean };
export function DeliveryFields({ value, onChange, disabled, onBusy }: { value: Delivery; onChange: (value: Delivery) => void; disabled: boolean; onBusy: (busy: boolean) => void }) {
  const id = useId(), [ccInput, setCcInput] = useState(""), [error, setError] = useState("");
  const [files, setFiles] = useState<Record<string, Attachment>>({}), [uploads, setUploads] = useState<Upload[]>([]);
  const transfers = useRef(new Set<XMLHttpRequest>());
  const metadataMissing = value.attachmentIds.some(fileId => !files[fileId]);
  useEffect(() => { onBusy(!!ccInput.trim() || metadataMissing || uploads.length > 0); }, [ccInput, metadataMissing, uploads.length, onBusy]);
  useEffect(() => () => { transfers.current.forEach(xhr => xhr.abort()); }, []);
  useEffect(() => {
    const missing = value.attachmentIds.filter(fileId => !files[fileId]);
    if (!missing.length) return;
    const controller = new AbortController();
    fetch(`/api/attachments?ids=${missing.join(",")}`, { signal: controller.signal }).then(async response => {
      if (!response.ok) throw Error("Could not load attachment details. Remove unavailable files or reopen this editor.");
      const data = await response.json();
      setFiles(current => ({ ...current, ...Object.fromEntries(data.files.map((file: Attachment) => [file.id, file])) }));
    }).catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [value.attachmentIds, files]);

  function addCc() {
    if (!ccInput.trim()) return;
    const parsed = ccSchema.safeParse([...value.cc, ...ccInput.split(/[,;\n]+/).map(v => v.trim()).filter(Boolean)]);
    if (!parsed.success) { setError("Enter valid email addresses, separated by commas (up to 50)."); return; }
    setError(""); setCcInput(""); onChange({ ...value, cc: parsed.data });
  }
  async function upload(item: Upload) {
    setUploads(current => current.map(u => u.id === item.id ? { ...u, uploading: true, error: "" } : u));
    try {
      const start = await fetch("/api/attachments", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: item.id, name: item.file.name, size: item.file.size, contentType: item.file.type || "application/octet-stream" }) });
      const data = await start.json();
      if (!start.ok) throw Error(data.error || "Upload unavailable.");
      if (!data.ready) await new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest(); transfers.current.add(xhr);
        xhr.open("PUT", data.uploadUrl); xhr.setRequestHeader("Content-Type", item.file.type || "application/octet-stream");
        for (const [name, value] of Object.entries(data.uploadHeaders ?? {})) xhr.setRequestHeader(name, String(value));
        xhr.upload.onprogress = e => { if (e.lengthComputable) setUploads(current => current.map(u => u.id === item.id ? { ...u, progress: Math.round(e.loaded / e.total * 100) } : u)); };
        xhr.onload = () => { transfers.current.delete(xhr); if (xhr.status >= 200 && xhr.status < 300 || xhr.status === 409 || xhr.status === 400 && xhr.responseText.includes("Duplicate")) resolve(); else reject(Error("File transfer failed. Retry this upload.")); };
        xhr.onerror = xhr.onabort = () => { transfers.current.delete(xhr); reject(Error("Upload interrupted. Retry this file.")); };
        xhr.send(item.file);
      });
      const finish = await fetch(`/api/attachments/${item.id}`, { method: "POST" }), result = await finish.json();
      if (!finish.ok) throw Error(result.error || "Could not verify upload.");
      setFiles(current => ({ ...current, [item.id]: result.file }));
      setUploads(current => current.filter(u => u.id !== item.id));
      // Process one file at a time; keep the latest controlled selection below.
      return result.file as Attachment;
    } catch (e) { setUploads(current => current.map(u => u.id === item.id ? { ...u, uploading: false, error: e instanceof Error ? e.message : "Upload failed." } : u)); return null; }
  }
  const latest = useRef(value); latest.current = value;
  async function choose(selected: File[]) {
    const total = value.attachmentIds.reduce((sum, fileId) => sum + (files[fileId]?.size_bytes ?? 0), 0) + selected.reduce((sum, f) => sum + f.size, 0);
    if (total > MAX_ATTACHMENT_BYTES || value.attachmentIds.length + selected.length > MAX_ATTACHMENTS || selected.some(f => !f.size)) { setError("Choose up to 20 nonempty files, totaling no more than 25 MB per email."); return; }
    setError("");
    const items = selected.map(file => ({ id: crypto.randomUUID(), file, progress: 0, error: "", uploading: true }));
    setUploads(current => [...current, ...items]);
    for (const item of items) {
      const file = await upload(item);
      if (file) { const next = { ...latest.current, attachmentIds: [...latest.current.attachmentIds, file.id] }; latest.current = next; onChange(next); }
    }
  }
  return <div className="delivery-fields">
    <label htmlFor={`${id}-cc`}>CC addresses <span className="field-help">Optional</span></label>
    <div className="cc-chips">{value.cc.map(address => <span className="cc-chip" key={address}>{address}<button type="button" className="quiet" aria-label={`Remove CC ${address}`} disabled={disabled} onClick={() => onChange({ ...value, cc: value.cc.filter(v => v !== address) })}>×</button></span>)}</div>
    <div className="cc-entry"><input id={`${id}-cc`} type="text" inputMode="email" placeholder="name@company.com" value={ccInput} disabled={disabled} onChange={e => setCcInput(e.target.value)} onKeyDown={e => { if (e.key === "Enter" || e.key === "," || e.key === ";") { e.preventDefault(); addCc(); } }} /><button type="button" className="secondary" disabled={disabled || !ccInput.trim()} onClick={addCc}>Add CC</button></div>
    <p className="field-help">Add addresses with Enter or Add CC. Copied recipients are visible to everyone on that email.</p>
    <label htmlFor={`${id}-files`}>Attachments <span className="field-help">25 MB combined per email</span></label>
    <div className="file-drop" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); if (!disabled && !uploads.length && !metadataMissing) void choose([...e.dataTransfer.files]); }}>
      <input id={`${id}-files`} type="file" multiple disabled={disabled || !!uploads.length || metadataMissing} onChange={e => { if (e.target.files) void choose([...e.target.files]); e.target.value = ""; }} />
      <span className="field-help">Choose files or drop them here. Files are stored privately for scheduled sending.</span>
    </div>
    <ul className="attachment-list">{value.attachmentIds.map(fileId => <li key={fileId}><span><a href={`/api/attachments/${fileId}`} target="_blank" rel="noreferrer">{files[fileId]?.name || "Attachment details unavailable"}</a><small>{files[fileId] && formatFileSize(files[fileId].size_bytes)}</small></span><button type="button" className="quiet" disabled={disabled} onClick={() => { setError(""); onChange({ ...value, attachmentIds: value.attachmentIds.filter(v => v !== fileId) }); }}>Remove</button></li>)}
      {uploads.map(item => <li key={item.id}><span>{item.file.name}<small role="status">{item.error || `Uploading and verifying… ${item.progress}%`}</small>{item.uploading && <progress max={100} value={item.progress} aria-label={`Uploading ${item.file.name}`} />}</span>{!item.uploading && <div className="row"><button type="button" className="secondary" onClick={async () => { const file = await upload(item); if (file) onChange({ ...latest.current, attachmentIds: [...latest.current.attachmentIds, file.id] }); }}>Retry</button><button type="button" className="quiet" onClick={() => setUploads(current => current.filter(u => u.id !== item.id))}>Remove</button></div>}</li>)}
    </ul>
    {error && <p className="error" role="alert">{error}</p>}
  </div>;
}
