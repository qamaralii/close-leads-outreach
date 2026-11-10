import { logout } from "../auth/actions";
export default function Page() { return <section><h1>Membership required</h1><p>Ask your administrator to activate your outreach membership.</p><form action={logout}><button>Sign out</button></form></section>; }
