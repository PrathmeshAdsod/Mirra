"use client";

import Image from "next/image";
import Link from "next/link";
import { CaretDown, PencilSimple, Play, Sparkle } from "@phosphor-icons/react";
import { ChangeEvent, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import { getAccessToken } from "@/lib/supabase";
import { BrandShell } from "./brand-shell";

const MAX_BYTES = 45 * 1024 * 1024;

type GarmentCategory = "outerwear" | "full_body" | "upper_body" | "lower_body" | "shoes" | "auto";
type RegisteredProduct = { productId: string; referenceAssetId: string; name: string; category: GarmentCategory };
type Segment = { start_seconds: number; end_seconds: number };
type DetectedLook = {
  id: string;
  label: string;
  garment_category: GarmentCategory;
  is_hero: boolean;
  remix_allowed: boolean;
  product_id: string | null;
  reference_asset_id: string | null;
  poster_url?: string;
  segments: Segment[];
  remix_options: Array<{ id: string; label: string; reference_asset_id: string; garment_category: GarmentCategory; constraints?: { allowed_tags?: string[] } }>;
  sort_order: number;
};
type RemixOptionEdit = { referenceAssetId: string; label: string; category: GarmentCategory; allowedTags: string[] };
type LookEdit = {
  productId: string;
  referenceAssetId: string;
  category: GarmentCategory;
  isHero: boolean;
  remixAllowed: boolean;
  segments: Segment[];
  remixOptions: RemixOptionEdit[];
};
type CampaignPayload = {
  name: string;
  status: string;
  published_manifest_id?: string | null;
  duration_seconds?: number;
  source_url?: string;
  processing_error?: string | null;
  looks: DetectedLook[];
  products: Array<{ id: string; name: string; metadata?: { garment_category?: GarmentCategory }; reference_asset_id?: string | null }>;
};

function suggestedCategory(name: string): GarmentCategory {
  const value = name.toLowerCase();
  if (/coat|jacket|blazer|outerwear/.test(value)) return "outerwear";
  if (/dress|suit|jumpsuit|full.?body|complete look/.test(value)) return "full_body";
  if (/shirt|blouse|top|sweater|hoodie/.test(value)) return "upper_body";
  if (/pants|trouser|skirt|shorts|lower.?body/.test(value)) return "lower_body";
  if (/shoe|boot|sneaker|heel/.test(value)) return "shoes";
  return "auto";
}

function formatSeconds(value: number) {
  const minutes = Math.floor(value / 60).toString().padStart(2, "0");
  const seconds = (value % 60).toFixed(2).padStart(5, "0");
  return `${minutes}:${seconds}`;
}

function segmentSummary(segments: Segment[]) {
  if (!segments.length) return "No segment";
  if (segments.length === 1) return `${formatSeconds(Number(segments[0].start_seconds))} — ${formatSeconds(Number(segments[0].end_seconds))}`;
  return `${segments.length} segments · ${formatSeconds(Number(segments[0].start_seconds))} — ${formatSeconds(Number(segments.at(-1)?.end_seconds || 0))}`;
}

function defaultEdit(look: DetectedLook, products: RegisteredProduct[]): LookEdit {
  const product = products.find((item) => item.productId === look.product_id) || products[0];
  return {
    productId: product?.productId || "",
    referenceAssetId: product?.referenceAssetId || "",
    category: look.garment_category || product?.category || "auto",
    isHero: look.is_hero,
    remixAllowed: look.remix_allowed,
    segments: look.segments.map((segment) => ({ start_seconds: Number(segment.start_seconds), end_seconds: Number(segment.end_seconds) })),
    remixOptions: (look.remix_options || []).map((option) => ({ referenceAssetId: option.reference_asset_id, label: option.label, category: option.garment_category, allowedTags: option.constraints?.allowed_tags || [] })),
  };
}

function AlternativePicker({ products, mappedReferenceId, options, onChange }: { products: RegisteredProduct[]; mappedReferenceId: string; options: RemixOptionEdit[]; onChange: (options: RemixOptionEdit[]) => void }) {
  const candidates = products.filter((product) => product.referenceAssetId !== mappedReferenceId);
  return (
    <fieldset className="alternative-picker">
      <legend>Brand-approved alternatives</legend>
      {candidates.map((product) => {
        const chosen = options.find((option) => option.referenceAssetId === product.referenceAssetId);
        return (
          <div className="alternative-row" key={product.productId}>
            <label><input type="checkbox" checked={Boolean(chosen)} onChange={(event) => onChange(event.target.checked ? [...options, { referenceAssetId: product.referenceAssetId, label: product.name, category: product.category, allowedTags: [] }] : options.filter((option) => option.referenceAssetId !== product.referenceAssetId))} />{product.name}</label>
            {chosen && <input aria-label={`${product.name} allowed remix words`} value={chosen.allowedTags.join(", ")} placeholder="Allowed words, comma separated" onChange={(event) => onChange(options.map((option) => option.referenceAssetId === product.referenceAssetId ? { ...option, allowedTags: event.target.value.split(",").map((tag) => tag.trim()).filter(Boolean) } : option))} />}
          </div>
        );
      })}
      {!candidates.length && <p>Add another product reference to approve an alternative.</p>}
    </fieldset>
  );
}

export function BrandCampaignBuilder({ initialStep = 0, initialCampaignId = null }: { initialStep?: number; initialCampaignId?: string | null }) {
  const [step, setStep] = useState(Math.max(0, Math.min(5, initialStep)));
  const [campaignName, setCampaignName] = useState("");
  const [video, setVideo] = useState<File | null>(null);
  const [reference, setReference] = useState<File | null>(null);
  const [referenceInputKey, setReferenceInputKey] = useState(0);
  const [campaignId, setCampaignId] = useState<string | null>(initialCampaignId);
  const [productName, setProductName] = useState("");
  const [registeredProducts, setRegisteredProducts] = useState<RegisteredProduct[]>([]);
  const [direction, setDirection] = useState("");
  const [message, setMessage] = useState("");
  const [detectedLooks, setDetectedLooks] = useState<DetectedLook[]>([]);
  const [lookEdits, setLookEdits] = useState<Record<string, LookEdit>>({});
  const [sourceVideoUrl, setSourceVideoUrl] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  const [boundaryOpen, setBoundaryOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [videoTime, setVideoTime] = useState(0);
  const [videoDuration, setVideoDuration] = useState(0);
  const [publishedManifestId, setPublishedManifestId] = useState<string | null>(null);
  const [campaignPublished, setCampaignPublished] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const title = useMemo(() => ["Name the campaign", "Add the campaign film", "Add products and references", "Set the creative direction", "Review detected looks", "Ready to publish"][step], [step]);
  const reviewLooks = detectedLooks;
  const availableProducts = registeredProducts;
  const activeLook = reviewLooks[Math.min(selected, reviewLooks.length - 1)];
  const activeEdit = activeLook ? lookEdits[activeLook.id] || defaultEdit(activeLook, availableProducts) : null;

  useEffect(() => {
    if (step !== 4 || !campaignId) return;
    let stopped = false;
    let timer: number | undefined;
    async function refreshCampaign() {
      const token = await getAccessToken();
      if (!token || stopped) return;
      try {
        const payload = await apiFetch<CampaignPayload>(`/v1/campaigns/${campaignId}`, { headers: { Authorization: `Bearer ${token}` } });
        if (stopped) return;
        setCampaignName(payload.name);
        setCampaignPublished(payload.status === "published");
        if (payload.published_manifest_id) setPublishedManifestId(payload.published_manifest_id);
        if (payload.duration_seconds) setVideoDuration(Number(payload.duration_seconds));
        if (payload.source_url) setSourceVideoUrl(payload.source_url);
        const products = payload.products.filter((item) => item.reference_asset_id).map((item) => ({
          productId: item.id,
          referenceAssetId: item.reference_asset_id as string,
          name: item.name,
          category: item.metadata?.garment_category || "auto",
        }));
        if (products.length) setRegisteredProducts(products);
        if (payload.status === "failed") {
          setMessage(payload.processing_error || "Campaign analysis failed.");
          return;
        }
        if (["review", "published"].includes(payload.status) && payload.looks.length) {
          setDetectedLooks(payload.looks);
          setSelected((current) => Math.min(current, payload.looks.length - 1));
          setLookEdits((current) => {
            const next = { ...current };
            for (const look of payload.looks) next[look.id] ||= defaultEdit(look, products);
            return next;
          });
          setMessage(payload.status === "published" ? `${payload.looks.length} confirmed looks are published.` : `${payload.looks.length} detected looks are ready for confirmation.`);
          return;
        }
        setMessage(payload.status === "analyzing" ? "Gemini is analyzing the campaign once. You can leave this page open." : "Preparing campaign mechanics before the single Gemini analysis.");
        timer = window.setTimeout(refreshCampaign, 2500);
      } catch (error) {
        if (!stopped) {
          setMessage(error instanceof Error ? error.message : "Campaign status could not be refreshed.");
          timer = window.setTimeout(refreshCampaign, 4000);
        }
      }
    }
    void refreshCampaign();
    return () => { stopped = true; if (timer) window.clearTimeout(timer); };
  }, [campaignId, step]);

  useEffect(() => {
    if (!activeLook || !videoRef.current) return;
    const edit = lookEdits[activeLook.id] || defaultEdit(activeLook, availableProducts);
    const start = Number(edit.segments[0]?.start_seconds || 0);
    videoRef.current.currentTime = start;
    setVideoTime(start);
  }, [activeLook?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  function updateLook(patch: Partial<LookEdit>) {
    if (!activeLook) return;
    setLookEdits((current) => ({ ...current, [activeLook.id]: { ...(current[activeLook.id] || defaultEdit(activeLook, availableProducts)), ...patch } }));
  }

  function updateSegment(index: number, patch: Partial<Segment>) {
    if (!activeEdit) return;
    updateLook({ segments: activeEdit.segments.map((segment, segmentIndex) => segmentIndex === index ? { ...segment, ...patch } : segment) });
  }

  function addSegmentAtPlayhead() {
    if (!activeEdit || videoDuration <= 0) return;
    const start = Math.min(Math.max(0, videoTime), Math.max(0, videoDuration - 0.05));
    const end = Math.min(videoDuration, start + 1);
    updateLook({ segments: [...activeEdit.segments, { start_seconds: start, end_seconds: Math.max(start + 0.05, end) }].sort((a, b) => a.start_seconds - b.start_seconds) });
  }

  function removeSegment(index: number) {
    if (!activeEdit) return;
    updateLook({ segments: activeEdit.segments.filter((_segment, segmentIndex) => segmentIndex !== index) });
  }

  function chooseVideo(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0] || null;
    if (!file) return;
    if (file.size > MAX_BYTES) {
      setVideo(null);
      setMessage("This file is over 45 MB. Compress it to MP4/H.264 and try again.");
      return;
    }
    setVideo(file);
    setMessage(`${file.name} is ready. Deterministic preprocessing will start after upload.`);
  }

  async function saveCurrentProduct() {
    if (!campaignId || !reference || !productName.trim()) {
      setMessage("Choose a YouCam-ready reference first.");
      return null;
    }
    const token = await getAccessToken();
    if (!token) {
      setMessage("Sign in with a configured Supabase account to continue.");
      return null;
    }
    setBusy(true);
    const category = suggestedCategory(productName);
    const form = new FormData();
    form.append("file", reference);
    form.append("product_name", productName.trim());
    form.append("garment_category", category);
    try {
      const result = await apiFetch<{ product_id: string; reference_asset_id: string; garment_category: GarmentCategory }>(`/v1/campaigns/${campaignId}/references`, { method: "POST", body: form, headers: { Authorization: `Bearer ${token}` } });
      const product = { productId: result.product_id, referenceAssetId: result.reference_asset_id, name: productName.trim(), category: result.garment_category };
      setRegisteredProducts((current) => [...current, product]);
      setReference(null);
      setReferenceInputKey((current) => current + 1);
      setProductName("");
      setMessage(`${product.name} is ready. Add another product or continue.`);
      return product;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Reference could not be saved.");
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function continueFlow() {
    if (step === 0 && !campaignName.trim()) {
      setMessage("Name the campaign before continuing.");
      return;
    }
    const token = await getAccessToken();
    if (!token && step > 0) {
      setMessage("Sign in with a configured Supabase account to continue the real workflow.");
      return;
    }
    if (step === 1 && video && !campaignId) {
      setBusy(true);
      const form = new FormData();
      form.append("file", video);
      form.append("campaign_name", campaignName.trim() || "Untitled campaign");
      try {
        const result = await apiFetch<{ campaign_id: string }>("/v1/campaigns/preprocess", { method: "POST", body: form, headers: { Authorization: `Bearer ${token}` } });
        setCampaignId(result.campaign_id);
        setMessage("Upload accepted. FFprobe and FFmpeg are processing it in the background.");
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Upload could not start.");
        setBusy(false);
        return;
      }
      setBusy(false);
    }
    if (step === 2) {
      if (reference) {
        const saved = await saveCurrentProduct();
        if (!saved) return;
      } else if (!registeredProducts.length) {
        setMessage("Add at least one product and reference before continuing.");
        return;
      }
    }
    if (step === 3) {
      if (!campaignId) {
        setMessage("Upload the campaign video first.");
        return;
      }
      setBusy(true);
      try {
        await apiFetch(`/v1/campaigns/${campaignId}/analyze`, { method: "POST", body: JSON.stringify({ brand_direction: direction }), headers: { Authorization: `Bearer ${token}` } });
        setMessage("One Gemini campaign analysis is queued. Identical inputs reuse the stored analysis.");
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Analysis could not be queued.");
        setBusy(false);
        return;
      }
      setBusy(false);
    }
    if (step === 5 && campaignId) {
      setBusy(true);
      try {
        const result = await apiFetch<{ manifest_id: string }>(`/v1/campaigns/${campaignId}/publish`, { method: "POST", headers: { Authorization: `Bearer ${token}` } });
        setPublishedManifestId(result.manifest_id);
        setMessage(`Published immutable manifest ${result.manifest_id}.`);
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Campaign could not be published.");
      }
      setBusy(false);
      return;
    }
    setStep((current) => Math.min(5, current + 1));
  }

  async function saveReview() {
    if (!campaignId) {
      setStep(5);
      return;
    }
    const token = await getAccessToken();
    if (!token) return;
    if (!detectedLooks.length) {
      setMessage("Wait for the detected looks before saving.");
      return;
    }
    setBusy(true);
    try {
      for (const look of detectedLooks) {
        const edit = lookEdits[look.id] || defaultEdit(look, registeredProducts);
        if (!edit.productId || !edit.referenceAssetId) throw new Error(`${look.label} needs a mapped product and validated reference.`);
        if (!edit.segments.length) throw new Error(`${look.label} needs at least one confirmed campaign segment.`);
        await apiFetch(`/v1/campaigns/${campaignId}/looks/${look.id}`, {
          method: "PATCH",
          headers: { Authorization: `Bearer ${token}` },
          body: JSON.stringify({
            product_id: edit.productId,
            reference_asset_id: edit.referenceAssetId,
            garment_category: edit.category,
            is_hero: edit.isHero,
            remix_allowed: edit.remixAllowed,
            segments: edit.segments,
            remix_options: edit.remixAllowed ? edit.remixOptions.map((option) => ({ reference_asset_id: option.referenceAssetId, label: option.label, garment_category: option.category, allowed_tags: option.allowedTags })) : [],
          }),
        });
      }
      setMessage("Every detected look is confirmed and ready to publish.");
      setStep(5);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Look confirmation could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  async function toggleVideo() {
    const player = videoRef.current;
    if (!player) return;
    if (player.paused) await player.play();
    else player.pause();
  }

  if (step === 4 && campaignId && !detectedLooks.length) {
    return (
      <BrandShell active={4}>
        <section className="builder-stage analysis-wait"><p className="eyebrow">ONE CAMPAIGN ANALYSIS</p><Sparkle weight="fill" /><h1>Preparing the review.</h1><p>{message || "FFmpeg mechanics and the single Gemini analysis are running in the background."}</p><button className="secondary-button" onClick={() => setStep(3)}>Back to direction</button></section>
      </BrandShell>
    );
  }

  if (step === 4 && activeLook && activeEdit) {
    return (
      <BrandShell active={4}>
        <section className="review-header"><div><h1>Review detected looks</h1><p>{campaignId && !detectedLooks.length ? "Mirra is preparing the confirmed review." : `Mirra found ${reviewLooks.length} distinct looks. Confirm each one before publishing.`}</p></div><p className="campaign-title">{campaignName.toUpperCase()}</p></section>
        <section className="review-media">
          {sourceVideoUrl ? <video ref={videoRef} src={sourceVideoUrl} preload="metadata" playsInline onLoadedMetadata={(event) => setVideoDuration(event.currentTarget.duration || videoDuration)} onTimeUpdate={(event) => setVideoTime(event.currentTarget.currentTime)} /> : <div className="review-media-empty">Campaign video is temporarily unavailable.</div>}
          <div className="media-shade" />
          <div className="review-controls"><button aria-label="Play campaign" onClick={toggleVideo}><Play weight="fill" /></button><span>{formatSeconds(videoTime)} / {formatSeconds(videoDuration)}</span><div className="media-progress"><span style={{ width: `${Math.min(100, (videoTime / Math.max(videoDuration, 0.01)) * 100)}%` }} /></div></div>
        </section>
        <div className="look-filmstrip" role="tablist" aria-label="Detected looks" style={{ gridTemplateColumns: `repeat(${reviewLooks.length}, minmax(0, 1fr))` }}>
          {reviewLooks.map((item, index) => {
            const edit = lookEdits[item.id] || defaultEdit(item, availableProducts);
            return <button key={item.id} role="tab" aria-selected={selected === index} onClick={() => { setSelected(index); setBoundaryOpen(false); }} className={selected === index ? "selected" : ""}><span className="look-image">{item.poster_url ? <Image src={item.poster_url} alt="" fill sizes={`${Math.max(20, 100 / reviewLooks.length)}vw`} loading="eager" unoptimized /> : <span className="look-poster-empty">Poster unavailable</span>}</span><span><b>{item.label}</b><small>{segmentSummary(edit.segments)}</small></span></button>;
          })}
        </div>
        <section className="selected-look-controls" aria-label={`${activeLook.label} controls`}>
          <label>Mapped product<span className="select-wrap"><select value={activeEdit.productId} onChange={(event) => { const product = availableProducts.find((item) => item.productId === event.target.value); if (product) updateLook({ productId: product.productId, referenceAssetId: product.referenceAssetId, category: activeEdit.category === "auto" ? product.category : activeEdit.category, remixOptions: activeEdit.remixOptions.filter((option) => option.referenceAssetId !== product.referenceAssetId) }); }}><option value="" disabled>Choose product</option>{availableProducts.map((product) => <option value={product.productId} key={product.productId}>{product.name}</option>)}</select><CaretDown /></span></label>
          <label>Hero look<button type="button" className={`switch ${activeEdit.isHero ? "on" : ""}`} onClick={() => setLookEdits((current) => Object.fromEntries(reviewLooks.map((item) => { const edit = current[item.id] || defaultEdit(item, availableProducts); return [item.id, { ...edit, isHero: item.id === activeLook.id }]; })))} aria-pressed={activeEdit.isHero}><span /></button></label>
          <label>Remix allowed<button type="button" className={`switch ${activeEdit.remixAllowed ? "on" : ""}`} onClick={() => updateLook({ remixAllowed: !activeEdit.remixAllowed })} aria-pressed={activeEdit.remixAllowed}><span /></button></label>
          <label>Boundary times<button type="button" className="time-field" onClick={() => setBoundaryOpen((current) => !current)}>{segmentSummary(activeEdit.segments)}<PencilSimple /></button></label>
          {boundaryOpen && <div className="boundary-editor">{activeEdit.segments.map((segment, index) => <div className="boundary-row" key={`${activeLook.id}-${index}`}><small>Segment {index + 1}</small><label>Start seconds<input type="number" min="0" step="0.05" value={segment.start_seconds} onChange={(event) => updateSegment(index, { start_seconds: Number(event.target.value) })} /></label><label>End seconds<input type="number" min={Number(segment.start_seconds) + 0.05} max={videoDuration} step="0.05" value={segment.end_seconds} onChange={(event) => updateSegment(index, { end_seconds: Number(event.target.value) })} /></label><button type="button" className="boundary-remove" onClick={() => removeSegment(index)}>Remove</button></div>)}<button type="button" className="boundary-add" onClick={addSegmentAtPlayhead}>Add segment at {formatSeconds(videoTime)}</button></div>}
          <details className="advanced-controls"><summary>Advanced look controls</summary><label>Garment category<span className="select-wrap"><select value={activeEdit.category} onChange={(event) => updateLook({ category: event.target.value as GarmentCategory })}><option value="outerwear">Outerwear</option><option value="full_body">Full body</option><option value="upper_body">Upper body</option><option value="lower_body">Lower body</option><option value="shoes">Shoes</option><option value="auto">Auto fallback</option></select><CaretDown /></span></label>{activeEdit.remixAllowed && <AlternativePicker products={availableProducts} mappedReferenceId={activeEdit.referenceAssetId} options={activeEdit.remixOptions} onChange={(remixOptions) => updateLook({ remixOptions })} />}</details>
        </section>
        {message && <p className="form-message review-message" role="status">{message}</p>}
        <footer className="review-footer"><button className="secondary-button" onClick={() => setStep(3)}>Back</button><button className="primary-button" disabled={busy || campaignPublished || (Boolean(campaignId) && !detectedLooks.length)} onClick={saveReview}>{campaignPublished ? "Published" : busy ? "Saving…" : <>Save &amp; Preview <Sparkle weight="fill" /></>}</button></footer>
      </BrandShell>
    );
  }

  return (
    <BrandShell active={step}>
      <section className="builder-stage">
        <p className="eyebrow">CAMPAIGN SETUP</p><h1>{title}</h1>
        {step === 0 && <div className="stack-form"><label>Campaign name<input value={campaignName} onChange={(event) => setCampaignName(event.target.value)} placeholder="e.g. Autumn study" autoFocus /></label></div>}
        {step === 1 && <div className="upload-field"><input id="campaign-video" type="file" accept="video/mp4,video/quicktime,video/webm,video/x-matroska" onChange={chooseVideo} disabled={Boolean(campaignId)} /><label htmlFor="campaign-video"><b>{campaignId ? "Campaign video uploaded" : video ? video.name : "Choose campaign video"}</b><span>Up to 45 MB · 30 seconds by default · MP4/H.264 preferred</span></label></div>}
        {step === 2 && <div className="stack-form"><label>Product name<input value={productName} onChange={(event) => setProductName(event.target.value)} placeholder="e.g. Tailored jacket" /></label><label>YouCam-ready reference<input key={referenceInputKey} type="file" accept="image/jpeg,image/png" onChange={(event) => setReference(event.target.files?.[0] || null)} /></label><p className="field-help">Front-facing, one garment or a complete worn look. Under 10 MB. Confirm the exact category later in Advanced look controls.</p>{reference && <button type="button" className="secondary-button add-product-button" disabled={busy || !productName.trim()} onClick={() => void saveCurrentProduct()}>{busy ? "Saving…" : "Add product"}</button>}{registeredProducts.length > 0 && <ul className="product-list" aria-label="Campaign products">{registeredProducts.map((product) => <li key={product.productId}><span>{product.name}</span><small>{product.category === "auto" ? "Category to confirm" : product.category.replace("_", " ")}</small></li>)}</ul>}</div>}
        {step === 3 && <div className="stack-form"><label>Campaign direction<textarea value={direction} onChange={(event) => setDirection(event.target.value)} placeholder="Describe the intended hero look, styling rules, product priorities and allowed remix direction." /></label><p className="field-help">Mirra combines this with products, FFmpeg timing candidates, and the video in one Gemini analysis.</p></div>}
        {step === 5 && <div className="completion-panel"><Sparkle weight="fill" /><p>Publishing creates an immutable version of the confirmed looks, segments, products, references and remix rules.</p>{publishedManifestId && <Link className="secondary-button" href={`/mirror?manifest=${publishedManifestId}`}>Open shopper mirror</Link>}</div>}
        {message && <p className="form-message" role="status">{message}</p>}
        <div className="builder-actions">{step > 0 && <button className="secondary-button" onClick={() => setStep((current) => current - 1)}>Back</button>}<button className="primary-button" disabled={busy || (step === 0 && !campaignName.trim()) || (step === 1 && !video) || (step === 2 && !reference && !registeredProducts.length) || (step === 3 && !direction.trim()) || Boolean(publishedManifestId)} onClick={continueFlow}>{busy ? "Working…" : step === 2 && reference ? "Save & Continue" : step === 3 ? "Analyze once" : step === 5 ? publishedManifestId ? "Published" : `Publish ${campaignName}` : "Continue"}</button></div>
      </section>
    </BrandShell>
  );
}
