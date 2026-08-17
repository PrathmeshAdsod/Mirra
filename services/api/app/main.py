from __future__ import annotations

import hashlib
import io
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, UnidentifiedImageError

from .auth import require_user
from .config import get_settings
from .models import CampaignAnalyzeRequest, CampaignDirectionRequest, GarmentCategory, LookReviewRequest, MirrorSessionRequest, PriorityRequest, RemixRequest, SaveMirrorRequest, YouCamTaskRequest
from .providers.gemini import SCHEMA_VERSION, GeminiError, GeminiInteractions, campaign_cache_key
from .providers.youcam import YouCamClothesV3
from .repository import RepositoryNotConfigured, SupabaseRepository

settings = get_settings()
app = FastAPI(title="MIRRA API", version="0.1.0", docs_url="/docs" if settings.app_env != "production" else None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.web_origin.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska"}
IMAGE_TYPES = {"image/jpeg", "image/png"}


def repository() -> SupabaseRepository:
    try:
        return SupabaseRepository(settings)
    except RepositoryNotConfigured as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error


async def read_limited(upload: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"File exceeds the {limit // (1024 * 1024)} MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def reserve_daily_capacity(
    repo: SupabaseRepository,
    *,
    event_name: str,
    limit: int,
    reservation_keys: list[str],
    user_id: str | None = None,
) -> None:
    keys = sorted(set(reservation_keys))
    if not keys:
        return
    reserved = await repo.rpc(
        "reserve_daily_capacity",
        {
            "p_event_name": event_name,
            "p_limit": limit,
            "p_reservation_keys": keys,
            "p_user_id": user_id,
        },
    )
    if reserved == -1:
        raise HTTPException(status_code=429, detail="Daily provider capacity is reached; no provider request was sent")


def youcam_cache_key(*, source_scope: str, source_checksum: str, reference_checksum: str, garment_category: str) -> str:
    provider_category = YouCamClothesV3.provider_category(GarmentCategory(garment_category))
    canonical = json.dumps(
        {
            "source_scope": source_scope,
            "source_checksum": source_checksum,
            "reference_checksum": reference_checksum,
            "provider_garment_category": provider_category,
            "provider": "youcam-clothes-v3",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def ensure_youcam_request(
    repo: SupabaseRepository,
    *,
    photo: dict[str, Any],
    reference_path: str,
    reference_checksum: str,
    garment_category: str,
) -> tuple[dict[str, Any], bool]:
    cache_key = youcam_cache_key(
        source_scope=photo["storage_path"],
        source_checksum=photo["checksum"],
        reference_checksum=reference_checksum,
        garment_category=garment_category,
    )
    rows = await repo.upsert(
        "youcam_requests",
        {
            "cache_key": cache_key,
            "source_path": photo["storage_path"],
            "source_checksum": photo["checksum"],
            "reference_path": reference_path,
            "reference_checksum": reference_checksum,
            "garment_category": garment_category,
            "provider_garment_category": YouCamClothesV3.provider_category(GarmentCategory(garment_category)),
            "provider_state": "queued",
        },
        on_conflict="cache_key",
        ignore_duplicates=True,
    )
    if rows:
        return rows[0], True
    existing = await repo.one("youcam_requests", {"select": "*", "cache_key": f"eq.{cache_key}"})
    if not existing:
        raise HTTPException(status_code=503, detail="Could not establish the durable YouCam request")
    return existing, False


async def enqueue_youcam_request(repo: SupabaseRepository, request_row: dict[str, Any], *, priority: int) -> None:
    if request_row["provider_state"] != "queued":
        return
    existing = await repo.one(
        "jobs",
        {
            "select": "id,priority",
            "kind": "eq.youcam_request",
            "payload->>request_id": f"eq.{request_row['id']}",
            "status": "in.(queued,leased)",
        },
    )
    if existing:
        if int(existing.get("priority") or 0) < priority:
            await repo.update("jobs", {"id": f"eq.{existing['id']}"}, {"priority": priority, "available_at": datetime.now(UTC).isoformat()})
    else:
        try:
            await repo.insert("jobs", {"kind": "youcam_request", "payload": {"request_id": request_row["id"]}, "priority": priority, "status": "queued", "max_attempts": 60})
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 409:
                raise
            raced = await repo.one("jobs", {"select": "id,priority", "kind": "eq.youcam_request", "payload->>request_id": f"eq.{request_row['id']}", "status": "in.(queued,leased)"})
            if not raced:
                raise
            if int(raced.get("priority") or 0) < priority:
                await repo.update("jobs", {"id": f"eq.{raced['id']}"}, {"priority": priority, "available_at": datetime.now(UTC).isoformat()})


async def serialize_mirror_results(repo: SupabaseRepository, session_id: str) -> list[dict[str, Any]]:
    results = await repo.select(
        "mirror_results",
        {
            "select": "id,look_id,remix_option_id,youcam_request_id,normalized_constraints,created_at,youcam_requests(provider_state,attempts,next_poll_at,result_path,error,latency_ms)",
            "session_id": f"eq.{session_id}",
            "order": "created_at.asc",
        },
    )
    serialized = []
    for result in results:
        provider = result.pop("youcam_requests")
        item = {**result, **provider}
        if item.get("result_path"):
            item["result_url"] = await repo.signed_url(settings.supabase_result_bucket, item["result_path"], 600)
        serialized.append(item)
    return serialized


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "environment": settings.app_env,
        "providers": {
            "supabase": settings.supabase_configured,
            "gemini": settings.gemini_configured,
            "youcam": settings.youcam_configured,
        },
        "limits": {"campaign_bytes": settings.campaign_max_bytes, "campaign_seconds": settings.campaign_max_seconds},
    }


@app.get("/v1/providers/feasibility")
async def feasibility() -> dict[str, Any]:
    return {
        "gemini": {"configured": settings.gemini_configured, "model": settings.gemini_campaign_model, "required_real_run": True},
        "youcam": {"configured": settings.youcam_configured, "api": "AI Clothes v3", "required_real_run": True},
        "supabase": {"configured": settings.supabase_configured, "required_for_persistence": True},
    }


@app.get("/v1/discover")
async def discover_campaign() -> dict[str, Any]:
    repo = repository()
    campaigns = await repo.select(
        "campaigns",
        {"select": "id,brand_id,name,duration_seconds,source_path,playback_path,published_manifest_id", "status": "eq.published", "order": "updated_at.desc", "limit": "20"},
    )
    for campaign in campaigns:
        brand = await repo.one("brands", {"select": "name,slug,is_public", "id": f"eq.{campaign['brand_id']}", "is_public": "eq.true"})
        if not brand or not campaign.get("published_manifest_id") or not campaign.get("source_path"):
            continue
        manifest = await repo.one("campaign_manifests", {"select": "content", "id": f"eq.{campaign['published_manifest_id']}", "published": "eq.true"})
        if not manifest:
            continue
        hero = next((look for look in manifest["content"].get("looks", []) if look.get("is_hero")), None)
        poster_url = await repo.signed_url(settings.supabase_frame_bucket, hero["poster_path"], 900) if hero and hero.get("poster_path") else None
        return {
            "brand": {"name": brand["name"], "slug": brand["slug"]},
            "campaign": {
                "id": campaign["id"],
                "name": campaign["name"],
                "manifest_id": campaign["published_manifest_id"],
                "duration_seconds": campaign["duration_seconds"],
                "video_url": await repo.signed_url(settings.supabase_campaign_bucket, campaign.get("playback_path") or campaign["source_path"], 900),
                "poster_url": poster_url,
            },
        }
    raise HTTPException(status_code=404, detail="No public campaign is published yet")


@app.get("/v1/me")
async def current_user(user_id: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    repo = repository()
    membership = await repo.one("brand_members", {"select": "brand_id,role", "user_id": f"eq.{user_id}"})
    brand = await repo.one("brands", {"select": "name,slug", "id": f"eq.{membership['brand_id']}"}) if membership else None
    return {
        "user_id": user_id,
        "brand_id": membership.get("brand_id") if membership else None,
        "brand_role": membership.get("role") if membership else None,
        "brand_name": brand.get("name") if brand else None,
        "brand_slug": brand.get("slug") if brand else None,
    }


@app.post("/v1/campaigns/preprocess", status_code=status.HTTP_202_ACCEPTED)
async def preprocess_campaign(
    file: Annotated[UploadFile, File()],
    user_id: Annotated[str, Depends(require_user)],
    campaign_name: Annotated[str, Form()] = "Untitled campaign",
) -> dict[str, Any]:
    if file.content_type not in VIDEO_TYPES:
        raise HTTPException(status_code=415, detail="Use MP4/H.264 when possible; MOV, WebM and MKV are also accepted for preprocessing")
    content = await read_limited(file, settings.campaign_max_bytes)
    checksum = hashlib.sha256(content).hexdigest()
    repo = repository()
    brand_id = await repo.user_brand_id(user_id)
    if not brand_id:
        raise HTTPException(status_code=403, detail="This account is not assigned to a brand")
    campaign_id = str(uuid4())
    extension = Path(file.filename or "campaign.mp4").suffix.lower() or ".mp4"
    object_path = f"{brand_id}/{campaign_id}/source{extension}"
    await repo.upload(settings.supabase_campaign_bucket, object_path, content, file.content_type or "video/mp4")
    campaign = {
        "id": campaign_id,
        "brand_id": brand_id,
        "name": campaign_name.strip()[:120] or "Untitled campaign",
        "status": "preprocessing",
        "source_path": object_path,
        "source_mime": file.content_type,
        "source_bytes": len(content),
        "source_checksum": checksum,
        "input_version": 1,
    }
    await repo.insert("campaigns", campaign)
    await repo.insert("jobs", {"kind": "media_preprocess", "payload": {"campaign_id": campaign_id}, "priority": 100, "status": "queued"})
    return {"campaign_id": campaign_id, "status": "preprocessing", "checksum": checksum}


@app.post("/v1/campaigns/{campaign_id}/references", status_code=status.HTTP_201_CREATED)
async def add_campaign_reference(
    campaign_id: UUID,
    file: Annotated[UploadFile, File()],
    user_id: Annotated[str, Depends(require_user)],
    product_name: Annotated[str, Form()],
    garment_category: Annotated[GarmentCategory, Form()] = GarmentCategory.AUTO,
) -> dict[str, Any]:
    if file.content_type not in IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="YouCam references must be JPG or PNG")
    if not product_name.strip():
        raise HTTPException(status_code=422, detail="Product name is required")
    content = await read_limited(file, settings.youcam_image_max_bytes)
    try:
        image = Image.open(io.BytesIO(content))
        image.verify()
        width, height = Image.open(io.BytesIO(content)).size
    except (UnidentifiedImageError, OSError) as error:
        raise HTTPException(status_code=422, detail="The reference is not a valid image") from error
    if min(width, height) < 384 or max(width, height) < 512 or max(width, height) > 4096:
        raise HTTPException(status_code=422, detail="Reference dimensions must be at least 512×384 and no side may exceed 4096 px")
    checksum = hashlib.sha256(content).hexdigest()
    repo = repository()
    campaign = await repo.one("campaigns", {"select": "id,brand_id,input_version,status", "id": f"eq.{campaign_id}"})
    if not campaign or await repo.user_brand_id(user_id) != campaign["brand_id"]:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign["status"] == "published":
        raise HTTPException(status_code=409, detail="Published campaign inputs are immutable; create a new campaign version")
    asset_id = str(uuid4())
    extension = ".png" if file.content_type == "image/png" else ".jpg"
    object_path = f"{campaign['brand_id']}/{campaign_id}/{asset_id}{extension}"
    await repo.upload(settings.supabase_reference_bucket, object_path, content, file.content_type)
    prior_validation = await repo.one(
        "look_reference_assets",
        {"select": "validation_state,validation_result", "brand_id": f"eq.{campaign['brand_id']}", "checksum": f"eq.{checksum}", "order": "created_at.desc"},
    )
    prior_result = (prior_validation or {}).get("validation_result") or {}
    reusable_validation = prior_result if prior_result.get("kind") == "garment_reference" and prior_result.get("garment_category") == garment_category.value else None
    validation: dict[str, Any] = reusable_validation or {"valid": True, "reason": "Deterministic file validation passed; semantic validation disabled."}
    if settings.image_validation_enabled and not reusable_validation:
        try:
            reference_url = await repo.signed_url(settings.supabase_reference_bucket, object_path, 900)
            validation = await GeminiInteractions(settings).validate_image(
                image_url=reference_url,
                kind="garment_reference",
                garment_category=garment_category.value,
                image_mime_type=file.content_type,
            )
        except GeminiError as error:
            await repo.remove(settings.supabase_reference_bucket, [object_path])
            raise HTTPException(status_code=503, detail="Reference validation is temporarily unavailable; no YouCam unit was spent") from error
    if not validation.get("valid"):
        await repo.remove(settings.supabase_reference_bucket, [object_path])
        raise HTTPException(status_code=422, detail=f"Reference rejected before YouCam: {validation.get('reason', 'Image is not suitable')}")
    validation = {**validation, "kind": "garment_reference", "garment_category": garment_category.value}
    product_rows = await repo.insert("products", {"brand_id": campaign["brand_id"], "name": product_name.strip()[:140], "metadata": {"garment_category": garment_category.value}})
    product_id = product_rows[0]["id"]
    await repo.insert("campaign_products", {"campaign_id": str(campaign_id), "product_id": product_id})
    await repo.insert("look_reference_assets", {
        "id": asset_id,
        "brand_id": campaign["brand_id"],
        "product_id": product_id,
        "storage_path": object_path,
        "checksum": checksum,
        "mime_type": file.content_type,
        "width": width,
        "height": height,
        "validation_state": "validated",
        "validation_result": validation,
    })
    await repo.update("campaigns", {"id": f"eq.{campaign_id}"}, {"input_version": int(campaign["input_version"]) + 1})
    return {"product_id": product_id, "reference_asset_id": asset_id, "garment_category": garment_category.value}


@app.post("/v1/campaigns/{campaign_id}/analyze", status_code=status.HTTP_202_ACCEPTED)
async def queue_campaign_analysis(
    campaign_id: UUID,
    request: CampaignDirectionRequest,
    user_id: Annotated[str, Depends(require_user)],
) -> dict[str, Any]:
    repo = repository()
    campaign = await repo.one("campaigns", {"select": "id,brand_id,input_version,brand_direction,status,source_checksum", "id": f"eq.{campaign_id}"})
    if not campaign or await repo.user_brand_id(user_id) != campaign["brand_id"]:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign["status"] == "published":
        raise HTTPException(status_code=409, detail="Published campaigns are immutable; create a new campaign version")
    existing = await repo.one("jobs", {"select": "id,status", "kind": "eq.gemini_campaign_analysis", "payload->>campaign_id": f"eq.{campaign_id}", "status": "in.(queued,leased)"})
    if existing:
        return {"campaign_id": str(campaign_id), "status": "analyzing", "job_id": existing["id"], "deduplicated": True}
    direction = request.brand_direction.strip()
    direction_changed = direction != (campaign.get("brand_direction") or "").strip()
    input_version = int(campaign["input_version"]) + (1 if direction_changed else 0)
    key = campaign_cache_key(campaign_scope=str(campaign_id), video_checksum=campaign["source_checksum"], input_version=input_version, model=settings.gemini_campaign_model)
    attempts = await repo.select("campaign_analyses", {"select": "id,status", "cache_key": f"eq.{key}", "order": "attempt_index.asc"})
    if attempts and not any(item["status"] == "success" for item in attempts) and not request.force_reanalysis:
        raise HTTPException(status_code=409, detail="The first Gemini attempt failed; a deliberate force_reanalysis is required")
    if request.force_reanalysis and len(attempts) >= 2:
        raise HTTPException(status_code=409, detail="The one deliberate Gemini re-analysis has already been used")
    if request.force_reanalysis or not any(item["status"] == "success" for item in attempts):
        await reserve_daily_capacity(
            repo,
            event_name="gemini_campaign_reserved",
            limit=settings.gemini_campaign_daily_limit,
            reservation_keys=[f"{key}:attempt:{len(attempts)}"],
        )
    await repo.update("campaigns", {"id": f"eq.{campaign_id}"}, {"brand_direction": direction, "input_version": input_version, "status": "analyzing"})
    try:
        jobs = await repo.insert("jobs", {
            "kind": "gemini_campaign_analysis",
            "payload": {"campaign_id": str(campaign_id), "force_reanalysis": request.force_reanalysis},
            "priority": 95,
            "status": "queued",
            "max_attempts": 1,
        })
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 409:
            raise
        raced = await repo.one("jobs", {"select": "id,status", "kind": "eq.gemini_campaign_analysis", "payload->>campaign_id": f"eq.{campaign_id}", "status": "in.(queued,leased)"})
        if not raced:
            raise
        return {"campaign_id": str(campaign_id), "status": "analyzing", "job_id": raced["id"], "deduplicated": True}
    return {"campaign_id": str(campaign_id), "status": "analyzing", "job_id": jobs[0]["id"], "deduplicated": False}


@app.get("/v1/campaigns/{campaign_id}")
async def get_campaign(campaign_id: UUID, user_id: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    repo = repository()
    campaign = await repo.one("campaigns", {"select": "id,brand_id,name,status,source_path,playback_path,duration_seconds,deterministic_metadata,processing_error", "id": f"eq.{campaign_id}"})
    if not campaign or await repo.user_brand_id(user_id) != campaign["brand_id"]:
        raise HTTPException(status_code=404, detail="Campaign not found")
    looks = await repo.select("campaign_looks", {"select": "id,label,description,garment_category,is_hero,remix_allowed,confidence,sort_order,product_id,reference_asset_id,poster_path", "campaign_id": f"eq.{campaign_id}", "order": "sort_order.asc"})
    segments = await repo.select("campaign_segments", {"select": "id,look_id,start_seconds,end_seconds,sort_order", "campaign_id": f"eq.{campaign_id}", "order": "sort_order.asc"})
    remix_options = await repo.select("look_remix_options", {"select": "id,look_id,label,reference_asset_id,garment_category,constraints,sort_order", "look_id": f"in.({','.join(look['id'] for look in looks)})", "approved": "eq.true", "order": "sort_order.asc"}) if looks else []
    for look in looks:
        look["segments"] = [segment for segment in segments if segment["look_id"] == look["id"]]
        look["remix_options"] = [option for option in remix_options if option["look_id"] == look["id"]]
        if look.get("poster_path"):
            look["poster_url"] = await repo.signed_url(settings.supabase_frame_bucket, look["poster_path"], 600)
    links = await repo.select("campaign_products", {"select": "product_id", "campaign_id": f"eq.{campaign_id}", "order": "sort_order.asc"})
    product_ids = [str(link["product_id"]) for link in links]
    products = await repo.select("products", {"select": "id,name,sku,metadata", "id": f"in.({','.join(product_ids)})"}) if product_ids else []
    references = await repo.select("look_reference_assets", {"select": "id,product_id,validation_state", "product_id": f"in.({','.join(product_ids)})", "validation_state": "eq.validated"}) if product_ids else []
    reference_by_product = {str(reference["product_id"]): reference for reference in references}
    for product in products:
        reference = reference_by_product.get(str(product["id"]))
        product["reference_asset_id"] = reference["id"] if reference else None
    if campaign.get("playback_path") or campaign.get("source_path"):
        campaign["source_url"] = await repo.signed_url(settings.supabase_campaign_bucket, campaign.get("playback_path") or campaign["source_path"], 600)
    return {**campaign, "looks": looks, "products": products}


@app.get("/v1/manifests/{manifest_id}")
async def get_published_manifest(manifest_id: UUID, _: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    repo = repository()
    manifest = await repo.one("campaign_manifests", {"select": "id,version,content,published_at", "id": f"eq.{manifest_id}", "published": "eq.true"})
    if not manifest:
        raise HTTPException(status_code=404, detail="Published campaign not found")
    content = manifest["content"]
    campaign = content["campaign"]
    brand = content.get("brand") or {"name": "Brand", "slug": None}
    video_url = await repo.signed_url(settings.supabase_campaign_bucket, campaign["video_path"], 900)
    public_looks = []
    for look in content.get("looks", []):
        item = {
            "id": look["id"],
            "label": look["label"],
            "description": look.get("description"),
            "is_hero": look.get("is_hero", False),
            "remix_allowed": look.get("remix_allowed", False),
            "sort_order": look.get("sort_order", 0),
            "segments": look.get("segments", []),
            "remix_options": [{"id": option["id"], "label": option["label"]} for option in look.get("remix_options", [])],
        }
        if look.get("poster_path"):
            item["poster_url"] = await repo.signed_url(settings.supabase_frame_bucket, look["poster_path"], 900)
        public_looks.append(item)
    return {
        "id": manifest["id"],
        "version": manifest["version"],
        "published_at": manifest["published_at"],
        "brand": brand,
        "campaign": {"id": campaign["id"], "name": campaign["name"], "duration_seconds": campaign["duration_seconds"], "video_url": video_url},
        "looks": public_looks,
    }


@app.post("/v1/campaigns/{campaign_id}/publish", status_code=status.HTTP_201_CREATED)
async def publish_campaign(campaign_id: UUID, user_id: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    import json

    repo = repository()
    campaign = await repo.one("campaigns", {"select": "id,brand_id,name,status,source_path,playback_path,source_checksum,duration_seconds,published_manifest_id", "id": f"eq.{campaign_id}"})
    if not campaign or await repo.user_brand_id(user_id) != campaign["brand_id"]:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign["status"] == "published" and campaign.get("published_manifest_id"):
        existing = await repo.one("campaign_manifests", {"select": "id,version,content_checksum", "id": f"eq.{campaign['published_manifest_id']}"})
        if existing:
            return {"manifest_id": existing["id"], "version": existing["version"], "content_checksum": existing["content_checksum"], "idempotent": True}
    brand = await repo.one("brands", {"select": "name,slug", "id": f"eq.{campaign['brand_id']}"})
    looks = await repo.select("campaign_looks", {"select": "id,label,description,garment_category,is_hero,remix_allowed,sort_order,product_id,reference_asset_id,poster_path,look_reference_assets(storage_path,checksum)", "campaign_id": f"eq.{campaign_id}", "order": "sort_order.asc"})
    if not looks or any(not look.get("product_id") or not look.get("reference_asset_id") for look in looks):
        raise HTTPException(status_code=422, detail="Every look must have a confirmed product and YouCam-ready reference before publishing")
    if sum(1 for look in looks if look.get("is_hero")) != 1:
        raise HTTPException(status_code=422, detail="Confirm exactly one hero look before publishing")
    segments = await repo.select("campaign_segments", {"select": "look_id,start_seconds,end_seconds,sort_order", "campaign_id": f"eq.{campaign_id}", "order": "sort_order.asc"})
    if any(not any(segment["look_id"] == look["id"] for segment in segments) for look in looks):
        raise HTTPException(status_code=422, detail="Every unique look needs at least one confirmed campaign segment")
    ordered_segments = sorted(segments, key=lambda segment: float(segment["start_seconds"]))
    duration = float(campaign["duration_seconds"] or 0)
    for index, segment in enumerate(ordered_segments):
        start = float(segment["start_seconds"])
        end = float(segment["end_seconds"])
        if start < 0 or end <= start or end > duration + 0.05:
            raise HTTPException(status_code=422, detail="Campaign segments must be ordered and stay within the video duration")
        if index and start < float(ordered_segments[index - 1]["end_seconds"]) - 0.01:
            raise HTTPException(status_code=422, detail="Campaign segments cannot overlap; resolve the look boundary before publishing")
    remix_options = await repo.select("look_remix_options", {"select": "id,look_id,label,reference_path,garment_category,constraints,sort_order", "approved": "eq.true", "look_id": f"in.({','.join(look['id'] for look in looks)})", "order": "sort_order.asc"})
    manifest_looks = []
    for look in looks:
        reference = look.pop("look_reference_assets")
        manifest_looks.append({
            **look,
            "reference_path": reference["storage_path"],
            "reference_checksum": reference["checksum"],
            "segments": [segment for segment in segments if segment["look_id"] == look["id"]],
            "remix_options": [option for option in remix_options if option["look_id"] == look["id"]],
        })
    content = {
        "brand": {"name": (brand or {}).get("name") or "Brand", "slug": (brand or {}).get("slug")},
        "campaign": {"id": str(campaign_id), "name": campaign["name"], "video_path": campaign.get("playback_path") or campaign["source_path"], "video_checksum": campaign["source_checksum"], "duration_seconds": campaign["duration_seconds"]},
        "looks": manifest_looks,
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    checksum = hashlib.sha256(canonical.encode()).hexdigest()
    prior = await repo.select("campaign_manifests", {"select": "version", "campaign_id": f"eq.{campaign_id}", "order": "version.desc", "limit": "1"})
    version = int(prior[0]["version"]) + 1 if prior else 1
    try:
        rows = await repo.insert("campaign_manifests", {"campaign_id": str(campaign_id), "version": version, "content": content, "content_checksum": checksum, "published": True, "published_at": datetime.now(UTC).isoformat(), "created_by": user_id})
        manifest_id = rows[0]["id"]
        idempotent = False
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 409:
            raise
        raced = await repo.one("campaign_manifests", {"select": "id,version", "campaign_id": f"eq.{campaign_id}", "content_checksum": f"eq.{checksum}", "published": "eq.true"})
        if not raced:
            raise
        manifest_id = raced["id"]
        version = int(raced["version"])
        idempotent = True
    await repo.update("campaigns", {"id": f"eq.{campaign_id}"}, {"status": "published", "published_manifest_id": manifest_id})
    return {"manifest_id": manifest_id, "version": version, "content_checksum": checksum, "idempotent": idempotent}


@app.patch("/v1/campaigns/{campaign_id}/looks/{look_id}")
async def confirm_campaign_look(campaign_id: UUID, look_id: UUID, request: LookReviewRequest, user_id: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    repo = repository()
    campaign = await repo.one("campaigns", {"select": "id,brand_id,duration_seconds,status", "id": f"eq.{campaign_id}"})
    if not campaign or await repo.user_brand_id(user_id) != campaign["brand_id"]:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign["status"] == "published":
        raise HTTPException(status_code=409, detail="Published campaign manifests are immutable")
    product = await repo.one("campaign_products", {"select": "product_id", "campaign_id": f"eq.{campaign_id}", "product_id": f"eq.{request.product_id}"})
    reference = await repo.one("look_reference_assets", {"select": "id,product_id", "id": f"eq.{request.reference_asset_id}", "brand_id": f"eq.{campaign['brand_id']}", "validation_state": "eq.validated"})
    if not product or not reference or reference["product_id"] != str(request.product_id):
        raise HTTPException(status_code=422, detail="Choose a validated campaign product and its matching YouCam reference")
    duration = float(campaign["duration_seconds"] or 0)
    if any(segment.end_seconds > duration + 0.05 for segment in request.segments):
        raise HTTPException(status_code=422, detail="Look boundaries must stay within the campaign duration")
    requested_segments = sorted(request.segments, key=lambda segment: segment.start_seconds)
    for index, segment in enumerate(requested_segments):
        if index and segment.start_seconds < requested_segments[index - 1].end_seconds - 0.01:
            raise HTTPException(status_code=422, detail="Segments for one look cannot overlap")
    other_segments = await repo.select("campaign_segments", {"select": "look_id,start_seconds,end_seconds", "campaign_id": f"eq.{campaign_id}", "look_id": f"neq.{look_id}"})
    for segment in requested_segments:
        if any(segment.start_seconds < float(other["end_seconds"]) - 0.01 and segment.end_seconds > float(other["start_seconds"]) + 0.01 for other in other_segments):
            raise HTTPException(status_code=422, detail="A segment cannot belong to two unique looks; adjust the boundary or merge the outfit identity")
    remix_rows = []
    if request.remix_allowed:
        for index, option in enumerate(request.remix_options):
            if option.reference_asset_id == request.reference_asset_id:
                raise HTTPException(status_code=422, detail="A remix alternative must use a different approved reference")
            alternative = await repo.one("look_reference_assets", {"select": "id,product_id,storage_path", "id": f"eq.{option.reference_asset_id}", "brand_id": f"eq.{campaign['brand_id']}", "validation_state": "eq.validated"})
            if not alternative:
                raise HTTPException(status_code=422, detail="Choose a validated brand reference for every remix alternative")
            linked = await repo.one("campaign_products", {"select": "product_id", "campaign_id": f"eq.{campaign_id}", "product_id": f"eq.{alternative['product_id']}"})
            if not linked:
                raise HTTPException(status_code=422, detail="Remix references must belong to a product in this campaign")
            remix_rows.append({
                "look_id": str(look_id),
                "label": option.label,
                "reference_asset_id": str(option.reference_asset_id),
                "reference_path": alternative["storage_path"],
                "garment_category": option.garment_category.value,
                "constraints": {"allowed_tags": [tag.strip()[:60] for tag in option.allowed_tags if tag.strip()]},
                "approved": True,
                "sort_order": index,
            })
    if request.is_hero:
        await repo.update("campaign_looks", {"campaign_id": f"eq.{campaign_id}"}, {"is_hero": False})
    rows = await repo.update("campaign_looks", {"id": f"eq.{look_id}", "campaign_id": f"eq.{campaign_id}"}, {
        "product_id": str(request.product_id),
        "reference_asset_id": str(request.reference_asset_id),
        "garment_category": request.garment_category.value,
        "is_hero": request.is_hero,
        "remix_allowed": request.remix_allowed,
    })
    if not rows:
        raise HTTPException(status_code=404, detail="Look not found")
    rebuilt_segments = [
        {"look_id": str(other["look_id"]), "start_seconds": float(other["start_seconds"]), "end_seconds": float(other["end_seconds"])}
        for other in other_segments
    ] + [
        {"look_id": str(look_id), "start_seconds": segment.start_seconds, "end_seconds": segment.end_seconds}
        for segment in requested_segments
    ]
    rebuilt_segments.sort(key=lambda segment: segment["start_seconds"])
    await repo.delete("campaign_segments", {"campaign_id": f"eq.{campaign_id}"})
    await repo.insert("campaign_segments", [{"campaign_id": str(campaign_id), **segment, "sort_order": index} for index, segment in enumerate(rebuilt_segments)])
    await repo.delete("look_remix_options", {"look_id": f"eq.{look_id}"})
    if remix_rows:
        await repo.insert("look_remix_options", remix_rows)
    return {"id": str(look_id), "status": "confirmed", "remix_options": len(remix_rows)}


@app.post("/v1/providers/feasibility/gemini")
async def run_gemini_feasibility(
    request: CampaignAnalyzeRequest,
    user_id: Annotated[str, Depends(require_user)],
) -> dict[str, Any]:
    repo = repository()
    brand_id = await repo.user_brand_id(user_id)
    if not brand_id:
        raise HTTPException(status_code=403, detail="A brand membership is required for provider feasibility runs")
    campaign = await repo.one("campaigns", {"select": "id,source_checksum,input_version", "id": f"eq.{request.campaign_id}", "brand_id": f"eq.{brand_id}"})
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.get("source_checksum") != request.video_checksum or int(campaign.get("input_version") or 0) != request.campaign_input_version:
        raise HTTPException(status_code=422, detail="Feasibility inputs must match the current owned campaign version")
    key = campaign_cache_key(campaign_scope=str(request.campaign_id), video_checksum=request.video_checksum, input_version=request.campaign_input_version, model=settings.gemini_campaign_model)
    cached = await repo.one("campaign_analyses", {"select": "*", "cache_key": f"eq.{key}", "status": "eq.success"})
    if cached and not request.force_reanalysis:
        return {"cached": True, "analysis_id": cached["id"], "analysis": cached["result"]}
    previous = await repo.select("campaign_analyses", {"select": "id,status", "cache_key": f"eq.{key}"})
    if previous and not any(item["status"] == "success" for item in previous) and not request.force_reanalysis:
        raise HTTPException(status_code=409, detail="The first Gemini attempt failed; a deliberate force_reanalysis is required")
    if request.force_reanalysis:
        if len(previous) >= 2:
            raise HTTPException(status_code=409, detail="The one deliberate Gemini re-analysis has already been used")
    await reserve_daily_capacity(
        repo,
        event_name="gemini_campaign_reserved",
        limit=settings.gemini_campaign_daily_limit,
        reservation_keys=[f"{key}:attempt:{len(previous)}"],
    )
    analyzer = GeminiInteractions(settings)
    started = time.monotonic()
    try:
        analysis, raw = await analyzer.analyze_campaign(
            video_url=str(request.video_url),
            products=request.products,
            brand_direction=request.brand_direction,
            timing_candidates=request.timing_candidates,
        )
    except GeminiError as error:
        await repo.insert("campaign_analyses", {
            "campaign_id": str(request.campaign_id),
            "cache_key": key,
            "model": settings.gemini_campaign_model,
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
            "result": None,
            "attempt_index": len(previous),
        })
        raise HTTPException(status_code=502, detail="Gemini campaign analysis failed; use one deliberate re-analysis only after correcting the cause") from error
    elapsed_ms = round((time.monotonic() - started) * 1000)
    rows = await repo.insert("campaign_analyses", {
        "campaign_id": str(request.campaign_id),
        "cache_key": key,
        "model": settings.gemini_campaign_model,
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "result": analysis,
        "latency_ms": elapsed_ms,
        "provider_interaction_id": raw.get("id"),
        "attempt_index": len(previous),
    })
    return {"cached": False, "analysis_id": rows[0]["id"], "latency_ms": elapsed_ms, "analysis": analysis}


@app.post("/v1/providers/feasibility/youcam", status_code=status.HTTP_202_ACCEPTED)
async def run_youcam_feasibility(
    request: YouCamTaskRequest,
    user_id: Annotated[str, Depends(require_user)],
) -> dict[str, Any]:
    repo = repository()
    if not await repo.user_brand_id(user_id):
        raise HTTPException(status_code=403, detail="A brand membership is required for provider feasibility runs")
    await reserve_daily_capacity(
        repo,
        event_name="youcam_user_reserved",
        limit=settings.youcam_daily_user_limit,
        reservation_keys=[f"feasibility:{uuid4()}"],
        user_id=user_id,
    )
    provider = YouCamClothesV3(settings)
    started_at = datetime.now(UTC)
    task_id = await provider.create_task(
        source_url=str(request.source_url),
        reference_url=str(request.reference_url),
        garment_category=request.garment_category,
    )
    next_poll_at = started_at + timedelta(seconds=3)
    rows = await repo.insert("provider_feasibility_runs", {
        "provider": "youcam_clothes_v3",
        "created_by": user_id,
        "provider_task_id": task_id,
        "provider_state": "processing",
        "attempts": 0,
        "next_poll_at": next_poll_at.isoformat(),
        "request_metadata": {"garment_category": request.garment_category.value, "provider_garment_category": YouCamClothesV3.provider_category(request.garment_category)},
    })
    run_id = rows[0]["id"]
    await repo.insert("jobs", {"kind": "feasibility_youcam_poll", "payload": {"run_id": run_id}, "priority": 100, "status": "queued", "available_at": next_poll_at.isoformat(), "max_attempts": 60})
    return {"run_id": run_id, "provider_task_id": task_id, "state": "processing", "next_poll_at": next_poll_at}


@app.get("/v1/providers/feasibility/youcam/{run_id}")
async def get_youcam_feasibility(run_id: UUID, user_id: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    repo = repository()
    run = await repo.one("provider_feasibility_runs", {"select": "id,provider,provider_state,attempts,next_poll_at,latency_ms,result_path,error", "id": f"eq.{run_id}", "created_by": f"eq.{user_id}"})
    if not run:
        raise HTTPException(status_code=404, detail="Feasibility run not found")
    if run.get("result_path"):
        run["result_url"] = await repo.signed_url(settings.supabase_result_bucket, run["result_path"], 600)
    return run


@app.post("/v1/shopper/photos", status_code=status.HTTP_201_CREATED)
async def upload_shopper_photo(
    file: Annotated[UploadFile, File()],
    user_id: Annotated[str, Depends(require_user)],
) -> dict[str, Any]:
    if file.content_type not in IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Use a JPG or PNG photo")
    content = await read_limited(file, settings.youcam_image_max_bytes)
    try:
        image = Image.open(io.BytesIO(content))
        image.verify()
        width, height = Image.open(io.BytesIO(content)).size
    except (UnidentifiedImageError, OSError) as error:
        raise HTTPException(status_code=422, detail="The uploaded file is not a valid image") from error
    if min(width, height) < 384 or max(width, height) < 512 or max(width, height) > 4096:
        raise HTTPException(status_code=422, detail="Photo dimensions must be at least 512×384 and no side may exceed 4096 px")
    checksum = hashlib.sha256(content).hexdigest()
    repo = repository()
    existing = await repo.one("shopper_photos", {"select": "id,status,width,height", "user_id": f"eq.{user_id}", "checksum": f"eq.{checksum}", "status": "eq.validated"})
    if existing:
        return {"id": existing["id"], "status": existing["status"], "width": existing["width"], "height": existing["height"], "deduplicated": True}
    photo_id = str(uuid4())
    extension = ".png" if file.content_type == "image/png" else ".jpg"
    path = f"{user_id}/{photo_id}/source{extension}"
    await repo.upload(settings.supabase_private_bucket, path, content, file.content_type)
    validation: dict[str, Any] = {"valid": True, "reason": "Deterministic file validation passed; semantic validation disabled."}
    if settings.image_validation_enabled:
        try:
            source_url = await repo.signed_url(settings.supabase_private_bucket, path, 900)
            validation = await GeminiInteractions(settings).validate_image(image_url=source_url, kind="shopper_source", image_mime_type=file.content_type)
        except GeminiError as error:
            await repo.remove(settings.supabase_private_bucket, [path])
            raise HTTPException(status_code=503, detail="Photo validation is temporarily unavailable; no YouCam unit was spent") from error
    photo_status = "validated" if validation.get("valid") else "rejected"
    if photo_status == "rejected":
        await repo.remove(settings.supabase_private_bucket, [path])
        raise HTTPException(status_code=422, detail=f"Photo rejected before YouCam: {validation.get('reason', 'Image is not suitable')}")
    await repo.insert("shopper_photos", {"id": photo_id, "user_id": user_id, "storage_path": path, "checksum": checksum, "width": width, "height": height, "status": photo_status, "validation_result": validation})
    return {"id": photo_id, "status": "validated", "width": width, "height": height, "deduplicated": False}


@app.get("/v1/mirror-sessions")
async def list_mirror_sessions(user_id: Annotated[str, Depends(require_user)], saved: bool = True) -> dict[str, Any]:
    repo = repository()
    filters = {"select": "id,status,manifest_id,saved,created_at,updated_at", "user_id": f"eq.{user_id}", "order": "updated_at.desc", "limit": "50"}
    if saved:
        filters["saved"] = "eq.true"
    sessions = await repo.select("mirror_sessions", filters)
    for session in sessions:
        manifest = await repo.one("campaign_manifests", {"select": "content", "id": f"eq.{session['manifest_id']}"})
        campaign = (manifest or {}).get("content", {}).get("campaign", {})
        session["campaign"] = {"id": campaign.get("id"), "name": campaign.get("name")}
        session["results"] = await serialize_mirror_results(repo, session["id"])
    return {"sessions": sessions}


@app.post("/v1/mirror-sessions", status_code=status.HTTP_202_ACCEPTED)
async def create_mirror_session(request: MirrorSessionRequest, user_id: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    repo = repository()
    manifest = await repo.one("campaign_manifests", {"select": "id,version,content", "id": f"eq.{request.manifest_id}", "published": "eq.true"})
    photo = await repo.one("shopper_photos", {"select": "id,storage_path,checksum", "id": f"eq.{request.shopper_photo_id}", "user_id": f"eq.{user_id}", "status": "eq.validated"})
    if not manifest or not photo:
        raise HTTPException(status_code=404, detail="Published campaign or validated photo not found")
    looks = manifest["content"].get("looks", [])
    if not any(str(look.get("id")) == str(request.initial_look_id) for look in looks):
        raise HTTPException(status_code=422, detail="Initial look is not in this campaign")
    existing_session = await repo.one("mirror_sessions", {"select": "id,status", "user_id": f"eq.{user_id}", "manifest_id": f"eq.{request.manifest_id}", "shopper_photo_id": f"eq.{request.shopper_photo_id}"})
    if existing_session:
        return {"id": existing_session["id"], "status": existing_session["status"], "results": await serialize_mirror_results(repo, existing_session["id"]), "deduplicated": True}
    missing_request_keys = []
    for look in looks:
        cache_key = youcam_cache_key(
            source_scope=photo["storage_path"],
            source_checksum=photo["checksum"],
            reference_checksum=look["reference_checksum"],
            garment_category=look.get("garment_category") or "auto",
        )
        if not await repo.one("youcam_requests", {"select": "id", "cache_key": f"eq.{cache_key}"}):
            missing_request_keys.append(cache_key)
    await reserve_daily_capacity(
        repo,
        event_name="youcam_user_reserved",
        limit=settings.youcam_daily_user_limit,
        reservation_keys=missing_request_keys,
        user_id=user_id,
    )
    proposed_session_id = str(uuid4())
    session_rows = await repo.upsert(
        "mirror_sessions",
        {"id": proposed_session_id, "user_id": user_id, "manifest_id": str(request.manifest_id), "shopper_photo_id": str(request.shopper_photo_id), "status": "generating"},
        on_conflict="user_id,manifest_id,shopper_photo_id",
        ignore_duplicates=True,
    )
    if session_rows:
        session_id = proposed_session_id
    else:
        raced = await repo.one("mirror_sessions", {"select": "id,status", "user_id": f"eq.{user_id}", "manifest_id": f"eq.{request.manifest_id}", "shopper_photo_id": f"eq.{request.shopper_photo_id}"})
        if not raced:
            raise HTTPException(status_code=503, detail="Could not establish the mirror session")
        return {"id": raced["id"], "status": raced["status"], "results": await serialize_mirror_results(repo, raced["id"]), "deduplicated": True}
    initial_index = next(index for index, look in enumerate(looks) if str(look["id"]) == str(request.initial_look_id))
    associations = []
    queued_requests: list[tuple[dict[str, Any], int]] = []
    for index, look in enumerate(looks):
        provider_request, _ = await ensure_youcam_request(
            repo,
            photo=photo,
            reference_path=look["reference_path"],
            reference_checksum=look["reference_checksum"],
            garment_category=look.get("garment_category") or "auto",
        )
        associations.append({"session_id": session_id, "look_id": str(look["id"]), "youcam_request_id": provider_request["id"]})
        priority = 100 if index == initial_index else 90 if index == initial_index + 1 else 50
        queued_requests.append((provider_request, priority))
    await repo.insert("mirror_results", associations)
    for provider_request, priority in queued_requests:
        await enqueue_youcam_request(repo, provider_request, priority=priority)
    await repo.rpc("refresh_mirror_session_status", {"p_session_id": session_id})
    return {"id": session_id, "status": "generating", "results": await serialize_mirror_results(repo, session_id), "deduplicated": False}


@app.get("/v1/mirror-sessions/{session_id}")
async def get_mirror_session(session_id: UUID, user_id: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    repo = repository()
    session = await repo.one("mirror_sessions", {"select": "id,status,manifest_id,saved,created_at", "id": f"eq.{session_id}", "user_id": f"eq.{user_id}"})
    if not session:
        raise HTTPException(status_code=404, detail="Mirror session not found")
    return {**session, "results": await serialize_mirror_results(repo, str(session_id))}


@app.post("/v1/mirror-sessions/{session_id}/priority")
async def prioritize_look(session_id: UUID, request: PriorityRequest, user_id: Annotated[str, Depends(require_user)]) -> dict[str, str]:
    repo = repository()
    session = await repo.one("mirror_sessions", {"select": "id", "id": f"eq.{session_id}", "user_id": f"eq.{user_id}"})
    if not session:
        raise HTTPException(status_code=404, detail="Mirror session not found")
    result = await repo.one("mirror_results", {"select": "id", "session_id": f"eq.{session_id}", "look_id": f"eq.{request.look_id}", "remix_option_id": "is.null"})
    if not result:
        raise HTTPException(status_code=404, detail="Look result not found")
    await repo.rpc("prioritize_mirror_result", {"p_result_id": result["id"], "p_priority": 120})
    return {"status": "prioritized"}


@app.post("/v1/mirror-sessions/{session_id}/save")
async def save_mirror_session(session_id: UUID, request: SaveMirrorRequest, user_id: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    repo = repository()
    session = await repo.one("mirror_sessions", {"select": "id", "id": f"eq.{session_id}", "user_id": f"eq.{user_id}"})
    if not session:
        raise HTTPException(status_code=404, detail="Mirror session not found")
    await repo.update("mirror_sessions", {"id": f"eq.{session_id}"}, {"saved": request.saved})
    return {"id": str(session_id), "saved": request.saved}


@app.post("/v1/mirror-sessions/{session_id}/remix", status_code=status.HTTP_202_ACCEPTED)
async def create_remix(session_id: UUID, request: RemixRequest, user_id: Annotated[str, Depends(require_user)]) -> dict[str, Any]:
    repo = repository()
    session = await repo.one("mirror_sessions", {"select": "id,manifest_id,shopper_photo_id,shopper_photos(storage_path,checksum)", "id": f"eq.{session_id}", "user_id": f"eq.{user_id}"})
    if not session:
        raise HTTPException(status_code=404, detail="Mirror session not found")
    manifest = await repo.one("campaign_manifests", {"select": "content", "id": f"eq.{session['manifest_id']}", "published": "eq.true"})
    if not manifest or not any(str(look.get("id")) == str(request.look_id) for look in manifest["content"].get("looks", [])):
        raise HTTPException(status_code=422, detail="That look is not part of this mirror session")
    option = None
    if request.preset_id:
        option = await repo.one("look_remix_options", {"select": "id,look_id,reference_asset_id,reference_path,garment_category,constraints", "id": f"eq.{request.preset_id}", "look_id": f"eq.{request.look_id}", "approved": "eq.true"})
    if not option:
        raise HTTPException(status_code=422, detail="Choose a brand-approved remix preset")
    normalized: dict[str, Any] = {}
    if request.text_constraint:
        allowed_tags = [str(tag) for tag in option.get("constraints", {}).get("allowed_tags", [])]
        try:
            normalized = await GeminiInteractions(settings).parse_remix_constraint(request.text_constraint, allowed_tags)
        except GeminiError as error:
            raise HTTPException(status_code=503, detail="Remix constraint parsing is temporarily unavailable; no YouCam unit was spent") from error
        if normalized.get("rejected_freeform"):
            raise HTTPException(status_code=422, detail="That request falls outside this brand's approved remix options")
    reference = await repo.one("look_reference_assets", {"select": "checksum", "id": f"eq.{option['reference_asset_id']}", "validation_state": "eq.validated"})
    if not reference:
        raise HTTPException(status_code=422, detail="The approved remix reference is no longer available")
    remix_cache_key = youcam_cache_key(
        source_scope=session["shopper_photos"]["storage_path"],
        source_checksum=session["shopper_photos"]["checksum"],
        reference_checksum=reference["checksum"],
        garment_category=option["garment_category"],
    )
    if not await repo.one("youcam_requests", {"select": "id", "cache_key": f"eq.{remix_cache_key}"}):
        await reserve_daily_capacity(
            repo,
            event_name="youcam_user_reserved",
            limit=settings.youcam_daily_user_limit,
            reservation_keys=[remix_cache_key],
            user_id=user_id,
        )
    provider_request, _ = await ensure_youcam_request(
        repo,
        photo=session["shopper_photos"],
        reference_path=option["reference_path"],
        reference_checksum=reference["checksum"],
        garment_category=option["garment_category"],
    )
    existing_result = await repo.one("mirror_results", {
        "select": "id",
        "session_id": f"eq.{session_id}",
        "look_id": f"eq.{request.look_id}",
        "remix_option_id": f"eq.{option['id']}",
        "youcam_request_id": f"eq.{provider_request['id']}",
    })
    if existing_result:
        return {"result_id": existing_result["id"], "state": provider_request["provider_state"], "deduplicated": True}
    try:
        result_rows = await repo.insert("mirror_results", {
            "session_id": str(session_id),
            "look_id": str(request.look_id),
            "remix_option_id": option["id"],
            "youcam_request_id": provider_request["id"],
            "normalized_constraints": normalized,
        })
        result = result_rows[0]
    except httpx.HTTPStatusError as error:
        if error.response.status_code != 409:
            raise
        raced = await repo.one("mirror_results", {
            "select": "id",
            "session_id": f"eq.{session_id}",
            "look_id": f"eq.{request.look_id}",
            "remix_option_id": f"eq.{option['id']}",
            "youcam_request_id": f"eq.{provider_request['id']}",
        })
        if not raced:
            raise
        return {"result_id": raced["id"], "state": provider_request["provider_state"], "deduplicated": True}
    await enqueue_youcam_request(repo, provider_request, priority=90)
    return {"result_id": result["id"], "state": provider_request["provider_state"], "deduplicated": False}
