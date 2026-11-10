import type { Metadata } from "next";
import "./style.css";
export const metadata: Metadata = { title: "Close Outreach", description: "Review and send personalized outreach" };
export default function Layout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body><main><header><a href="/">Close Outreach</a></header>{children}</main></body></html>;
}
