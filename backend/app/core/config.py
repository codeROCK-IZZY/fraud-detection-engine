"""Application configuration.

All settings come from environment variables prefixed with ``FDE_`` (or a local
``.env`` file, which is git-ignored). Nothing secret has a default value: the
app refuses to start without ``FDE_JWT_SECRET``.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FDE_", env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://fde:fde@localhost:5432/fde"

    # --- security ---
    jwt_secret: SecretStr
    # Key for pseudonymising device fingerprints and payee references (HMAC-SHA256).
    # Must stay stable: changing it makes every known device and payee look new.
    pseudonym_secret: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    cookie_secure: bool = True
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    login_rate_limit_per_minute: int = 10
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # --- storage ---
    artifact_dir: Path = Path("./var/artifacts")
    artifact_s3_bucket: str | None = None
    upload_dir: Path = Path("./var/uploads")
    max_upload_mb: int = 50
    max_batch_transactions: int = 1_000

    # --- streaming ---
    streaming_enabled: bool = False
    kafka_bootstrap: str = "localhost:9092"
    kafka_topic_raw: str = "transactions.raw"
    kafka_topic_scored: str = "transactions.scored"
    kafka_topic_dlq: str = "transactions.dlq"
    kafka_group_id: str = "fde-stream-scorer"
    stream_batch_size: int = 500
    stream_max_attempts: int = 3

    # --- ML / decisioning ---
    model_name: str = "fraud-txn"
    label_maturity_days: int = 30

    # --- logging ---
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("jwt_secret", "pseudonym_secret")
    @classmethod
    def _secret_strength(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("secrets (FDE_JWT_SECRET, FDE_PSEUDONYM_SECRET) must be at least 32 characters")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
