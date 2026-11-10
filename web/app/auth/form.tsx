"use client";
import { useActionState } from "react";
import { login, password, reset } from "./actions";
export function AuthForm({ mode }: { mode: "login" | "password" | "reset" }) {
  const [message, action, pending] = useActionState(mode === "login" ? login : mode === "password" ? password : reset, "");
  return <form action={action}>
    {mode !== "password" && <label>Email<input name="email" type="email" autoComplete="email" required /></label>}
    {mode !== "reset" && <label>Password<input name="password" type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={mode === "password" ? 12 : undefined} required /></label>}
    {mode === "password" && <label>Confirm password<input name="confirm" type="password" autoComplete="new-password" required /></label>}
    <p role="status">{message}</p><button disabled={pending}>{pending ? "Please wait…" : mode === "login" ? "Sign in" : mode === "reset" ? "Send reset link" : "Set password"}</button>
  </form>;
}
