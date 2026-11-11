import { NextRequest } from "next/server";
import { requireMember } from "@/lib/auth";
import { contactDirectoryQuery } from "@/lib/contact-directory";
export async function GET(request: NextRequest) {
  const { db } = await requireMember();
  const { data, count, error } = await contactDirectoryQuery(db, request.nextUrl.searchParams);
  if (error) return Response.json({ error: "Contact lookup failed." }, { status: 500 });
  return Response.json({ contacts: data, count }, { headers: { "Cache-Control": "private, no-store" } });
}
