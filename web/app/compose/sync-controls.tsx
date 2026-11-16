"use client";
import { useEffect, useRef, useState } from "react";
import { syncLabel, syncRunning, type SyncJob } from "@/lib/lead-sync";

export function SyncControls({ onCompleted }: { onCompleted: () => void }) {
  const [job, setJob] = useState<SyncJob | null>(null), [lastSuccess, setLastSuccess] = useState<SyncJob | null>(null);
  const [pending, setPending] = useState(false), [error, setError] = useState(""), [loaded, setLoaded] = useState(false);
  const completion = useRef<string | null | undefined>(undefined);
  const request = useRef<{ id: string; retryId: string | null } | null>(null);
  const mounted = useRef(true);
  const statusSequence = useRef(0);
  async function status() {
    const sequence = ++statusSequence.current;
    const response = await fetch("/api/lead-sync", { cache: "no-store" });
    if (!response.ok) throw Error("Sync status unavailable. Try again shortly.");
    const data = await response.json();
    if (!mounted.current || sequence !== statusSequence.current) return;
    setJob(data.job); setLastSuccess(data.lastSuccess); setLoaded(true);
    const marker = data.lastSuccess ? `${data.lastSuccess.id}:${data.lastSuccess.finished_at}` : null;
    if (completion.current !== undefined && marker !== null && marker !== completion.current) onCompleted();
    completion.current = marker;
  }
  useEffect(() => {
    mounted.current = true;
    let stopped = false, active = false;
    const update = async () => {
      if (active || stopped) return;
      active = true;
      try { await status(); if (!stopped) setError(""); }
      catch { if (!stopped) setError("Sync status unavailable. Check your connection or try again shortly."); }
      finally { active = false; }
    };
    void update(); const timer = setInterval(() => { void update(); }, 5000);
    return () => { stopped = true; mounted.current = false; clearInterval(timer); };
  }, [onCompleted]);
  async function start(retryId: string | null = null) {
    setPending(true); setError("");
    if (!request.current || request.current.retryId !== retryId) request.current = { id: crypto.randomUUID(), retryId };
    try {
      const response = await fetch("/api/lead-sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ requestId: request.current.id, retryJobId: retryId }) });
      const data = await response.json();
      if (!response.ok) throw Error(data.error || "Could not start sync.");
      // Block further starts while waiting for the first durable status read.
      setJob(current => ({ ...(current ?? {}), id: data.jobId, status: "queued" } as SyncJob));
      await status(); request.current = null;
    } catch (e) { if (mounted.current) setError(e instanceof Error ? e.message : "Response lost. Check status before retrying."); }
    finally { if (mounted.current) setPending(false); }
  }
  const running = syncRunning(job);
  return <div className="lead-sync-controls">
    <div className="lead-sync-actions"><button type="button" className="secondary" disabled={pending || running || !loaded} onClick={() => { void start(); }}>{pending ? "Starting sync…" : running ? "Sync in progress…" : "Sync from Close"}</button>{job?.status === "failed" && <button type="button" className="quiet" disabled={pending} onClick={() => { void start(job.id); }}>Retry failed sync</button>}</div>
    <p className="field-help">{lastSuccess?.finished_at ? <>Last successful sync: <time dateTime={lastSuccess.finished_at}>{new Date(lastSuccess.finished_at).toLocaleString()}</time></> : "Sync imports new leads and refreshes existing primary contacts."}</p>
    {job && <div className={`sync-progress${job.status === "failed" ? " sync-failed" : ""}`} role="status"><strong>{syncLabel(job)}</strong>{job.status === "importing" && job.total_rows && <progress max={job.total_rows} value={job.rows_processed} aria-label="Lead import progress" />}{job.error && <small>{job.error}</small>}{job.status === "failed" && job.rows_processed > 0 && <small>{job.rows_processed.toLocaleString()} leads were refreshed before the failure. Retry resumes this export; Sync from Close starts a fresh one.</small>}{running && <small>This is shared across the workspace. You can leave this page while it runs.</small>}</div>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>;
}
