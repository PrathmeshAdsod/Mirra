"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { getSupabaseBrowserClient } from "@/lib/supabase";

export function SignOutButton({ className = "quiet-button" }: { className?: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const supabase = getSupabaseBrowserClient();
    if (!supabase) return;
    void supabase.auth.getSession().then(({ data }) => setVisible(Boolean(data.session)));
    const { data } = supabase.auth.onAuthStateChange((_event, session) => setVisible(Boolean(session)));
    return () => data.subscription.unsubscribe();
  }, []);

  async function signOut() {
    if (busy) return;
    setBusy(true);
    const supabase = getSupabaseBrowserClient();
    if (supabase) await supabase.auth.signOut({ scope: "local" });
    router.replace("/sign-in");
    router.refresh();
  }

  if (!visible) return null;
  return <button type="button" className={className} onClick={signOut} disabled={busy}>{busy ? "Signing out…" : "Sign out"}</button>;
}
