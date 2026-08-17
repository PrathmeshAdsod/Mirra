from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..models import GarmentCategory, ProviderState


class YouCamError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class YouCamPoll:
    state: ProviderState
    result_url: str | None = None
    error: dict[str, Any] | None = None
    raw_status: str | None = None


class YouCamClothesV3:
    CATEGORY_MAP = {
        GarmentCategory.OUTERWEAR: GarmentCategory.UPPER_BODY.value,
        GarmentCategory.FULL_BODY: GarmentCategory.FULL_BODY.value,
        GarmentCategory.UPPER_BODY: GarmentCategory.UPPER_BODY.value,
        GarmentCategory.LOWER_BODY: GarmentCategory.LOWER_BODY.value,
        GarmentCategory.SHOES: GarmentCategory.SHOES.value,
        GarmentCategory.AUTO: GarmentCategory.AUTO.value,
    }

    def __init__(self, settings: Settings):
        if not settings.youcam_api_key:
            raise YouCamError("YOUCAM_API_KEY is not configured")
        self.base = settings.youcam_api_base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {settings.youcam_api_key}", "content-type": "application/json"}

    @classmethod
    def provider_category(cls, garment_category: GarmentCategory) -> str:
        return cls.CATEGORY_MAP[garment_category]

    async def create_task(self, *, source_url: str, reference_url: str, garment_category: GarmentCategory) -> str:
        payload = {"src_file_url": source_url, "ref_file_url": reference_url, "garment_category": self.provider_category(garment_category)}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(f"{self.base}/s2s/v2.0/task/cloth-v3", headers=self.headers, json=payload)
        if response.status_code >= 400:
            raise YouCamError(
                f"YouCam task creation failed with HTTP {response.status_code}: {response.text[:500]}",
                retryable=response.status_code == 429 or response.status_code >= 500,
                status_code=response.status_code,
            )
        body = response.json()
        task_id = body.get("data", {}).get("task_id")
        if not task_id:
            raise YouCamError("YouCam accepted the request without returning task_id")
        return str(task_id)

    async def poll(self, task_id: str) -> YouCamPoll:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.base}/s2s/v2.0/task/cloth-v3/{task_id}", headers=self.headers)
        if response.status_code >= 500 or response.status_code == 429:
            raise YouCamError(f"Transient YouCam poll failure: HTTP {response.status_code}", retryable=True, status_code=response.status_code)
        if response.status_code >= 400:
            return YouCamPoll(state=ProviderState.FAILED, error={"http_status": response.status_code, "body": response.text[:500]})
        data = response.json().get("data", {})
        raw_status = str(data.get("task_status") or "processing").lower()
        if raw_status == "success":
            result_url = data.get("results", {}).get("url")
            if not result_url:
                return YouCamPoll(state=ProviderState.FAILED, error={"message": "Success response did not contain results.url"}, raw_status=raw_status)
            return YouCamPoll(state=ProviderState.SUCCESS, result_url=str(result_url), raw_status=raw_status)
        if raw_status in {"error", "failed", "failure"}:
            return YouCamPoll(state=ProviderState.FAILED, error=data.get("error") or {"message": "Provider failed"}, raw_status=raw_status)
        return YouCamPoll(state=ProviderState.PROCESSING, raw_status=raw_status)
