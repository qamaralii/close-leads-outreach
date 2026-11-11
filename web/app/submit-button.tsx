"use client";

import { useFormStatus } from "react-dom";

export function SubmitButton({ children, pendingLabel, className }: { children: React.ReactNode; pendingLabel: string; className?: string }) {
  const { pending } = useFormStatus();
  return <button className={className} disabled={pending} aria-disabled={pending}>{pending ? pendingLabel : children}</button>;
}
