import { NextRequest } from "next/server";
import { requireMember } from "@/lib/auth";
export async function GET(request: NextRequest) {
  const { db } = await requireMember();
  const raw = Number(request.nextUrl.searchParams.get("page") ?? 0);
  const page = Number.isSafeInteger(raw) && raw >= 0 ? Math.min(raw, 10000) : 0;
  const search = (request.nextUrl.searchParams.get("q") ?? "").trim().slice(0, 100).replace(/[%_,().\\]/g, " ");
  let query = db.from("close_leads").select("close_lead_id,company_name,contact_name,email", { count: "exact" });
  if (search) query = query.or(`company_name.ilike.%${search}%,contact_name.ilike.%${search}%,email.ilike.%${search}%`);
  const { data, count, error } = await query.order("close_lead_id").range(page * 25, page * 25 + 24);
  if (error) return Response.json({ error: "Contact lookup failed." }, { status: 500 });
  return Response.json({ contacts: data, count }, { headers: { "Cache-Control": "private, no-store" } });
}
