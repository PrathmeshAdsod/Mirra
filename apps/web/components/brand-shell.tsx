"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { getAccessToken } from "@/lib/supabase";
import { SignOutButton } from "./sign-out-button";

const steps = ["Basics", "Video", "Products", "Direction", "Review", "Publish"];

export function BrandShell({ active, children }: { active: number; children: React.ReactNode }) {
  const [workspaceName, setWorkspaceName] = useState("Brand workspace");

  useEffect(() => {
    let stopped = false;
    async function loadWorkspace() {
      const token = await getAccessToken();
      if (!token) return;
      const profile = await apiFetch<{ brand_name: string | null }>("/v1/me", { headers: { Authorization: `Bearer ${token}` } }).catch(() => null);
      if (!stopped && profile?.brand_name) setWorkspaceName(profile.brand_name);
    }
    void loadWorkspace();
    return () => { stopped = true; };
  }, []);

  return (
    <main className="editorial-page brand-page">
      <header className="topbar brand-topbar">
        <div className="brand-lockup"><Link className="wordmark" href="/">MIRRA</Link><span>/ BRAND</span></div>
        <nav className="center-nav" aria-label="Brand navigation"><Link href="/brand/campaigns/new">Campaigns</Link><span>Collections</span><span>Brand profile</span></nav>
        <div className="workspace-control"><span>{workspaceName}</span><SignOutButton /></div>
      </header>
      <ol className="stepper" aria-label="Campaign progress">
        {steps.map((step, index) => (
          <li key={step} className={index === active ? "active" : index < active ? "complete" : ""}>
            <span>{index + 1}</span><b>{step}</b>
          </li>
        ))}
      </ol>
      {children}
    </main>
  );
}
