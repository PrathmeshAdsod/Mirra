"use client";

import Image from "next/image";
import { Pause, Play } from "@phosphor-icons/react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import { mediaUrl } from "@/lib/media";

const frames = ["campaign-hero.png", "look-01.png", "look-02.png", "look-03.png"];

export function CampaignPreview() {
  const [playing, setPlaying] = useState(false);
  const [frame, setFrame] = useState(0);
  const [videoProgress, setVideoProgress] = useState(0);
  const [published, setPublished] = useState<{ brand: { name: string }; campaign: { name: string; manifest_id: string; duration_seconds: number; video_url: string; poster_url?: string | null } } | null>(null);
  const [publicCampaignChecked, setPublicCampaignChecked] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    let stopped = false;
    void apiFetch<{ brand: { name: string }; campaign: { name: string; manifest_id: string; duration_seconds: number; video_url: string; poster_url?: string | null } }>("/v1/discover")
      .then((payload) => { if (!stopped) setPublished(payload); })
      .catch(() => undefined)
      .finally(() => { if (!stopped) setPublicCampaignChecked(true); });
    return () => { stopped = true; };
  }, []);

  useEffect(() => {
    if (!playing || published) return;
    const timer = window.setInterval(() => setFrame((current) => (current + 1) % frames.length), 1800);
    return () => window.clearInterval(timer);
  }, [playing, published]);

  async function togglePreview() {
    if (published && videoRef.current) {
      if (videoRef.current.paused) await videoRef.current.play();
      else videoRef.current.pause();
      setPlaying(!videoRef.current.paused);
      return;
    }
    setPlaying((current) => !current);
  }

  const allowDevelopmentMedia = process.env.NODE_ENV !== "production" && Boolean(process.env.NEXT_PUBLIC_DEMO_MEDIA_BASE_URL);
  const label = published ? `${published.brand.name} — ${published.campaign.name}` : "MIRRA EDITORIAL — STUDY 01";

  return (
    <section className="landing-media" id="discover" aria-label="MIRRA editorial campaign preview">
      {published ? <video ref={videoRef} src={published.campaign.video_url} poster={published.campaign.poster_url || undefined} muted playsInline preload="metadata" onTimeUpdate={(event) => setVideoProgress((event.currentTarget.currentTime / Math.max(event.currentTarget.duration, 0.01)) * 100)} onEnded={() => setPlaying(false)} /> : allowDevelopmentMedia ? <Image src={mediaUrl(frames[frame])} alt="Editorial fashion campaign in a concrete gallery" fill priority sizes="(max-width: 900px) 100vw, 94vw" /> : publicCampaignChecked ? <div className="landing-media-empty"><p>The next public campaign is being prepared.</p></div> : <div className="landing-media-empty"><p>Loading the latest campaign…</p></div>}
      <div className="media-shade" />
      {(published || allowDevelopmentMedia) && <button className="media-play" type="button" aria-label={playing ? "Pause campaign preview" : "Play campaign preview"} onClick={togglePreview}>{playing ? <Pause weight="fill" /> : <Play weight="fill" />}</button>}
      <div className="media-progress" aria-hidden="true"><span style={{ width: published ? `${videoProgress}%` : `${((frame + 1) / frames.length) * 100}%` }} /></div>
      <p className="campaign-label">{published ? <Link href={`/mirror?manifest=${published.campaign.manifest_id}`}>{label.toUpperCase()}</Link> : label}</p>
    </section>
  );
}
