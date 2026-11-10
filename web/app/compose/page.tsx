import { requireMember } from "@/lib/auth";
import { Composer } from "./ui";
export default async function Page() { await requireMember(); return <><nav><a href="/">Campaigns</a></nav><h1>Create a campaign</h1><Composer /></>; }
