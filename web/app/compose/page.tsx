import { requireMember } from "@/lib/auth";
import { Composer } from "./ui";
import { Navigation } from "../navigation";
export default async function Page() { await requireMember(); return <><Navigation current="compose" /><div className="page-heading"><div><div className="eyebrow">New campaign</div><h1>Create a campaign</h1><p>Select your contacts and write a message, then review before sending.</p></div></div><Composer /></>; }
