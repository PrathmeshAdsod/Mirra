"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { apiFetch } from "@/lib/api";
import { getSupabaseBrowserClient } from "@/lib/supabase";

export default function SignInPage() {
  const router = useRouter();
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const supabase = getSupabaseBrowserClient();
    if (!supabase) {
      setMessage("Supabase is not configured. Add the two public keys from SETUP.md.");
      return;
    }
    setBusy(true);
    const form = new FormData(event.currentTarget);
    const { data, error } = await supabase.auth.signInWithPassword({
      email: String(form.get("email")),
      password: String(form.get("password")),
    });
    setBusy(false);
    if (error) {
      setMessage(error.message);
      return;
    }
    const requested = new URLSearchParams(window.location.search).get("next");
    if (requested?.startsWith("/") && !requested.startsWith("//")) {
      router.push(requested);
      return;
    }
    try {
      const profile = await apiFetch<{ brand_id: string | null }>("/v1/me", { headers: { Authorization: `Bearer ${data.session?.access_token}` } });
      router.push(profile.brand_id ? "/brand/campaigns/new" : "/mirrors");
    } catch {
      router.push("/mirrors");
    }
  }

  return (
    <main className="editorial-page auth-page">
      <header className="topbar"><Link className="wordmark" href="/">MIRRA</Link></header>
      <section className="auth-panel">
        <p className="eyebrow">WELCOME BACK</p>
        <h1>Enter your mirror.</h1>
        <form onSubmit={signIn} className="stack-form">
          <label>Email<input name="email" type="email" autoComplete="email" required /></label>
          <label>Password<input name="password" type="password" autoComplete="current-password" required /></label>
          <button className="primary-button" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
        {message && <p className="form-message" role="status">{message}</p>}
      </section>
    </main>
  );
}
