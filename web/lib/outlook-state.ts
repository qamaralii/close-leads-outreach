import { timingSafeEqual } from "node:crypto";

export function matchesOAuthState(state: string, cookie: string) {
  return /^[a-f0-9]{64}$/.test(state) && /^[a-f0-9]{64}$/.test(cookie)
    && timingSafeEqual(Buffer.from(state), Buffer.from(cookie));
}

export function canCompleteAuthorization(attempt: { status: string; expires_at: string; account_id: string | null }, status: string | null, accountId: string | null, now = Date.now()) {
  return attempt.status === "pending" && Date.parse(attempt.expires_at) > now && status === "success"
    && !!attempt.account_id && attempt.account_id === accountId;
}
