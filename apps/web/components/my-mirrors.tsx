"use client";

import Link from "next/link";
import Image from "next/image";
import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { getAccessToken } from "@/lib/supabase";
import { SignOutButton } from "./sign-out-button";

type SavedMirror = {
  id: string;
  manifest_id: string;
  status: string;
  created_at: string;
  campaign: { name?: string };
  results: Array<{ provider_state: string; remix_option_id?: string | null; result_url?: string }>;
};

export function MyMirrors() {
  const [sessions, setSessions] = useState<SavedMirror[]>([]);
  const [message, setMessage] = useState("Loading your saved mirrors…");

  useEffect(() => {
    void (async () => {
      const token = await getAccessToken();
      if (!token) {
        setMessage("Sign in to see your saved mirrors.");
        return;
      }
      try {
        const payload = await apiFetch<{ sessions: SavedMirror[] }>("/v1/mirror-sessions?saved=true", { headers: { Authorization: `Bearer ${token}` } });
        setSessions(payload.sessions);
        setMessage(payload.sessions.length ? "" : "You have not saved a mirror yet.");
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Saved mirrors could not be loaded.");
      }
    })();
  }, []);

  return (
    <main className="editorial-page mirrors-library">
      <header className="topbar"><Link className="wordmark" href="/">MIRRA</Link><p>MY MIRRORS</p><div className="header-actions"><Link href="/mirror">Open Mirror</Link><SignOutButton /></div></header>
      <section className="library-heading"><p className="eyebrow">SAVED</p><h1>Your mirrors.</h1><p>Only real completed YouCam results appear here.</p></section>
      {message && <p className="library-empty" role="status">{message}</p>}
      <div className="mirror-library-list">
        {sessions.map((session) => {
          const ready = session.results.filter((result) => result.provider_state === "success" && !result.remix_option_id);
          const preview = ready.find((result) => result.result_url)?.result_url;
          return <Link href={`/mirror?manifest=${session.manifest_id}&session=${session.id}`} key={session.id}><article>{preview ? <Image src={preview} alt="Saved YouCam mirror result" width={190} height={125} unoptimized /> : <div className="library-awaiting">Preparing</div>}<div><p className="eyebrow">{session.status}</p><h2>{session.campaign.name || "Published campaign"}</h2><p>{ready.length} real look{ready.length === 1 ? "" : "s"} ready · {new Date(session.created_at).toLocaleDateString()}</p></div></article></Link>;
        })}
      </div>
    </main>
  );
}
