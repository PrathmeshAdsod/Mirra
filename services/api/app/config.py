from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


API_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=API_ENV_FILE, extra="ignore", case_sensitive=False)

    app_env: str = "development"
    web_origin: str = "http://localhost:3000"

    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_role_key: str | None = None
    supabase_campaign_bucket: str = "campaign-source"
    supabase_reference_bucket: str = "look-references"
    supabase_frame_bucket: str = "campaign-frames"
    supabase_private_bucket: str = "mirror-private"
    supabase_result_bucket: str = "mirror-results"

    gemini_api_key: str | None = None
    gemini_campaign_model: str = "gemini-3.7-flash"
    gemini_utility_model: str = "gemini-3.5-flash-lite"
    gemini_interactions_api_version: str = "v1beta"
    gemini_utility_interactions_api_version: str = "v1"
    gemini_campaign_daily_limit: int = Field(default=18, ge=1)
    image_validation_enabled: bool = True

    youcam_api_key: str | None = None
    youcam_api_base_url: str = "https://yce-api-01.makeupar.com"
    youcam_max_in_flight: int = 2
    youcam_daily_user_limit: int = Field(default=25, ge=1)

    campaign_max_bytes: int = Field(default=45 * 1024 * 1024, ge=1)
    campaign_max_seconds: float = Field(default=30.0, ge=1)
    youcam_image_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    worker_id: str = "mirra-worker"

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def youcam_configured(self) -> bool:
        return bool(self.youcam_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
