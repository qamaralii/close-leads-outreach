"use client";

export default function ErrorPage({ reset }: { reset: () => void }) {
  return <section className="auth auth-state"><span className="state-icon" aria-hidden="true">!</span><span className="eyebrow">Something went wrong</span><h1>We couldn’t load this page</h1><p>Your work may still be saved. Try loading the page again before repeating an action.</p><button onClick={reset}>Try again</button><a className="text-link" href="/">Return to campaigns</a></section>;
}
