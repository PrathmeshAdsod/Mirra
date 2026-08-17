"use client";

import Image from "next/image";
import Link from "next/link";
import { ArrowsOut, BookmarkSimple, Check, Play, Sparkle, UserFocus, X } from "@phosphor-icons/react";
import { ChangeEvent, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import { getAccessToken } from "@/lib/supabase";

type ManifestLook = {
  id: string;
  label: string;
  poster_url?: string;
  is_hero?: boolean;
  remix_allowed?: boolean;
  segments: Array<{ start_seconds: number; end_seconds: number }>;
  remix_options: Array<{ id: string; label: string }>;
};
type MirrorResult = { id: string; look_id: string; remix_option_id?: string | null; provider_state: string; result_url?: string; error?: unknown };

function shortTime(value: number) {
  const minutes = Math.floor(value / 60).toString().padStart(2, "0");
  const seconds = Math.floor(value % 60).toString().padStart(2, "0");
  return `${minutes}:${seconds}`;
}

export function MirrorPlayer({ manifestId = null, initialSessionId = null }: { manifestId?: string | null; initialSessionId?: string | null }) {
  const [selected, setSelected] = useState(0);
  const [shopperPhoto, setShopperPhoto] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [zoomed, setZoomed] = useState(false);
  const [remixOpen, setRemixOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [results, setResults] = useState<MirrorResult[]>([]);
  const [displayResultId, setDisplayResultId] = useState<string | null>(null);
  const [pollVersion, setPollVersion] = useState(0);
  const [priorityReady, setPriorityReady] = useState(false);
  const [remixText, setRemixText] = useState("");
  const [selectedPresetId, setSelectedPresetId] = useState<string | null>(null);
  const [mobilePane, setMobilePane] = useState<"campaign" | "mirror">("campaign");
  const [manifestLooks, setManifestLooks] = useState<ManifestLook[]>([]);
  const [brandName, setBrandName] = useState("Brand");
  const [campaignName, setCampaignName] = useState("Campaign");
  const [campaignVideoUrl, setCampaignVideoUrl] = useState<string | null>(null);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const looks = useMemo(() => manifestLooks, [manifestLooks]);
  const currentLook = looks[Math.min(selected, looks.length - 1)];
  const currentLookId = currentLook?.id;
  const activePresetId = currentLook?.remix_options.some((option) => option.id === selectedPresetId) ? selectedPresetId : currentLook?.remix_options[0]?.id || null;
  const realResult = useMemo(() => {
    const successful = results.filter((result) => result.look_id === currentLookId && result.provider_state === "success" && result.result_url);
    return successful.find((result) => result.id === displayResultId) || successful.find((result) => !result.remix_option_id);
  }, [currentLookId, displayResultId, results]);
  const currentProviderState = results.find((result) => result.look_id === currentLookId && !result.remix_option_id)?.provider_state;

  useEffect(() => {
    return () => {
      if (shopperPhoto?.startsWith("blob:")) URL.revokeObjectURL(shopperPhoto);
    };
  }, [shopperPhoto]);

  useEffect(() => {
    if (!manifestId) return;
    let stopped = false;
    async function loadManifest() {
      const token = await getAccessToken();
      if (!token) {
        if (!stopped) setMessage("Sign in to open this published campaign.");
        return;
      }
      try {
        const payload = await apiFetch<{ brand: { name: string }; campaign: { name: string; duration_seconds: number; video_url: string }; looks: ManifestLook[] }>(`/v1/manifests/${manifestId}`, { headers: { Authorization: `Bearer ${token}` } });
        if (stopped) return;
        setBrandName(payload.brand.name);
        setCampaignName(payload.campaign.name);
        setDuration(Number(payload.campaign.duration_seconds) || 0);
        setCampaignVideoUrl(payload.campaign.video_url);
        setManifestLooks(payload.looks);
        const heroIndex = payload.looks.findIndex((look) => look.is_hero);
        const initialIndex = heroIndex >= 0 ? heroIndex : 0;
        setSelected(initialIndex);
        setCurrentTime(Number(payload.looks[initialIndex]?.segments[0]?.start_seconds || 0));
        setPriorityReady(true);
      } catch (error) {
        if (!stopped) setMessage(error instanceof Error ? error.message : "Campaign could not be opened.");
      }
    }
    void loadManifest();
    return () => { stopped = true; };
  }, [manifestId]);

  useEffect(() => {
    if (!sessionId) return;
    let stopped = false;
    let timer: number | undefined;
    async function refresh() {
      const token = await getAccessToken();
      if (!token) return;
      try {
        const payload = await apiFetch<{ status: string; saved: boolean; results: MirrorResult[] }>(`/v1/mirror-sessions/${sessionId}`, { headers: { Authorization: `Bearer ${token}` } });
        if (!stopped) {
          setResults(payload.results);
          setSaved(Boolean(payload.saved));
          const ready = payload.results.filter((result) => !result.remix_option_id && result.provider_state === "success").length;
          const providerWorkRemaining = payload.results.some((result) => ["queued", "submitting", "processing"].includes(result.provider_state));
          setMessage(`${ready} of ${looks.length} looks ready${providerWorkRemaining ? " · generating" : payload.status === "ready" ? " · mirror complete" : ""}`);
          if (!stopped && (providerWorkRemaining || !["ready", "partial", "failed"].includes(payload.status))) timer = window.setTimeout(refresh, 2500);
        }
      } catch (error) {
        if (!stopped) {
          setMessage(error instanceof Error ? error.message : "Mirror status could not be refreshed.");
          timer = window.setTimeout(refresh, 4000);
        }
      }
    }
    void refresh();
    return () => { stopped = true; if (timer) window.clearTimeout(timer); };
  }, [looks.length, pollVersion, sessionId]);

  useEffect(() => {
    if (!priorityReady || !sessionId || !currentLookId) return;
    async function prioritize() {
      const token = await getAccessToken();
      if (!token) return;
      await apiFetch(`/v1/mirror-sessions/${sessionId}/priority`, { method: "POST", body: JSON.stringify({ look_id: currentLookId }), headers: { Authorization: `Bearer ${token}` } }).catch(() => undefined);
    }
    void prioritize();
  }, [currentLookId, priorityReady, sessionId]);

  async function choosePhoto(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!/image\/(jpeg|png)/.test(file.type) || file.size > 10 * 1024 * 1024) {
      setMessage("Use one front-facing JPG or PNG under 10 MB.");
      return;
    }
    setShopperPhoto(URL.createObjectURL(file));
    setMobilePane("mirror");
    const token = await getAccessToken();
    if (!token) {
      setMessage("Sign in with Supabase to start the real YouCam generation.");
      return;
    }
    if (!manifestId || !looks[0]) {
      setMessage("Photo validated locally. Open a published campaign link to start the real mirror.");
      return;
    }
    const form = new FormData();
    form.append("file", file);
    try {
      const photo = await apiFetch<{ id: string }>("/v1/shopper/photos", { method: "POST", body: form, headers: { Authorization: `Bearer ${token}` } });
      const session = await apiFetch<{ id: string; results: Array<{ look_id: string; state: string }> }>("/v1/mirror-sessions", { method: "POST", body: JSON.stringify({ manifest_id: manifestId, shopper_photo_id: photo.id, initial_look_id: currentLookId || looks[0].id }), headers: { Authorization: `Bearer ${token}` } });
      setSessionId(session.id);
      setPriorityReady(true);
      setMessage("Current look queued first. Additional looks will appear progressively.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The mirror could not start.");
    }
  }

  function selectLook(index: number) {
    setSelected(index);
    setDisplayResultId(null);
    const start = Number(looks[index]?.segments[0]?.start_seconds || 0);
    setCurrentTime(start);
    if (videoRef.current) videoRef.current.currentTime = start;
  }

  function updatePlayback(time: number) {
    setCurrentTime(time);
    const matching = looks.findIndex((look) => look.segments.some((segment) => time >= Number(segment.start_seconds) && time < Number(segment.end_seconds)));
    if (matching >= 0 && matching !== selected) setSelected(matching);
  }

  async function togglePlayback() {
    const player = videoRef.current;
    if (!player) return;
    if (player.paused) await player.play();
    else player.pause();
  }

  function seekCampaign(time: number) {
    const next = Math.max(0, Math.min(duration, time));
    if (videoRef.current) videoRef.current.currentTime = next;
    updatePlayback(next);
  }

  async function toggleSaved() {
    const next = !saved;
    if (sessionId) {
      const token = await getAccessToken();
      if (token) await apiFetch(`/v1/mirror-sessions/${sessionId}/save`, { method: "POST", body: JSON.stringify({ saved: next }), headers: { Authorization: `Bearer ${token}` } }).catch((error) => setMessage(error instanceof Error ? error.message : "Save failed"));
    }
    setSaved(next);
  }

  async function submitRemix() {
    if (!sessionId || !currentLookId || !activePresetId) {
      setMessage("This look does not expose a brand-approved remix preset yet.");
      return;
    }
    const token = await getAccessToken();
    if (!token) return;
    try {
      const remix = await apiFetch<{ result_id: string }>(`/v1/mirror-sessions/${sessionId}/remix`, { method: "POST", body: JSON.stringify({ look_id: currentLookId, preset_id: activePresetId, text_constraint: remixText || null }), headers: { Authorization: `Bearer ${token}` } });
      setDisplayResultId(remix.result_id);
      setPollVersion((current) => current + 1);
      setRemixOpen(false);
      setMessage("Brand-approved remix queued with YouCam.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Remix could not start.");
    }
  }

  if (!manifestId) {
    return <main className="mirror-page mirror-entry"><header className="mirror-header"><Link className="wordmark" href="/">MIRRA</Link><p>YOUR MIRROR</p><Link href="/">Exit mirror</Link></header><section><p className="eyebrow">PUBLISHED CAMPAIGNS ONLY</p><h1>Open a campaign mirror.</h1><p>MIRRA never fabricates a provider result. Use a published campaign link or return to your saved mirrors.</p><Link className="mirror-entry-link" href="/mirrors">My Mirrors</Link></section></main>;
  }

  return (
    <main className={`mirror-page ${zoomed ? "zoomed" : ""}`}>
      <header className="mirror-header"><Link className="wordmark" href="/">MIRRA</Link><p>{brandName} / {campaignName}</p><Link href="/">Exit mirror</Link></header>
      <section className="mirror-labels"><span>CAMPAIGN</span><span>YOUR MIRROR</span></section>
      <div className="mobile-pane-toggle" aria-label="Mirror view"><button className={mobilePane === "campaign" ? "active" : ""} onClick={() => setMobilePane("campaign")}>Campaign</button><button className={mobilePane === "mirror" ? "active" : ""} onClick={() => setMobilePane("mirror")}>Your mirror</button></div>
      <section className={`diptych show-${mobilePane}`}>
        <div className="diptych-pane campaign-pane">
          {campaignVideoUrl ? <video ref={videoRef} src={campaignVideoUrl} playsInline preload="metadata" onLoadedMetadata={(event) => { setDuration(event.currentTarget.duration || duration); event.currentTarget.currentTime = currentTime; }} onTimeUpdate={(event) => updatePlayback(event.currentTarget.currentTime)} /> : <div className="provider-wait"><span>CAMPAIGN</span><p>Loading the immutable published manifest…</p></div>}
          <div className="mirror-playback"><button aria-label="Play campaign" onClick={togglePlayback}><Play weight="fill" /></button><span>{shortTime(currentTime)} / {shortTime(duration)}</span><input className="timeline-scrubber" aria-label="Seek campaign" type="range" min="0" max={Math.max(duration, 0.01)} step="0.01" value={Math.min(currentTime, Math.max(duration, 0.01))} onChange={(event) => seekCampaign(Number(event.target.value))} /></div>
        </div>
        <div className="diptych-pane result-pane">
          {realResult?.result_url ? <Image src={realResult.result_url} alt={`Your real YouCam result for ${currentLook?.label}`} fill unoptimized sizes="50vw" /> : shopperPhoto ? <><Image src={shopperPhoto} alt="Shopper source photo awaiting virtual try-on" fill unoptimized sizes="50vw" /><div className="provider-wait"><span>{["failed", "provider_unknown"].includes(currentProviderState || "") ? "YOUCAM UNAVAILABLE" : "YOUCAM PENDING"}</span><p>{["failed", "provider_unknown"].includes(currentProviderState || "") ? "This look could not be generated. No synthetic fallback is shown." : "Your real result replaces this clearly labelled source photo when the provider task completes."}</p></div></> : sessionId ? <div className="provider-wait provider-wait-center"><span>YOUCAM PENDING</span><p>The saved result is still being prepared.</p></div> : <label className="photo-gate"><UserFocus className="photo-gate-mark" /><b>Add one photo</b><small>Front-facing · shoulders visible · one person</small><input type="file" accept="image/jpeg,image/png" onChange={choosePhoto} /></label>}
        </div>
      </section>
      <div className="mirror-timeline" role="tablist" aria-label="Campaign looks" style={{ gridTemplateColumns: `repeat(${looks.length}, minmax(0, 1fr))` }}>{looks.map((look, index) => <button role="tab" aria-selected={selected === index} key={look.id} onClick={() => selectLook(index)}><span className={selected === index ? "active" : ""} />{look.label}</button>)}</div>
      <div className="mirror-actions">
        <button disabled={!sessionId || !currentLook?.remix_allowed || !currentLook.remix_options.length} onClick={() => setRemixOpen(true)}>Remix <Sparkle weight="fill" /></button>
        <button onClick={() => setZoomed(!zoomed)}>Zoom <ArrowsOut /></button>
        <button disabled={!sessionId} onClick={toggleSaved}>{saved ? "Saved" : "Save"}{saved ? <Check /> : <BookmarkSimple />}</button>
      </div>
      <p className="mirror-status" role="status">{message || (shopperPhoto ? "Waiting to start · real provider required" : "Add a valid photo to begin")}</p>
      {remixOpen && <aside className="remix-sheet" aria-label="Remix this look"><button className="sheet-close" onClick={() => setRemixOpen(false)} aria-label="Close remix"><X /></button><p className="eyebrow">BRAND-APPROVED REMIX</p><h2>Keep it within the campaign.</h2>{currentLook?.remix_options.length ? <div className="remix-presets">{currentLook.remix_options.map((option) => <button className={activePresetId === option.id ? "active" : ""} onClick={() => setSelectedPresetId(option.id)} key={option.id}>{option.label}</button>)}</div> : <p className="remix-empty">No approved alternatives for this look.</p>}<label>Optional constraint<input value={remixText} onChange={(event) => setRemixText(event.target.value)} placeholder="e.g. softer, still tailored" /></label><button className="primary-button" disabled={!sessionId || !activePresetId} onClick={submitRemix}>Generate real remix</button></aside>}
    </main>
  );
}
