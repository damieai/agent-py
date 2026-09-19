from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///.runtime/agent.db"
    artifact_root: Path = Path(".runtime/artifacts")
    auth_secret: SecretStr = SecretStr("")
    auth_public_key: str = ""
    auth_issuer: str = "agent-py-local"
    auth_audience: str = "agent-py"
    execution_mode: Literal["simulation", "live"] = "simulation"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "agent-py-v1"
    model_id: str = ""
    collection_manifest: Path | None = None
    sandbox_image: str = ""
    sandbox_oracle: Path | None = None
    sandbox_root: Path = Path(".runtime/sandboxes")
    allow_model_api: bool = False
    model_input_micro_per_token: int = 0
    model_output_micro_per_token: int = 0
    model_api_key: SecretStr = SecretStr("")
    webhook_secret: SecretStr = SecretStr("")
    webhook_tenant: str = ""
    daily_budget_micro_usd: int = 20_000_000
    max_active_per_tenant: int = 8
    max_queue_per_tenant: int = 100

    @model_validator(mode="after")
    def validate_production(self):
        if self.environment == "production":
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
            if not self.auth_public_key or self.auth_issuer == "agent-py-local":
                raise ValueError("Production requires an external issuer and RSA public key")
        if self.daily_budget_micro_usd <= 0:
            raise ValueError("A positive daily budget is mandatory")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
