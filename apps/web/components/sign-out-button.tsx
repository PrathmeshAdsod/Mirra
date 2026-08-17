"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { getSupabaseBrowserClient } from "@/lib/supabase";

export function SignOutButton({ className = "quiet-button" }: { className?: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function signOut() {
    if (busy) return;
    setBusy(true);
    const supabase = getSupabaseBrowserClient();
    if (supabase) await supabase.auth.signOut({ scope: "local" });
    router.replace("/sign-in");
    router.refresh();
  }

  return <button type="button" className={className} onClick={signOut} disabled={busy}>{busy ? "Signing out…" : "Sign out"}</button>;
}
