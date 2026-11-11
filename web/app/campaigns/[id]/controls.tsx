"use client";
import { useActionState, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { confirm } from "../../actions";
import { useReview } from "./message";
export function Controls({ id, revision, draft, blocked = false }: { id: string; revision: number; draft: boolean; blocked?: boolean }) {
  const router = useRouter();
  const [error, action, pending] = useActionState(confirm, "");
  const { editing } = useReview();
  const [reviewed, setReviewed] = useState(false);
  useEffect(() => { setReviewed(false); }, [revision, editing]);
  useEffect(() => { if (!draft) { const timer = setInterval(() => router.refresh(), 10000); return () => clearInterval(timer); } }, [draft, router]);
  if (!draft) return <p>Results refresh every 10 seconds. Microsoft acceptance is shown as submitted; it is not a delivery receipt.</p>;
  if (blocked) return <p>Confirmation is unavailable because the reviewed sender is no longer active.</p>;
  return <form className="review-confirmation" action={action}><input type="hidden" name="campaignId" value={id} /><input type="hidden" name="revision" value={revision} /><label><input type="checkbox" name="reviewed" value="yes" checked={reviewed} onChange={e => setReviewed(e.target.checked)} disabled={pending || !!editing} required /> I reviewed the saved recipients, sender, and messages and confirm sending.</label>{editing && <p className="field-help">Save or cancel your message edit before confirming.</p>}<p className="error" role="alert">{error}</p><button disabled={pending || !!editing || !reviewed}>{pending ? "Submitting…" : "Confirm & send"}</button></form>;
}
