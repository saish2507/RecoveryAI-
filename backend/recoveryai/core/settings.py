"""Typed configuration for the whole platform.

Every tunable lives here so an integrator can see the complete configuration
surface in one file instead of grepping for `os.environ`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "db" / "recoveryai.db"


def _split_csv(value: object) -> object:
    """Accept `a,b,c` (and empty) in .env for list fields."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


#: A list field that reads `a,b,c` from the environment instead of JSON.
#:
#: `NoDecode` is load-bearing. Without it pydantic-settings JSON-decodes any
#: complex-typed field *before* validators run, so both `API_KEYS=` (empty) and
#: `CORS_ORIGINS=http://a,http://b` — the exact syntax documented in
#: `.env.example` — raise `SettingsError` at import time and the process never
#: starts. Nobody writes `API_KEYS=["sk_1"]` in a .env file.
CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("RECOVERYAI_ENV_FILE", REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── LLM provider ──────────────────────────────────────────
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    # Flash-lite, and deliberately: this workload is a high volume of small
    # structured tool calls, where daily request headroom decides how much of the
    # book the agent actually gets to decide. Depth is not the binding constraint;
    # quota is. `gemini-3.5-flash` allows 20 requests/day on the free tier, which
    # this system exhausts in roughly ten cases before every later decision falls
    # back to the policy tables.
    #
    # `gemini-2.5-pro` and `gemini-2.5-flash` are not options: both still appear
    # in ListModels but 404 as "no longer available to new users".
    llm_model: str = "gemini-3.5-flash-lite"
    llm_timeout_seconds: float = 30.0

    # Rate/cost governance. Provider-agnostic: point these at whatever
    # your enterprise LLM allows. Defaults sit just under Gemini's free tier.
    # Sits just under the provider's free tier. Every decision now goes to the
    # model, so a busy day can exhaust this — when it does the agent falls back
    # to the policy tables and marks the step `fallback_budget`, which is the
    # trace saying plainly that no model was consulted rather than pretending one
    # was. Raise both together with the tier, never one alone.
    llm_max_rpm: int = Field(default=4, ge=1)
    llm_max_rpd: int = Field(default=90, ge=1)

    # ── Agent behaviour ───────────────────────────────────────
    agent_mode: str = "live"  # live | shadow
    action_executor: str = "simulated"  # simulated | webhook | razorpay
    action_webhook_url: str = ""
    action_webhook_timeout_seconds: float = 10.0

    # ── Razorpay ──────────────────────────────────────────────
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    #: Guard against a production `.env` being copied onto a machine running the
    #: recovery loop. A non-`rzp_test_` key is refused unless this is set.
    razorpay_allow_live: bool = False
    razorpay_callback_url: str = ""
    razorpay_timeout_seconds: float = Field(default=15.0, gt=0)
    followup_delay_seconds: float = Field(default=14_400.0, gt=0)  # 4h: real retry cadence

    # ── Escalation notifications ──────────────────────────────
    # Empty by default and deliberately so: a fresh checkout must not start
    # posting into somebody's chat workspace.
    slack_webhook_url: str = ""
    slack_timeout_seconds: float = Field(default=5.0, gt=0)

    # ── Ingestion security ────────────────────────────────────
    webhook_signing_secret: str = ""
    webhook_signature_header: str = "X-RecoveryAI-Signature"

    # ── API security ──────────────────────────────────────────
    api_keys: CsvList = Field(default_factory=list)
    api_key_header: str = "X-API-Key"
    cors_origins: CsvList = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:4173"]
    )

    # ── Storage ───────────────────────────────────────────────
    database_url: str = ""

    # ── Demo simulator ────────────────────────────────────────
    simulator_enabled: bool = True
    simulator_interval_seconds: float = Field(default=6.0, gt=0)

    # ── Logging ───────────────────────────────────────────────
    log_level: str = "INFO"
    log_json: bool = True

    _split_lists = field_validator("api_keys", "cors_origins", mode="before")(_split_csv)

    @field_validator("agent_mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"live", "shadow"}:
            raise ValueError("AGENT_MODE must be 'live' or 'shadow'")
        return v

    @field_validator("action_executor")
    @classmethod
    def _validate_executor(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"simulated", "webhook", "razorpay"}:
            raise ValueError("ACTION_EXECUTOR must be 'simulated', 'webhook' or 'razorpay'")
        return v

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{DEFAULT_DB_PATH}"

    @property
    def llm_configured(self) -> bool:
        """False means the agent runs deterministically and makes zero network calls."""
        return bool(self.gemini_api_key.strip())

    @property
    def shadow_mode(self) -> bool:
        return self.agent_mode == "shadow"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)


@lru_cache
def get_settings() -> Settings:
    return Settings()
