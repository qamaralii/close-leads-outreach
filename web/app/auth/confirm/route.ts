import { sessionClient } from "@/lib/supabase";
import { NextRequest, NextResponse } from "next/server";
export async function GET(request: NextRequest) {
  const token_hash = request.nextUrl.searchParams.get("token_hash");
  const type = request.nextUrl.searchParams.get("type");
  const origin = process.env.APP_URL!;
  if (token_hash && (type === "invite" || type === "recovery")) {
    const db = await sessionClient();
    const { error } = await db.auth.verifyOtp({ token_hash, type });
    if (!error) return NextResponse.redirect(new URL("/auth/password", origin));
  }
  return NextResponse.redirect(new URL("/auth/invalid", origin));
}
