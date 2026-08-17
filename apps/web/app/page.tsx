import Link from "next/link";
import { Sparkle } from "@phosphor-icons/react/dist/ssr";
import { CampaignPreview } from "@/components/campaign-preview";

export default function HomePage() {
  return (
    <main className="editorial-page landing-page">
      <header className="topbar landing-topbar">
        <Link className="wordmark" href="/">MIRRA</Link>
        <nav className="center-nav" aria-label="Primary navigation">
          <Link href="#discover">Discover</Link>
          <Link href="/brand/campaigns/new">For Brands</Link>
          <Link href="/mirrors">My Mirrors</Link>
        </nav>
        <Link href="/sign-in">Sign in</Link>
      </header>

      <section className="landing-intro">
        <h1>Wear the<br />campaign.</h1>
        <div className="landing-pitch">
          <p>Your photo. Every confirmed look.<br />One living mirror.</p>
          <Link className="primary-button" href="/mirror">Mirror Me <Sparkle weight="fill" /></Link>
        </div>
      </section>

      <CampaignPreview />
    </main>
  );
}
