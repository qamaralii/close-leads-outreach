"use server";
import { randomBytes } from "node:crypto";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { requireMember } from "@/lib/auth";
import { cleanup, hashState, manage, provider } from "@/lib/outlook";
import { adminClient } from "@/lib/supabase";

export async function connect() {
  const { user } = await requireMember();
  let destination: string;
  try {
    const config = process.env.COMPOSIO_OUTLOOK_AUTH_CONFIG_ID;
    if (!config || !process.env.APP_URL) throw new Error("Missing configuration");
    const state = randomBytes(32).toString("hex");
    const attempt = await manage(user.id, "begin", { state_hash: hashState(state), auth_config_id: config });
    const callback = new URL("/settings/outlook/callback", process.env.APP_URL);
    callback.searchParams.set("state", state);
    const link = await provider().link(attempt.entity_id, config, callback.toString());
    const bound = await manage(user.id, "bind", { attempt_id: attempt.id, state_hash: hashState(state), account_id: link.connected_account_id });
    if (!bound.bound) throw new Error("Superseded attempt");
    (await cookies()).set("outlook_state", state, { httpOnly: true, sameSite: "lax", secure: callback.protocol === "https:", path: "/settings/outlook", maxAge: 600 });
    destination = link.redirect_url;
  } catch { destination = "/settings/outlook?notice=start_failed"; }
  revalidatePath("/settings/outlook");
  redirect(destination);
}

export async function disconnect() {
  const { user } = await requireMember();
  try { await manage(user.id, "disconnect"); }
  catch { redirect("/settings/outlook?notice=save_failed"); }
  (await cookies()).set("outlook_state", "", { path: "/settings/outlook", maxAge: 0 });
  await cleanup(user.id);
  revalidatePath("/"); revalidatePath("/settings/outlook");
  redirect("/settings/outlook?notice=disconnected");
}

export async function cancel() {
  const { user } = await requireMember();
  try { await manage(user.id, "cancel"); }
  catch { redirect("/settings/outlook?notice=save_failed"); }
  (await cookies()).set("outlook_state", "", { path: "/settings/outlook", maxAge: 0 });
  await cleanup(user.id);
  redirect("/settings/outlook");
}

export async function retryCleanup() {
  const { user } = await requireMember();
  const ok = await cleanup(user.id);
  revalidatePath("/settings/outlook");
  redirect(`/settings/outlook?notice=${ok ? "cleanup_done" : "cleanup_failed"}`);
}

export async function checkConnection() {
  const { user } = await requireMember();
  const { data: c } = await adminClient().from("outlook_connections").select("*").eq("user_id", user.id).maybeSingle();
  let ok = false;
  if (c?.status === "active") {
    try { ok = await provider().verify(c.composio_connected_account_id, c.composio_entity_id) === c.mailbox_email.trim().toLowerCase(); }
    catch { /* Do not mark a working mapping inactive on a transient provider failure. */ }
  }
  redirect(`/settings/outlook?notice=${ok ? "verified" : "check_failed"}`);
}
