import { NextRequest, NextResponse } from "next/server";
import { cookies } from "next/headers";
import { canCompleteAuthorization, matchesOAuthState } from "@/lib/outlook-state";
import { requireMember } from "@/lib/auth";
import { adminClient } from "@/lib/supabase";
import { cleanup, hashState, manage, provider } from "@/lib/outlook";

export async function GET(request: NextRequest) {
  const { user } = await requireMember();
  const params = request.nextUrl.searchParams;
  const state = params.get("state") || "";
  const saved = (await cookies()).get("outlook_state")?.value || "";
  let notice = "callback_failed";
  // Require both the initiating browser cookie and owner-scoped database attempt.
  if (matchesOAuthState(state, saved)) {
    try {
      const { data: attempt, error } = await adminClient().from("outlook_auth_attempts").select("*").eq("user_id", user.id).eq("state_hash", hashState(state)).single();
      if (error || !attempt || !canCompleteAuthorization(attempt, params.get("status"), params.get("connected_account_id"))) throw new Error("Invalid attempt");
      const { data: identity } = await adminClient().from("outlook_identities").select("entity_id").eq("user_id", user.id).single();
      if (!identity) throw new Error("Missing identity");
      const mailbox = await provider().verify(attempt.account_id, identity.entity_id, attempt.auth_config_id);
      await manage(user.id, "complete", { attempt_id: attempt.id, state_hash: hashState(state), account_id: attempt.account_id, mailbox });
      await cleanup(user.id);
      notice = "connected";
    } catch { /* Only allowlisted messages, never OAuth payloads or credentials. */ }
  }
  const response = NextResponse.redirect(new URL(`/settings/outlook?notice=${notice}`, process.env.APP_URL!));
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("Referrer-Policy", "no-referrer");
  if (notice === "connected") response.cookies.set("outlook_state", "", { path: "/settings/outlook", maxAge: 0 });
  return response;
}
