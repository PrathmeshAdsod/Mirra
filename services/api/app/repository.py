from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings


class RepositoryNotConfigured(RuntimeError):
    pass


class SupabaseRepository:
    def __init__(self, settings: Settings):
        if not settings.supabase_configured:
            raise RepositoryNotConfigured("Supabase URL and service-role key are required")
        self.settings = settings
        self.base = settings.supabase_url.rstrip("/")
        self.headers = {
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
        }

    async def request(self, method: str, path: str, *, json: Any = None, params: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> Any:
        merged = {**self.headers, **(headers or {})}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.request(method, f"{self.base}{path}", json=json, params=params, headers=merged)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    async def select(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        return await self.request("GET", f"/rest/v1/{table}", params=params)

    async def one(self, table: str, params: dict[str, str]) -> dict[str, Any] | None:
        rows = await self.select(table, {**params, "limit": "1"})
        return rows[0] if rows else None

    async def insert(self, table: str, record: dict[str, Any] | list[dict[str, Any]]) -> Any:
        return await self.request("POST", f"/rest/v1/{table}", json=record, headers={"Prefer": "return=representation"})

    async def upsert(self, table: str, record: dict[str, Any] | list[dict[str, Any]], *, on_conflict: str, ignore_duplicates: bool = False) -> Any:
        resolution = "ignore-duplicates" if ignore_duplicates else "merge-duplicates"
        return await self.request(
            "POST",
            f"/rest/v1/{table}",
            json=record,
            params={"on_conflict": on_conflict},
            headers={"Prefer": f"resolution={resolution},return=representation"},
        )

    async def update(self, table: str, filters: dict[str, str], record: dict[str, Any]) -> Any:
        return await self.request("PATCH", f"/rest/v1/{table}", params=filters, json=record, headers={"Prefer": "return=representation"})

    async def delete(self, table: str, filters: dict[str, str]) -> Any:
        return await self.request("DELETE", f"/rest/v1/{table}", params=filters, headers={"Prefer": "return=representation"})

    async def rpc(self, name: str, args: dict[str, Any]) -> Any:
        return await self.request("POST", f"/rest/v1/rpc/{name}", json=args)

    async def upload(self, bucket: str, path: str, data: bytes, content_type: str, *, upsert: bool = False) -> None:
        encoded = "/".join(quote(part, safe="") for part in path.split("/"))
        headers = {**self.headers, "Content-Type": content_type, "x-upsert": "true" if upsert else "false"}
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self.base}/storage/v1/object/{bucket}/{encoded}", content=data, headers=headers)
        response.raise_for_status()

    async def remove(self, bucket: str, paths: list[str]) -> None:
        if not paths:
            return
        await self.request("DELETE", f"/storage/v1/object/{quote(bucket, safe='')}", json={"prefixes": paths})

    async def download(self, bucket: str, path: str) -> bytes:
        encoded = "/".join(quote(part, safe="") for part in path.split("/"))
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.get(f"{self.base}/storage/v1/object/authenticated/{bucket}/{encoded}", headers=self.headers)
        response.raise_for_status()
        return response.content

    async def signed_url(self, bucket: str, path: str, expires_in: int = 900) -> str:
        encoded = "/".join(quote(part, safe="") for part in path.split("/"))
        payload = await self.request("POST", f"/storage/v1/object/sign/{bucket}/{encoded}", json={"expiresIn": expires_in})
        signed = payload["signedURL"]
        return signed if signed.startswith("http") else f"{self.base}/storage/v1{signed}"

    async def user_brand_id(self, user_id: str) -> str | None:
        row = await self.one("brand_members", {"select": "brand_id", "user_id": f"eq.{user_id}"})
        return str(row["brand_id"]) if row else None
