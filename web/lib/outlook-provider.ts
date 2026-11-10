import { z } from "zod";

const accountId = z.string().regex(/^ca_[A-Za-z0-9_-]+$/);
const linkSchema = z.object({ connected_account_id: accountId, redirect_url: z.url(), expires_at: z.iso.datetime({ offset: true }) });
const accountSchema = z.object({ id: accountId, user_id: z.string(), status: z.literal("ACTIVE"), is_disabled: z.literal(false),
  toolkit: z.object({ slug: z.literal("outlook") }), auth_config: z.object({ id: z.string() }) });

export class OutlookProvider {
  constructor(private key: string, private transport: typeof fetch = fetch) {
    if (!key) throw new Error("Outlook connection setup is not configured.");
  }
  private async request(path: string, method = "GET", body?: unknown) {
    let response: Response;
    try {
      response = await this.transport(`https://backend.composio.dev/api/v3.1/${path}`, {
        method, headers: { "x-api-key": this.key, "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", redirect: "error", signal: AbortSignal.timeout(20000),
      });
    } catch { throw new Error("Could not reach Outlook authorization. Please retry."); }
    if (!response.ok) throw new Error("Outlook authorization request failed. Please retry or contact your administrator.");
    // Never expose provider response bodies: they can contain credentials.
    try { return await response.json(); } catch { throw new Error("Invalid authorization response."); }
  }
  async link(entity: string, config: string, callback: string) {
    const configData = await this.request(`auth_configs/${encodeURIComponent(config)}`);
    if (configData?.toolkit?.slug !== "outlook" || configData?.auth_scheme !== "OAUTH2") throw new Error("The configured authorization must use Outlook OAuth2.");
    const raw = await this.request("connected_accounts/link", "POST", { user_id: entity, auth_config_id: config, callback_url: callback });
    const parsed = linkSchema.safeParse(raw);
    if (!parsed.success || Date.parse(parsed.data.expires_at) <= Date.now()) throw new Error("Invalid authorization link response.");
    const url = new URL(parsed.data.redirect_url);
    if (url.protocol !== "https:" || url.username || url.password || !(url.hostname === "composio.dev" || url.hostname.endsWith(".composio.dev"))) {
      throw new Error("Unexpected authorization link host.");
    }
    return parsed.data;
  }
  async verify(id: string, entity: string, config?: string) {
    if (!accountId.safeParse(id).success) throw new Error("Invalid connected account.");
    const parsed = accountSchema.safeParse(await this.request(`connected_accounts/${encodeURIComponent(id)}`));
    if (!parsed.success || parsed.data.id !== id || parsed.data.user_id !== entity || (config && parsed.data.auth_config.id !== config)) {
      throw new Error("The Outlook account could not be verified. Reconnect your mailbox.");
    }
    const profile = await this.request("tools/execute/proxy", "POST", {
      connected_account_id: id, user_id: entity, method: "GET", endpoint: "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName",
    });
    const address = profile?.data?.mail || profile?.data?.userPrincipalName;
    const mailbox = z.email().safeParse(typeof address === "string" ? address.trim().toLowerCase() : address);
    if (profile?.status !== 200 || !mailbox.success) throw new Error("Could not verify the Outlook mailbox address.");
    return mailbox.data;
  }
  async disable(id: string) {
    if (!accountId.safeParse(id).success) throw new Error("Invalid connected account.");
    await this.request(`connected_accounts/${encodeURIComponent(id)}/status`, "PATCH", { enabled: false });
    const account = await this.request(`connected_accounts/${encodeURIComponent(id)}`);
    if (account?.id !== id || account?.is_disabled !== true) throw new Error("Provider disconnection could not be confirmed. Retry cleanup.");
  }
}
