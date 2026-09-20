from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///.runtime/agent.db"
    artifact_root: Path = Path(".runtime/artifacts")
    auth_secret: SecretStr = SecretStr("")
    auth_public_key: str = ""
    auth_jwks_file: Path | None = None
    auth_token_type: str = Field(default="JWT", min_length=1, max_length=80)
    auth_max_token_lifetime_seconds: int = Field(default=3600, ge=60, le=86400)
    auth_clock_skew_seconds: int = Field(default=0, ge=0, le=60)
    auth_issuer: str = "agent-py-local"
    auth_audience: str = "agent-py"
    execution_mode: Literal["simulation", "live"] = "simulation"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "agent-py-v1"
    release_manifest: Path | None = None
    release_expected_id: str = Field(default="", pattern=r"^(|sha256:[a-f0-9]{64})$")
    release_root: Path = Path(".")
    model_id: str = ""
    collection_manifest: Path | None = None
    sandbox_image: str = ""
    sandbox_oracle: Path | None = None
    sandbox_root: Path = Path(".runtime/sandboxes")
    sandbox_timeout_seconds: int = Field(default=120, ge=1, le=120)
    repair_manifest: Path | None = None
    allow_candidate_execution: bool = False
    allow_model_api: bool = False
    model_input_micro_per_token: int = 0
    model_output_micro_per_token: int = 0
    model_api_key: SecretStr = SecretStr("")
    webhook_secret: SecretStr = SecretStr("")
    webhook_tenant: str = ""
    daily_budget_micro_usd: int = 20_000_000
    max_active_per_tenant: int = Field(default=8, ge=1, le=128)
    max_queue_per_tenant: int = Field(default=100, ge=1, le=10000)
    worker_lease_seconds: int = Field(default=360, ge=330, le=900)
    worker_activity_limit: int = Field(default=8, ge=1, le=128)
    max_reads_per_dependency: int = Field(default=4, ge=1, le=128)
    metrics_secret: SecretStr = SecretStr("")
    monitoring_tenants: list[str] = Field(default_factory=list, max_length=100)
    trace_file: Path | None = None
    audit_signing_manifest: Path | None = None
    context_strategy: Literal["lexical", "bm25_rrf"] = "lexical"
    worker_metrics_enabled: bool = False
    worker_metrics_host: Literal["127.0.0.1", "0.0.0.0"] = "127.0.0.1"
    worker_metrics_port: int = Field(default=9465, ge=0, le=65535)

    @model_validator(mode="after")
    def validate_production(self):
        if bool(self.release_manifest) != bool(self.release_expected_id):
            raise ValueError(
                "Release manifest and independent expected ID must be configured together"
            )
        if self.auth_public_key and self.auth_jwks_file is not None:
            raise ValueError("Configure exactly one RSA source: public key or local JWKS")
        if self.environment == "production":
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
            if (
                not self.auth_public_key and self.auth_jwks_file is None
            ) or self.auth_issuer == "agent-py-local":
                raise ValueError(
                    "Production requires an external issuer and RSA public key or local JWKS"
                )
        if self.daily_budget_micro_usd <= 0:
            raise ValueError("A positive daily budget is mandatory")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
