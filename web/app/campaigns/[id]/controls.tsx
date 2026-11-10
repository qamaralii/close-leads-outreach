"use client";
import { useActionState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { confirm } from "../../actions";
export function Controls({ id, draft }: { id: string; draft: boolean }) {
  const router = useRouter();
  const [error, action, pending] = useActionState(confirm, "");
  useEffect(() => { if (!draft) { const timer = setInterval(() => router.refresh(), 10000); return () => clearInterval(timer); } }, [draft, router]);
  if (!draft) return <p>Results refresh every 10 seconds. Microsoft acceptance is shown as submitted; it is not a delivery receipt.</p>;
  return <form action={action}><input type="hidden" name="campaignId" value={id} /><label><input type="checkbox" name="reviewed" value="yes" required /> I reviewed the saved recipients, sender, and messages and confirm sending.</label><p className="error">{error}</p><button disabled={pending}>{pending ? "Submitting…" : "Confirm & send"}</button></form>;
}
