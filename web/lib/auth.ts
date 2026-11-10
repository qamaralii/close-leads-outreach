import "server-only";
import { redirect } from "next/navigation";
import { sessionClient } from "./supabase";

export async function requireMember() {
  const db = await sessionClient();
  const { data: { user }, error } = await db.auth.getUser();
  if (error || !user) redirect("/login");
  const { data: member } = await db.from("outreach_members").select("is_active").eq("user_id", user.id).single();
  if (!member?.is_active) redirect("/access");
  return { db, user };
}
