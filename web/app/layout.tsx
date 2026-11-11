import type { Metadata } from "next";
import "./style.css";
export const metadata: Metadata = { title: "Intugle Outreach", description: "Review and send personalized outreach with Intugle" };
export default function Layout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body><main><header className="app-header"><a className="brand" href="/" aria-label="Intugle Outreach home"><img className="brand-logo" src="/intugle-logo.png" width="480" height="171" alt="Intugle" /></a><span className="app-tagline">Personal outreach, thoughtfully sent.</span></header>{children}</main></body></html>;
}
