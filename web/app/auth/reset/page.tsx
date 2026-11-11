import { AuthForm } from "../form";
export default function Page() { return <section className="auth"><div className="auth-intro"><span className="eyebrow">Account recovery</span><h1>Reset your password</h1><p>Enter your account email and we’ll send a secure recovery link.</p></div><AuthForm mode="reset" /><p className="auth-footer"><a href="/login">Back to sign in</a></p></section>; }
