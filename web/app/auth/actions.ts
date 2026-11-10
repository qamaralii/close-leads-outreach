"use server";
import { redirect } from "next/navigation";
import { sessionClient } from "@/lib/supabase";
import { z } from "zod";

export async function login(_: string, form: FormData) {
  const credentials = z.object({ email: z.email(), password: z.string().min(1).max(200) }).safeParse(Object.fromEntries(form));
  if (!credentials.success) return "Enter your email and password.";
  const db = await sessionClient();
  const { error } = await db.auth.signInWithPassword(credentials.data);
  if (error) return "Sign-in failed. Check your credentials or request an invitation.";
  redirect("/");
}
export async function reset(_: string, form: FormData) {
  const email = z.email().safeParse(form.get("email"));
  if (!email.success) return "Enter a valid email address.";
  const db = await sessionClient();
  await db.auth.resetPasswordForEmail(email.data, { redirectTo: `${process.env.APP_URL}/auth/password` });
  return "If this account exists, a password-reset email has been sent.";
}
export async function password(_: string, form: FormData) {
  const value = z.string().min(12).max(128).safeParse(form.get("password"));
  if (!value.success) return "Use a password of 12–128 characters.";
  if (value.data !== form.get("confirm")) return "Passwords do not match.";
  const db = await sessionClient();
  const { data: { user } } = await db.auth.getUser();
  if (!user) return "Your link expired. Request a new invitation or reset email.";
  const { error } = await db.auth.updateUser({ password: value.data });
  if (error) return "Password update failed. Request a fresh link or use a stronger password.";
  redirect("/");
}
export async function logout() {
  const db = await sessionClient();
  await db.auth.signOut();
  redirect("/login");
}
