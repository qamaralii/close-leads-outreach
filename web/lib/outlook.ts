import "server-only";
import { createHash } from "node:crypto";
import { adminClient } from "./supabase";
import { OutlookProvider } from "./outlook-provider";

export const hashState = (state: string) => createHash("sha256").update(state).digest("hex");
export const provider = () => new OutlookProvider(process.env.COMPOSIO_API_KEY || "");

export async function manage(userId: string, operation: string, data: Record<string, unknown> = {}) {
  const result = await adminClient().rpc("manage_outlook_connection", { p_user_id: userId, p_operation: operation, p_data: data });
  if (result.error) throw new Error("Could not save the connection change. Refresh and retry; wait 30 seconds between connection attempts.");
  return result.data;
}

export async function cleanup(userId: string) {
  const { data, error } = await adminClient().from("outlook_account_cleanup").select("account_id").eq("user_id", userId).is("completed_at", null).order("created_at").limit(10);
  if (error) return false;
  let success = true;
  for (const row of data) {
    try { await provider().disable(row.account_id); await manage(userId, "cleanup_done", { account_id: row.account_id }); }
    catch { success = false; }
  }
  return success;
}
