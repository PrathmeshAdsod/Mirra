from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from ..config import Settings


SCHEMA_VERSION = "campaign-analysis-v2"

CAMPAIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "campaign_summary": {"type": "string"},
        "looks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "label": {"type": "string"},
                    "identity_summary": {"type": "string"},
                    "distinguishing_features": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
                    "description": {"type": "string"},
                    "garment_category": {"type": "string", "enum": ["outerwear", "full_body", "upper_body", "lower_body", "shoes", "auto"]},
                    "matched_product_id": {"type": ["string", "null"]},
                    "is_hero": {"type": "boolean"},
                    "remix_recommended": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "segments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "start_seconds": {"type": "number"},
                                "end_seconds": {"type": "number"},
                                "view": {"type": "string", "enum": ["front", "side", "back", "detail", "transition", "unknown"]},
                                "identity_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["start_seconds", "end_seconds", "view", "identity_confidence"],
                        },
                    },
                },
                "required": ["label", "identity_summary", "distinguishing_features", "description", "garment_category", "matched_product_id", "is_hero", "remix_recommended", "confidence", "segments"],
            },
        },
        "transition_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["campaign_summary", "looks", "transition_notes"],
}

IMAGE_VALIDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "valid": {"type": "boolean"},
        "reason": {"type": "string"},
        "person_count": {"type": "integer", "minimum": 0, "maximum": 5},
        "face_fully_visible": {"type": "boolean"},
        "forward_standing_pose": {"type": "boolean"},
        "intended_region_visible": {"type": "boolean"},
        "single_garment_or_outfit": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["valid", "reason", "person_count", "face_fully_visible", "forward_standing_pose", "intended_region_visible", "single_garment_or_outfit", "confidence"],
}

REMIX_CONSTRAINT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "requested_tags": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        "rejected_freeform": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
    },
    "required": ["requested_tags", "rejected_freeform"],
}


class GeminiError(RuntimeError):
    pass


def campaign_cache_key(*, campaign_scope: str, video_checksum: str, input_version: int, model: str) -> str:
    canonical = f"{campaign_scope}:{video_checksum}:{input_version}:{model}:{SCHEMA_VERSION}".encode()
    return hashlib.sha256(canonical).hexdigest()


class GeminiInteractions:
    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise GeminiError("GEMINI_API_KEY is not configured")
        self.settings = settings

    async def _create(self, payload: dict[str, Any], *, api_version: str) -> dict[str, Any]:
        endpoint = f"https://generativelanguage.googleapis.com/{api_version}/interactions"
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(endpoint, headers={"x-goog-api-key": self.settings.gemini_api_key}, json=payload)
        if response.status_code >= 400:
            raise GeminiError(f"Gemini Interactions failed with HTTP {response.status_code}: {response.text[:500]}")
        return response.json()

    @staticmethod
    def _output_text(response: dict[str, Any]) -> str:
        chunks: list[str] = []
        for step in response.get("steps", []):
            if step.get("type") != "model_output":
                continue
            for item in step.get("content", []):
                if item.get("type") == "text" and item.get("text"):
                    chunks.append(item["text"])
        if not chunks and response.get("output_text"):
            chunks.append(response["output_text"])
        if not chunks:
            raise GeminiError("Gemini response contained no text output")
        return "".join(chunks)

    async def analyze_campaign(self, *, video_url: str, products: list[dict[str, Any]], brand_direction: str, timing_candidates: list[float]) -> tuple[dict[str, Any], dict[str, Any]]:
        media_inputs: list[dict[str, Any]] = [{"type": "video", "uri": video_url, "mime_type": "video/mp4", "resolution": "medium"}]
        semantic_products: list[dict[str, Any]] = []
        for product in products:
            item = {key: product.get(key) for key in ("id", "name", "sku", "metadata", "reference_asset_id") if product.get(key) is not None}
            if product.get("reference_url"):
                media_inputs.append({
                    "type": "image",
                    "uri": product["reference_url"],
                    "mime_type": product.get("reference_mime_type") or "image/jpeg",
                    "resolution": "medium",
                })
                item["reference_media_index"] = len(media_inputs) - 1
            semantic_products.append(item)
        prompt = {
            "task": "Analyze this single fashion campaign in one pass. A look is outfit identity, never a camera shot. Normalize all front, side, back, close-up, detail and non-contiguous appearances of the same styled outfit into one unique look with multiple segments. Identify garment semantics, hero look, transitions, product matches and how the brand direction applies. Use FFmpeg candidates as timing hints, not identity truth.",
            "products": semantic_products,
            "brand_direction": brand_direction,
            "ffmpeg_timing_candidates_seconds": timing_candidates,
            "rules": [
                "At most 8 unique outfit identities",
                "Do not create a new look for a camera cut, crop, angle, pose, occlusion or detail shot",
                "Put every reappearance of one outfit into that look's segments, including non-contiguous occurrences",
                "Only split looks when garment identity or the styled outfit materially changes",
                "Choose an explicit garment category whenever supported",
                "Use auto only when evidence is insufficient",
                "Each product reference_media_index identifies its reference image in the ordered interaction input, where index 0 is the campaign video",
                "Segments must be chronological, bounded by the video and non-overlapping",
                "Mark ambiguous transitions with lower identity confidence instead of inventing another look",
            ],
        }
        payload = {
            "model": self.settings.gemini_campaign_model,
            "input": media_inputs if len(media_inputs) > 1 else media_inputs[0],
            "system_instruction": "You are MIRRA's campaign understanding engine. Return only schema-valid JSON. Be conservative and expose low confidence. Campaign inputs: " + json.dumps(prompt, separators=(",", ":")),
            "response_format": {"type": "text", "mime_type": "application/json", "schema": CAMPAIGN_SCHEMA},
            "generation_config": {"thinking_level": "low", "max_output_tokens": 6000},
            "store": False,
        }
        response = await self._create(payload, api_version=self.settings.gemini_interactions_api_version)
        text = self._output_text(response)
        try:
            return json.loads(text), response
        except json.JSONDecodeError:
            repaired = await self.repair_json(text)
            return repaired, response

    async def validate_image(self, *, image_url: str, kind: str, garment_category: str = "auto", image_mime_type: str = "image/jpeg") -> dict[str, Any]:
        if kind not in {"shopper_source", "garment_reference"}:
            raise GeminiError("Unknown image validation kind")
        requirements = {
            "shopper_source": [
                "exactly one adult person",
                "face fully visible and unobstructed",
                "front-facing standing pose",
                "shoulders and intended clothing region clearly visible",
                "no distracting second person",
            ],
            "garment_reference": [
                "one front-facing standalone garment or one front-facing person wearing one clear outfit",
                "the intended try-on region is complete and unobstructed",
                "no composite collage or multiple competing garments",
                "if a person is present, face and pose are usable",
            ],
        }[kind]
        payload = {
            "model": self.settings.gemini_utility_model,
            "input": {"type": "image", "uri": image_url, "mime_type": image_mime_type, "resolution": "medium"},
            "system_instruction": "Be conservative. Return schema-valid JSON only. Reject ambiguous, cropped, obstructed, multi-person or multi-product inputs. Validation inputs: " + json.dumps({"task": "Validate this image before a YouCam Clothes v3 unit is spent.", "kind": kind, "garment_category": garment_category, "requirements": requirements}, separators=(",", ":")),
            "response_format": {"type": "text", "mime_type": "application/json", "schema": IMAGE_VALIDATION_SCHEMA},
            "generation_config": {"thinking_level": "minimal", "max_output_tokens": 700},
            "store": False,
        }
        response = await self._create(payload, api_version=self.settings.gemini_utility_interactions_api_version)
        try:
            return json.loads(self._output_text(response))
        except json.JSONDecodeError as error:
            raise GeminiError("Flash-Lite image validation did not return valid JSON") from error

    async def repair_json(self, malformed: str) -> dict[str, Any]:
        payload = {
            "model": self.settings.gemini_utility_model,
            "input": "Repair this malformed campaign JSON without inventing new facts:\n" + malformed[:30000],
            "response_format": {"type": "text", "mime_type": "application/json", "schema": CAMPAIGN_SCHEMA},
            "generation_config": {"thinking_level": "minimal", "max_output_tokens": 6000},
            "store": False,
        }
        response = await self._create(payload, api_version=self.settings.gemini_utility_interactions_api_version)
        try:
            return json.loads(self._output_text(response))
        except json.JSONDecodeError as error:
            raise GeminiError("Flash-Lite repair did not return valid JSON") from error

    async def parse_remix_constraint(self, text: str, allowed_tags: list[str]) -> dict[str, Any]:
        payload = {
            "model": self.settings.gemini_utility_model,
            "input": json.dumps({
                    "task": "Map the shopper's short remix request only to exact allowed brand tags. Put anything outside the allowed set in rejected_freeform.",
                    "shopper_text": text,
                    "allowed_tags": allowed_tags,
                }, separators=(",", ":")),
            "response_format": {"type": "text", "mime_type": "application/json", "schema": REMIX_CONSTRAINT_SCHEMA},
            "generation_config": {"thinking_level": "minimal", "max_output_tokens": 500},
            "store": False,
        }
        response = await self._create(payload, api_version=self.settings.gemini_utility_interactions_api_version)
        try:
            parsed = json.loads(self._output_text(response))
        except json.JSONDecodeError as error:
            raise GeminiError("Flash-Lite remix parsing did not return valid JSON") from error
        allowed = set(allowed_tags)
        parsed["requested_tags"] = [tag for tag in parsed.get("requested_tags", []) if tag in allowed]
        return parsed
