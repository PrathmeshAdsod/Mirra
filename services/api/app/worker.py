from __future__ import annotations

import argparse
import asyncio
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .config import get_settings
from .media import MediaValidationError, extract_jpeg_frame, normalize_playback_mp4, probe_video
from .models import GarmentCategory, ProviderState
from .providers.youcam import YouCamClothesV3, YouCamError
from .providers.gemini import GeminiError, GeminiInteractions, SCHEMA_VERSION, campaign_cache_key
from .repository import SupabaseRepository

settings = get_settings()


def backoff_seconds(attempts: int) -> int:
    return min(30, 3 + attempts * 2)


async def complete(repo: SupabaseRepository, job_id: str) -> None:
    await repo.rpc("finish_job", {"p_job_id": job_id})


async def reschedule(repo: SupabaseRepository, job_id: str, when: datetime, message: str | None = None) -> None:
    await repo.rpc("reschedule_job", {"p_job_id": job_id, "p_available_at": when.isoformat(), "p_last_error": message})


async def fail(repo: SupabaseRepository, job: dict[str, Any], message: str) -> None:
    await repo.rpc("fail_job", {"p_job_id": job["id"], "p_last_error": message[:1000]})


async def process_media(repo: SupabaseRepository, job: dict[str, Any]) -> None:
    campaign_id = job["payload"]["campaign_id"]
    campaign = await repo.one("campaigns", {"select": "id,source_path,source_mime", "id": f"eq.{campaign_id}"})
    if not campaign:
        raise RuntimeError("Campaign no longer exists")
    payload = await repo.download(settings.supabase_campaign_bucket, campaign["source_path"])
    suffix = Path(campaign["source_path"]).suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(payload)
        path = Path(handle.name)
    try:
        probe = await probe_video(path, settings.campaign_max_seconds)
        metadata = probe.as_dict()
        playback_path = campaign["source_path"]
        normalized_path: Path | None = None
        if campaign.get("source_mime") != "video/mp4" or probe.video_codec != "h264":
            normalized_path = path.with_suffix(".playback.mp4")
            await normalize_playback_mp4(path, normalized_path)
            playback_path = f"{Path(campaign['source_path']).parent.as_posix()}/playback.mp4"
            await repo.upload(settings.supabase_campaign_bucket, playback_path, normalized_path.read_bytes(), "video/mp4", upsert=True)
            metadata["normalized_for_playback"] = True
        else:
            metadata["normalized_for_playback"] = False
        await repo.update("campaigns", {"id": f"eq.{campaign_id}"}, {"status": "inputs_pending", "deterministic_metadata": metadata, "duration_seconds": probe.duration_seconds, "playback_path": playback_path, "playback_mime": "video/mp4"})
        await complete(repo, job["id"])
    except MediaValidationError as error:
        await repo.update("campaigns", {"id": f"eq.{campaign_id}"}, {"status": "failed", "processing_error": str(error)})
        await fail(repo, job, str(error))
    finally:
        path.unlink(missing_ok=True)
        if "normalized_path" in locals() and normalized_path:
            normalized_path.unlink(missing_ok=True)


async def poll_feasibility(repo: SupabaseRepository, job: dict[str, Any]) -> None:
    run_id = job["payload"]["run_id"]
    run = await repo.one("provider_feasibility_runs", {"select": "*", "id": f"eq.{run_id}"})
    if not run:
        raise RuntimeError("Feasibility run no longer exists")
    provider = YouCamClothesV3(settings)
    attempts = int(run.get("attempts") or 0) + 1
    try:
        result = await provider.poll(run["provider_task_id"])
    except YouCamError as error:
        if not error.retryable or attempts >= int(job.get("max_attempts") or 60):
            await repo.update("provider_feasibility_runs", {"id": f"eq.{run_id}"}, {"provider_state": "failed", "attempts": attempts, "next_poll_at": None, "error": {"message": str(error), "http_status": error.status_code}})
            await fail(repo, job, str(error))
            return
        when = datetime.now(UTC) + timedelta(seconds=backoff_seconds(attempts))
        await repo.update("provider_feasibility_runs", {"id": f"eq.{run_id}"}, {"provider_state": "processing", "attempts": attempts, "next_poll_at": when.isoformat(), "error": {"message": str(error), "http_status": error.status_code}})
        await reschedule(repo, job["id"], when, str(error))
        return
    if result.state == ProviderState.PROCESSING:
        if attempts >= int(job.get("max_attempts") or 60):
            message = "YouCam feasibility run exceeded its durable polling window"
            await repo.update("provider_feasibility_runs", {"id": f"eq.{run_id}"}, {"provider_state": "failed", "attempts": attempts, "next_poll_at": None, "error": {"message": message}})
            await fail(repo, job, message)
            return
        when = datetime.now(UTC) + timedelta(seconds=backoff_seconds(attempts))
        await repo.update("provider_feasibility_runs", {"id": f"eq.{run_id}"}, {"provider_state": "processing", "attempts": attempts, "next_poll_at": when.isoformat()})
        await reschedule(repo, job["id"], when)
        return
    if result.state == ProviderState.FAILED:
        await repo.update("provider_feasibility_runs", {"id": f"eq.{run_id}"}, {"provider_state": "failed", "attempts": attempts, "error": result.error})
        await fail(repo, job, str(result.error))
        return
    async with httpx.AsyncClient(timeout=90) as client:
        output = await client.get(result.result_url)
        output.raise_for_status()
    result_path = f"feasibility/{run_id}.jpg"
    await repo.upload(settings.supabase_result_bucket, result_path, output.content, output.headers.get("content-type", "image/jpeg"), upsert=True)
    created = datetime.fromisoformat(str(run["created_at"]).replace("Z", "+00:00"))
    latency_ms = round((datetime.now(UTC) - created).total_seconds() * 1000)
    await repo.update("provider_feasibility_runs", {"id": f"eq.{run_id}"}, {"provider_state": "success", "attempts": attempts, "next_poll_at": None, "latency_ms": latency_ms, "result_path": result_path})
    await complete(repo, job["id"])


async def analyze_campaign(repo: SupabaseRepository, job: dict[str, Any]) -> None:
    campaign_id = job["payload"]["campaign_id"]
    campaign = await repo.one("campaigns", {"select": "id,source_path,playback_path,source_checksum,input_version,brand_direction,deterministic_metadata,duration_seconds", "id": f"eq.{campaign_id}"})
    if not campaign:
        raise RuntimeError("Campaign no longer exists")
    metadata = campaign.get("deterministic_metadata") or {}
    if not metadata.get("duration_seconds"):
        await reschedule(repo, job["id"], datetime.now(UTC) + timedelta(seconds=2), "Waiting for deterministic preprocessing")
        return
    links = await repo.select("campaign_products", {"select": "product_id", "campaign_id": f"eq.{campaign_id}", "order": "sort_order.asc"})
    product_ids = [str(link["product_id"]) for link in links]
    if not product_ids:
        await reschedule(repo, job["id"], datetime.now(UTC) + timedelta(seconds=2), "Waiting for campaign products")
        return
    products = await repo.select("products", {"select": "id,name,sku,metadata", "id": f"in.({','.join(product_ids)})"})
    references = await repo.select("look_reference_assets", {"select": "id,product_id,storage_path,checksum,mime_type,validation_state", "product_id": f"in.({','.join(product_ids)})", "validation_state": "eq.validated"})
    refs_by_product = {str(ref["product_id"]): ref for ref in references}
    product_input = []
    for product in products:
        reference = refs_by_product.get(str(product["id"]))
        product_input.append({
            **product,
            "reference_asset_id": reference.get("id") if reference else None,
            "reference_url": await repo.signed_url(settings.supabase_reference_bucket, reference["storage_path"], 1800) if reference else None,
            "reference_mime_type": reference.get("mime_type") if reference else None,
        })
    if not all(item.get("reference_asset_id") for item in product_input):
        await reschedule(repo, job["id"], datetime.now(UTC) + timedelta(seconds=2), "Waiting for validated YouCam references")
        return
    key = campaign_cache_key(campaign_scope=str(campaign_id), video_checksum=campaign["source_checksum"], input_version=int(campaign["input_version"]), model=settings.gemini_campaign_model)
    prior = await repo.select("campaign_analyses", {"select": "id,result", "cache_key": f"eq.{key}", "status": "eq.success", "order": "attempt_index.desc"})
    analysis = prior[0]["result"] if prior and not job["payload"].get("force_reanalysis") else None
    existing_looks = await repo.select("campaign_looks", {"select": "id", "campaign_id": f"eq.{campaign_id}", "limit": "1"})
    if analysis is not None and existing_looks:
        await repo.update("campaigns", {"id": f"eq.{campaign_id}"}, {"status": "review", "processing_error": None})
        await complete(repo, job["id"])
        return
    if analysis is None:
        attempts = await repo.select("campaign_analyses", {"select": "id,status", "cache_key": f"eq.{key}", "order": "attempt_index.asc"})
        if attempts and not job["payload"].get("force_reanalysis"):
            await repo.update("campaigns", {"id": f"eq.{campaign_id}"}, {"status": "failed", "processing_error": "A deliberate Gemini re-analysis is required"})
            await fail(repo, job, "A deliberate Gemini re-analysis is required")
            return
        if len(attempts) >= 2:
            await repo.update("campaigns", {"id": f"eq.{campaign_id}"}, {"status": "failed", "processing_error": "Gemini re-analysis limit reached"})
            await fail(repo, job, "Gemini re-analysis limit reached")
            return
        try:
            signed_video = await repo.signed_url(settings.supabase_campaign_bucket, campaign.get("playback_path") or campaign["source_path"], 1800)
            started = time.monotonic()
            analysis, raw = await GeminiInteractions(settings).analyze_campaign(
                video_url=signed_video,
                products=product_input,
                brand_direction=campaign.get("brand_direction") or "Preserve the original campaign styling.",
                timing_candidates=metadata.get("timing_candidates", []),
            )
            latency_ms = round((time.monotonic() - started) * 1000)
            await repo.insert("campaign_analyses", {
                "campaign_id": campaign_id,
                "cache_key": key,
                "model": settings.gemini_campaign_model,
                "schema_version": SCHEMA_VERSION,
                "status": "success",
                "result": analysis,
                "provider_interaction_id": raw.get("id"),
                "latency_ms": latency_ms,
                "attempt_index": len(attempts),
            })
        except GeminiError as error:
            await repo.insert("campaign_analyses", {
                "campaign_id": campaign_id,
                "cache_key": key,
                "model": settings.gemini_campaign_model,
                "schema_version": SCHEMA_VERSION,
                "status": "failed",
                "result": None,
                "attempt_index": len(attempts),
            })
            await repo.update("campaigns", {"id": f"eq.{campaign_id}"}, {"status": "failed", "processing_error": str(error)})
            await fail(repo, job, str(error))
            return
    if existing_looks:
        await repo.delete("campaign_looks", {"campaign_id": f"eq.{campaign_id}"})
    known_products = {str(product["id"]): product for product in products}
    duration = float(campaign["duration_seconds"] or metadata["duration_seconds"])
    analysis_video_path = campaign.get("playback_path") or campaign["source_path"]
    video_payload = await repo.download(settings.supabase_campaign_bucket, analysis_video_path)
    suffix = Path(analysis_video_path).suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(video_payload)
        video_path = Path(handle.name)
    pending_segments: list[dict[str, Any]] = []
    try:
        for look_order, proposed in enumerate(analysis.get("looks", [])[:8]):
            product_id = str(proposed.get("matched_product_id") or "")
            if product_id not in known_products:
                product_id = ""
            reference = refs_by_product.get(product_id) if product_id else None
            category = proposed.get("garment_category") or "auto"
            product_category = known_products.get(product_id, {}).get("metadata", {}).get("garment_category")
            if category == "auto" and product_category in {item.value for item in GarmentCategory if item != GarmentCategory.AUTO}:
                category = product_category
            normalized_segments = []
            for segment in proposed.get("segments", []):
                start = max(0.0, min(float(segment.get("start_seconds", 0)), duration))
                end = max(start, min(float(segment.get("end_seconds", 0)), duration))
                if end - start >= 0.05:
                    normalized_segments.append((start, end))
            poster_path = None
            if normalized_segments:
                start, end = normalized_segments[0]
                try:
                    poster = await extract_jpeg_frame(video_path, start + (end - start) * 0.5)
                    poster_path = f"{campaign_id}/look-{look_order + 1:02d}.jpg"
                    await repo.upload(settings.supabase_frame_bucket, poster_path, poster, "image/jpeg", upsert=True)
                except MediaValidationError:
                    poster_path = None
            look_rows = await repo.insert("campaign_looks", {
                "campaign_id": campaign_id,
                "label": str(proposed.get("label") or f"LOOK {look_order + 1:02d}")[:80],
                "description": str(proposed.get("description") or "")[:1000],
                "product_id": product_id or None,
                "reference_asset_id": reference.get("id") if reference else None,
                "poster_path": poster_path,
                "garment_category": category if category in {item.value for item in GarmentCategory} else "auto",
                "is_hero": bool(proposed.get("is_hero")),
                "remix_allowed": bool(proposed.get("remix_recommended")),
                "confidence": proposed.get("confidence"),
                "sort_order": look_order,
            })
            look_id = look_rows[0]["id"]
            for start, end in normalized_segments:
                pending_segments.append({"campaign_id": campaign_id, "look_id": look_id, "start_seconds": start, "end_seconds": end})
        pending_segments.sort(key=lambda segment: (segment["start_seconds"], segment["end_seconds"]))
        if pending_segments:
            await repo.insert("campaign_segments", [{**segment, "sort_order": index} for index, segment in enumerate(pending_segments)])
    finally:
        video_path.unlink(missing_ok=True)
    await repo.update("campaigns", {"id": f"eq.{campaign_id}"}, {"status": "review", "processing_error": None})
    await complete(repo, job["id"])


async def refresh_linked_sessions(repo: SupabaseRepository, request_id: str) -> None:
    links = await repo.select("mirror_results", {"select": "session_id", "youcam_request_id": f"eq.{request_id}"})
    for session_id in {str(link["session_id"]) for link in links}:
        await repo.rpc("refresh_mirror_session_status", {"p_session_id": session_id})


async def process_youcam_request(repo: SupabaseRepository, job: dict[str, Any]) -> None:
    request_id = job["payload"]["request_id"]
    request = await repo.one("youcam_requests", {"select": "*", "id": f"eq.{request_id}"})
    if not request:
        raise RuntimeError("YouCam request no longer exists")
    if request["provider_state"] in {"success", "failed", "provider_unknown"}:
        await refresh_linked_sessions(repo, request_id)
        await complete(repo, job["id"])
        return
    provider = YouCamClothesV3(settings)
    if not request.get("provider_task_id"):
        if request["provider_state"] == "submitting":
            message = "YouCam task submission was interrupted before a task ID was persisted; manual reconciliation is required"
            await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "provider_unknown", "next_poll_at": None, "error": {"message": message}})
            await refresh_linked_sessions(repo, request_id)
            await fail(repo, job, message)
            return
        claimed = await repo.rpc("claim_youcam_submission_slot", {"p_request_id": request_id, "p_limit": settings.youcam_max_in_flight})
        if not claimed:
            await reschedule(repo, job["id"], datetime.now(UTC) + timedelta(seconds=2), "Waiting for a YouCam in-flight slot")
            return
        source_url = await repo.signed_url(settings.supabase_private_bucket, request["source_path"], 1800)
        reference_url = await repo.signed_url(settings.supabase_reference_bucket, request["reference_path"], 1800)
        try:
            task_id = await provider.create_task(source_url=source_url, reference_url=reference_url, garment_category=GarmentCategory(request["garment_category"]))
        except httpx.HTTPError:
            message = "YouCam task creation ended with an ambiguous transport error; manual reconciliation is required and no blind retry was made"
            await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "provider_unknown", "next_poll_at": None, "error": {"message": message}})
            await refresh_linked_sessions(repo, request_id)
            await fail(repo, job, message)
            return
        except YouCamError as error:
            if error.status_code == 429:
                if int(job.get("attempts") or 0) >= int(job.get("max_attempts") or 60):
                    await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "failed", "next_poll_at": None, "error": {"message": "YouCam quota remained unavailable through the retry window", "http_status": error.status_code}})
                    await refresh_linked_sessions(repo, request_id)
                    await fail(repo, job, str(error))
                    return
                when = datetime.now(UTC) + timedelta(seconds=backoff_seconds(int(job.get("attempts") or 0)))
                await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "queued", "next_poll_at": when.isoformat(), "error": {"message": str(error), "http_status": error.status_code}})
                await reschedule(repo, job["id"], when, str(error))
                return
            if error.retryable:
                message = "YouCam task submission returned an ambiguous provider error; manual reconciliation is required"
                await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "provider_unknown", "next_poll_at": None, "error": {"message": message, "http_status": error.status_code}})
                await refresh_linked_sessions(repo, request_id)
                await fail(repo, job, message)
                return
            await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "failed", "error": {"message": str(error), "http_status": error.status_code}})
            await refresh_linked_sessions(repo, request_id)
            await fail(repo, job, str(error))
            return
        when = datetime.now(UTC) + timedelta(seconds=3)
        await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_task_id": task_id, "provider_state": "processing", "attempts": 0, "next_poll_at": when.isoformat()})
        await reschedule(repo, job["id"], when)
        return
    try:
        polled = await provider.poll(request["provider_task_id"])
    except YouCamError as error:
        attempts = int(request.get("attempts") or 0) + 1
        if error.retryable and attempts < int(job.get("max_attempts") or 60):
            when = datetime.now(UTC) + timedelta(seconds=backoff_seconds(attempts))
            await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "processing", "attempts": attempts, "next_poll_at": when.isoformat(), "error": {"message": str(error), "http_status": error.status_code}})
            await reschedule(repo, job["id"], when, str(error))
            return
        await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "failed", "attempts": attempts, "next_poll_at": None, "error": {"message": str(error), "http_status": error.status_code}})
        await refresh_linked_sessions(repo, request_id)
        await fail(repo, job, str(error))
        return
    attempts = int(request.get("attempts") or 0) + 1
    if polled.state == ProviderState.PROCESSING:
        if attempts >= int(job.get("max_attempts") or 60):
            message = "YouCam generation exceeded its durable polling window"
            await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "failed", "attempts": attempts, "next_poll_at": None, "error": {"message": message}})
            await refresh_linked_sessions(repo, request_id)
            await fail(repo, job, message)
            return
        when = datetime.now(UTC) + timedelta(seconds=backoff_seconds(attempts))
        await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "processing", "attempts": attempts, "next_poll_at": when.isoformat()})
        await reschedule(repo, job["id"], when)
        return
    if polled.state == ProviderState.FAILED:
        await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "failed", "attempts": attempts, "next_poll_at": None, "error": polled.error})
        await refresh_linked_sessions(repo, request_id)
        await fail(repo, job, str(polled.error))
        return
    result_path = f"requests/{request_id}.jpg"
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            output = await client.get(polled.result_url)
            output.raise_for_status()
        await repo.upload(settings.supabase_result_bucket, result_path, output.content, output.headers.get("content-type", "image/jpeg"), upsert=True)
    except httpx.HTTPError as error:
        if attempts >= int(job.get("max_attempts") or 60):
            await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "failed", "attempts": attempts, "next_poll_at": None, "error": {"message": "The completed YouCam result could not be persisted", "detail": str(error)[:500]}})
            await refresh_linked_sessions(repo, request_id)
            await fail(repo, job, str(error))
            return
        when = datetime.now(UTC) + timedelta(seconds=backoff_seconds(attempts))
        await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "processing", "attempts": attempts, "next_poll_at": when.isoformat(), "error": {"message": "Waiting to persist the completed YouCam result", "detail": str(error)[:500]}})
        await reschedule(repo, job["id"], when, str(error))
        return
    completed_at = datetime.now(UTC)
    created_at = datetime.fromisoformat(str(request["created_at"]).replace("Z", "+00:00"))
    latency_ms = round((completed_at - created_at).total_seconds() * 1000)
    await repo.update("youcam_requests", {"id": f"eq.{request_id}"}, {"provider_state": "success", "attempts": attempts, "next_poll_at": None, "result_path": result_path, "completed_at": completed_at.isoformat(), "latency_ms": latency_ms})
    await refresh_linked_sessions(repo, request_id)
    await complete(repo, job["id"])


HANDLERS = {
    "media_preprocess": process_media,
    "gemini_campaign_analysis": analyze_campaign,
    "feasibility_youcam_poll": poll_feasibility,
    "youcam_request": process_youcam_request,
}


async def run_once() -> int:
    repo = SupabaseRepository(settings)
    jobs = await repo.rpc("claim_jobs", {"p_worker_id": settings.worker_id, "p_limit": 4}) or []
    for job in jobs:
        handler = HANDLERS.get(job["kind"])
        if not handler:
            await fail(repo, job, f"Unknown job kind: {job['kind']}")
            continue
        try:
            await handler(repo, job)
        except (YouCamError, httpx.HTTPError) as error:
            attempts = int(job.get("attempts") or 0)
            if attempts >= int(job.get("max_attempts") or 8):
                await fail(repo, job, str(error))
            else:
                await reschedule(repo, job["id"], datetime.now(UTC) + timedelta(seconds=backoff_seconds(attempts)), str(error))
        except Exception as error:
            await fail(repo, job, str(error))
    return len(jobs)


async def run_forever() -> None:
    while True:
        count = await run_once()
        await asyncio.sleep(0.5 if count else 2.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_once() if args.once else run_forever())


if __name__ == "__main__":
    main()
