import { z } from "zod";
export const requestSchema = z.object({ requestId: z.uuid(), leadIds: z.array(z.string().regex(/^lead_[A-Za-z0-9]+$/)).min(1).max(1000), subject: z.string().min(1).max(998), body: z.string().min(1).max(100000) }).strict();
export type Lead = { close_lead_id: string; company_name: string | null; contact_name: string | null; first_name: string | null; email: string | null };
export function validEmail(value: string | null): value is string {
  if (!value) return false;
  const email = value.trim();
  if (email.length > 254 || !/^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+$/.test(email)) return false;
  const [local, domain] = email.split("@");
  return local.length <= 64 && !local.startsWith(".") && !local.endsWith(".") && !local.includes("..") && domain.includes(".") && domain.split(".").every(p => p.length > 0 && p.length <= 63 && !p.startsWith("-") && !p.endsWith("-"));
}
function checkText(text: string, subject: boolean) {
  if (!text.trim() || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text) || (subject && /[\r\n\t]/.test(text))) throw Error("Invalid message text or subject line breaks.");
}
export function resolvePreview(input: z.infer<typeof requestSchema>, leads: Lead[], suppressions: Set<string>) {
  checkText(input.subject, true); checkText(input.body, false);
  const pattern = /(?<!\{)\{\{\s*(first_name|company)\s*\}\}(?!\})/g;
  for (const text of [input.subject, input.body]) if (/\{\{|\}\}/.test(text.replace(pattern, ""))) throw Error("Supported placeholders: {{first_name}} and {{company}}.");
  if (new Set(input.leadIds).size !== input.leadIds.length) throw Error("Duplicate lead selection.");
  const seen = new Set<string>();
  const recipients = [];
  const excluded: { close_lead_id: string; reason: string }[] = [];
  for (const id of input.leadIds) {
    const lead = leads.find(row => row.close_lead_id === id);
    const email = lead?.email?.trim() ?? null;
    const reason = !lead ? "lead_not_found" : !validEmail(email) ? "missing_or_invalid_email" : suppressions.has(email.toLowerCase()) ? "suppressed" : seen.has(email.toLowerCase()) ? "duplicate_email" : null;
    if (reason) { excluded.push({ close_lead_id: id, reason }); continue; }
    seen.add(email!.toLowerCase());
    function render(text: string, subject: boolean) {
      const result = text.replace(pattern, (_, key) => {
        const value = key === "first_name" ? lead!.first_name : lead!.company_name;
        if (!value?.trim()) throw Error(`Missing ${key} for ${id}. Edit your selection or template.`);
        return value.trim();
      });
      checkText(result, subject);
      if (/\{\{|\}\}/.test(result) || result.length > (subject ? 998 : 100000)) throw Error(`Invalid personalized message for ${id}.`);
      return result;
    }
    recipients.push({ close_lead_id: id, recipient_email: email!, recipient_name: lead!.contact_name, company_name: lead!.company_name, rendered_subject: render(input.subject, true), rendered_body: render(input.body, false) });
  }
  if (!recipients.length) throw Error("No eligible recipients. Check email addresses, selection, and suppressions.");
  return { recipients, excluded };
}
