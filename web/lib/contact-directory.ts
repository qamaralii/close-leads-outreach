import type { SupabaseClient } from "@supabase/supabase-js";

export const contactSortColumns = { name: "name_sort", company: "company_sort", created: "close_created_at" } as const;
export type ContactSort = keyof typeof contactSortColumns;
export type SortDirection = "asc" | "desc";

export function contactDirectoryQuery(db: SupabaseClient, params: URLSearchParams) {
  const raw = Number(params.get("page") ?? 0);
  const page = Number.isSafeInteger(raw) && raw >= 0 ? Math.min(raw, 10000) : 0;
  const sort = params.get("sort") ?? "name";
  const column = Object.prototype.hasOwnProperty.call(contactSortColumns, sort)
    ? contactSortColumns[sort as ContactSort] : contactSortColumns.name;
  const ascending = params.get("direction") !== "desc";
  const search = (params.get("q") ?? "").trim().slice(0, 100).replace(/[%_,().\\]/g, " ");
  let query = db.from("close_contact_directory").select("close_lead_id,company_name,contact_name,email,close_created_at", { count: "exact" });
  if (search) query = query.or(`company_name.ilike.%${search}%,contact_name.ilike.%${search}%,email.ilike.%${search}%`);
  return query.order(column, { ascending, nullsFirst: false }).order("close_lead_id", { ascending: true }).range(page * 25, page * 25 + 24);
}
