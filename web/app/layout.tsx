import type { Metadata } from "next";
import "./style.css";
export const metadata: Metadata = { title: "Intugle Outreach", description: "Review and send personalized outreach with Intugle" };
export default function Layout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body><a className="skip-link" href="#main-content">Skip to content</a><div className="site-shell"><header className="app-header"><a className="brand" href="/" aria-label="Intugle Outreach home"><img className="brand-logo" src="/intugle-logo.png" width="480" height="171" alt="Intugle" /></a><div className="product-lockup"><span>OUTREACH</span><small>Personal outreach, thoughtfully sent.</small></div></header><main id="main-content">{children}</main></div></body></html>;
}
