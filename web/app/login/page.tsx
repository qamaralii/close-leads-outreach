import { AuthForm } from "../auth/form";
export default function Page() { return <section className="auth"><h1>Sign in</h1><p>Access is by invitation only.</p><AuthForm mode="login" /><p><a href="/auth/reset">Forgot password?</a></p></section>; }
