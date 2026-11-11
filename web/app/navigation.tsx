export function Navigation({ current, children }: { current: "campaigns" | "compose" | "outlook"; children?: React.ReactNode }) {
  return <nav aria-label="Main navigation">
    <a href="/" aria-current={current === "campaigns" ? "page" : undefined}>Campaigns</a>
    <a href="/compose" aria-current={current === "compose" ? "page" : undefined}>Create campaign</a>
    <a href="/settings/outlook" aria-current={current === "outlook" ? "page" : undefined}>Outlook settings</a>
    {children}
  </nav>;
}
