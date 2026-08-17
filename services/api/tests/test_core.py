from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app import worker as worker_module
from app.main import app, reserve_daily_capacity, youcam_cache_key
from app.media import MediaValidationError, extract_jpeg_frame, normalize_playback_mp4, probe_video
from app.models import GarmentCategory
from app.providers.gemini import CAMPAIGN_SCHEMA, SCHEMA_VERSION, GeminiInteractions, campaign_cache_key
from app.providers.youcam import YouCamClothesV3, YouCamError
from app.worker import backoff_seconds


def test_health_is_honest_when_providers_are_not_configured(monkeypatch):
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["limits"]["campaign_bytes"] == 45 * 1024 * 1024


def test_required_model_defaults_and_garment_categories():
    settings = Settings(_env_file=None)
    assert settings.gemini_campaign_model == "gemini-3.7-flash"
    assert settings.gemini_utility_model == "gemini-3.5-flash-lite"
    assert settings.gemini_interactions_api_version == "v1beta"
    assert settings.gemini_utility_interactions_api_version == "v1"
    assert settings.gemini_campaign_daily_limit == 18
    assert settings.youcam_daily_user_limit == 25
    assert {item.value for item in GarmentCategory} == {"outerwear", "full_body", "upper_body", "lower_body", "shoes", "auto"}


def test_campaign_cache_key_covers_relevant_versioning():
    base = campaign_cache_key(campaign_scope="campaign-a", video_checksum="a" * 64, input_version=1, model="gemini-3.7-flash")
    assert base == campaign_cache_key(campaign_scope="campaign-a", video_checksum="a" * 64, input_version=1, model="gemini-3.7-flash")
    assert base != campaign_cache_key(campaign_scope="campaign-a", video_checksum="a" * 64, input_version=2, model="gemini-3.7-flash")
    assert base != campaign_cache_key(campaign_scope="campaign-a", video_checksum="b" * 64, input_version=1, model="gemini-3.7-flash")
    assert base != campaign_cache_key(campaign_scope="campaign-b", video_checksum="a" * 64, input_version=1, model="gemini-3.7-flash")
    assert SCHEMA_VERSION == "campaign-analysis-v2"
    look_schema = CAMPAIGN_SCHEMA["properties"]["looks"]["items"]
    assert "identity_summary" in look_schema["required"]
    assert "distinguishing_features" in look_schema["required"]


def test_campaign_analysis_is_one_multimodal_interaction(monkeypatch):
    settings = Settings(_env_file=None, gemini_api_key="test-key")
    analyzer = GeminiInteractions(settings)
    captured = {}

    async def fake_create(payload, *, api_version):
        captured["payload"] = payload
        captured["api_version"] = api_version
        return {"steps": [{"type": "model_output", "content": [{"type": "text", "text": '{"campaign_summary":"test","looks":[],"transition_notes":[]}'}]}]}

    monkeypatch.setattr(analyzer, "_create", fake_create)
    asyncio.run(analyzer.analyze_campaign(
        video_url="https://storage.example/video.mp4",
        products=[{"id": "product-1", "name": "Jacket", "reference_asset_id": "ref-1", "reference_url": "https://storage.example/ref.jpg", "reference_mime_type": "image/jpeg"}],
        brand_direction="Keep the jacket exact.",
        timing_candidates=[1.25],
    ))
    assert captured["api_version"] == "v1beta"
    assert [item["type"] for item in captured["payload"]["input"]] == ["video", "image"]
    assert captured["payload"]["model"] == "gemini-3.7-flash"
    assert "https://storage.example/ref.jpg" not in captured["payload"]["system_instruction"]


def test_youcam_cache_key_deduplicates_the_actual_provider_request():
    base = youcam_cache_key(source_scope="user-a/photo-1", source_checksum="a" * 64, reference_checksum="b" * 64, garment_category="outerwear")
    assert base == youcam_cache_key(source_scope="user-a/photo-1", source_checksum="a" * 64, reference_checksum="b" * 64, garment_category="outerwear")
    assert base == youcam_cache_key(source_scope="user-a/photo-1", source_checksum="a" * 64, reference_checksum="b" * 64, garment_category="upper_body")
    assert base != youcam_cache_key(source_scope="user-b/photo-1", source_checksum="a" * 64, reference_checksum="b" * 64, garment_category="outerwear")
    assert base != youcam_cache_key(source_scope="user-a/photo-1", source_checksum="c" * 64, reference_checksum="b" * 64, garment_category="outerwear")
    assert base != youcam_cache_key(source_scope="user-a/photo-1", source_checksum="a" * 64, reference_checksum="c" * 64, garment_category="outerwear")
    assert base != youcam_cache_key(source_scope="user-a/photo-1", source_checksum="a" * 64, reference_checksum="b" * 64, garment_category="full_body")


def test_daily_capacity_uses_atomic_idempotent_rpc():
    class FakeRepository:
        def __init__(self, result: int):
            self.result = result
            self.calls = []

        async def rpc(self, name, args):
            self.calls.append((name, args))
            return self.result

    repo = FakeRepository(1)
    asyncio.run(reserve_daily_capacity(
        repo,
        event_name="youcam_user_reserved",
        limit=25,
        reservation_keys=["same", "same", "new"],
        user_id="00000000-0000-0000-0000-000000000001",
    ))
    assert repo.calls == [("reserve_daily_capacity", {
        "p_event_name": "youcam_user_reserved",
        "p_limit": 25,
        "p_reservation_keys": ["new", "same"],
        "p_user_id": "00000000-0000-0000-0000-000000000001",
    })]

    exhausted = FakeRepository(-1)
    try:
        asyncio.run(reserve_daily_capacity(
            exhausted,
            event_name="gemini_campaign_reserved",
            limit=18,
            reservation_keys=["analysis-key"],
        ))
    except HTTPException as error:
        assert error.status_code == 429
    else:
        raise AssertionError("Expected daily capacity exhaustion to fail before a provider request")


def test_youcam_errors_preserve_retryability():
    transient = YouCamError("quota", retryable=True, status_code=429)
    rejected = YouCamError("invalid", retryable=False, status_code=400)
    assert transient.retryable is True
    assert rejected.retryable is False


def test_brand_outerwear_maps_to_the_live_clothes_v3_enum():
    assert YouCamClothesV3.provider_category(GarmentCategory.OUTERWEAR) == "upper_body"
    assert YouCamClothesV3.provider_category(GarmentCategory.FULL_BODY) == "full_body"


def test_poll_backoff_is_bounded():
    assert backoff_seconds(0) == 3
    assert backoff_seconds(2) == 7
    assert backoff_seconds(100) == 30


def test_transient_youcam_poll_persists_attempt_and_next_poll(monkeypatch):
    class FakeRepository:
        def __init__(self):
            self.updates = []
            self.rpcs = []

        async def one(self, _table, _filters):
            return {"id": "request-1", "provider_task_id": "task-1", "provider_state": "processing", "attempts": 3}

        async def update(self, table, filters, record):
            self.updates.append((table, filters, record))

        async def select(self, _table, _filters):
            return []

        async def rpc(self, name, args):
            self.rpcs.append((name, args))

    class TransientProvider:
        def __init__(self, _settings):
            pass

        async def poll(self, _task_id):
            raise YouCamError("temporary", retryable=True, status_code=429)

    monkeypatch.setattr(worker_module, "YouCamClothesV3", TransientProvider)
    repo = FakeRepository()
    asyncio.run(worker_module.process_youcam_request(repo, {"id": "job-1", "payload": {"request_id": "request-1"}, "attempts": 4, "max_attempts": 60}))
    persisted = repo.updates[0][2]
    assert persisted["provider_state"] == "processing"
    assert persisted["attempts"] == 4
    assert persisted["next_poll_at"]
    assert repo.rpcs[0][0] == "reschedule_job"


def test_ffprobe_enforces_duration_and_returns_mechanics(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=640x480:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    result = asyncio.run(probe_video(video, max_seconds=30))
    assert result.duration_seconds == 1
    assert result.video_codec == "h264"
    assert (result.width, result.height) == (640, 480)
    poster = asyncio.run(extract_jpeg_frame(video, 0.5))
    assert poster.startswith(b"\xff\xd8")
    try:
        asyncio.run(probe_video(video, max_seconds=0.5))
    except MediaValidationError as error:
        assert "current limit" in str(error)
    else:
        raise AssertionError("Expected duration validation to fail")


def test_non_h264_input_normalizes_to_browser_safe_mp4(tmp_path: Path):
    source = tmp_path / "source.avi"
    output = tmp_path / "playback.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=640x480:d=1", "-c:v", "mpeg4", str(source)],
        check=True,
    )
    asyncio.run(normalize_playback_mp4(source, output))
    result = asyncio.run(probe_video(output, max_seconds=30))
    assert result.video_codec == "h264"
    assert "mp4" in result.format_name
