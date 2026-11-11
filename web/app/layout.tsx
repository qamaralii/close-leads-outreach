import type { Metadata } from "next";
import "./style.css";
export const metadata: Metadata = { title: "Close Outreach", description: "Review and send personalized outreach" };
export default function Layout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body><main><header className="app-header"><a className="brand" href="/"><span className="brand-mark" aria-hidden="true">C</span>Close Outreach</a><span className="app-tagline">Personal outreach, thoughtfully sent.</span></header>{children}</main></body></html>;
}
