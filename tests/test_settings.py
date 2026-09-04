"""Configuration surface.

The CSV-list tests exist because of a real failure: `API_KEYS=` in a compose file
crashed the container at import time, before a single line of application code
ran. pydantic-settings JSON-decodes complex-typed fields *before* validators see
them, so both an empty value and the comma-separated syntax documented in
`.env.example` raised `SettingsError`. Nobody writes `API_KEYS=["sk_1"]` in a
`.env` file, so the fix was `NoDecode` — and these tests keep it that way.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from recoveryai.core.settings import Settings


def env_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Build Settings purely from environment variables, ignoring any real .env."""
    monkeypatch.setenv("RECOVERYAI_ENV_FILE", "definitely-not-a-file.env")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


# ── CSV list fields ────────────────────────────────────────────────


def test_empty_api_keys_is_an_empty_list_not_a_crash(monkeypatch) -> None:
    settings = env_settings(monkeypatch, API_KEYS="")
    assert settings.api_keys == []
    assert settings.auth_enabled is False


def test_empty_cors_origins_does_not_crash(monkeypatch) -> None:
    assert env_settings(monkeypatch, CORS_ORIGINS="").cors_origins == []


def test_comma_separated_lists_parse(monkeypatch) -> None:
    """The syntax `.env.example` actually documents."""
    settings = env_settings(
        monkeypatch,
        API_KEYS="sk_live_a,sk_live_b",
        CORS_ORIGINS="https://console.acme.com,https://ops.acme.com",
    )
    assert settings.api_keys == ["sk_live_a", "sk_live_b"]
    assert settings.cors_origins == ["https://console.acme.com", "https://ops.acme.com"]
    assert settings.auth_enabled is True


def test_whitespace_around_csv_entries_is_trimmed(monkeypatch) -> None:
    assert env_settings(monkeypatch, API_KEYS=" sk_a , sk_b ").api_keys == ["sk_a", "sk_b"]


def test_a_single_value_is_still_a_list(monkeypatch) -> None:
    assert env_settings(monkeypatch, API_KEYS="sk_only").api_keys == ["sk_only"]


def test_unset_list_fields_fall_back_to_defaults(monkeypatch) -> None:
    settings = env_settings(monkeypatch)
    assert settings.api_keys == []
    assert "http://localhost:5173" in settings.cors_origins


# ── Validated enums ────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["live", "shadow", "SHADOW", " live "])
def test_valid_agent_modes_are_accepted_and_normalised(monkeypatch, mode: str) -> None:
    assert env_settings(monkeypatch, AGENT_MODE=mode).agent_mode == mode.strip().lower()


def test_invalid_agent_mode_fails_loudly_at_startup(monkeypatch) -> None:
    """Better to refuse to boot than to silently run live when shadow was meant."""
    with pytest.raises(ValidationError, match="AGENT_MODE"):
        env_settings(monkeypatch, AGENT_MODE="dry-run")


def test_invalid_executor_fails_loudly(monkeypatch) -> None:
    with pytest.raises(ValidationError, match="ACTION_EXECUTOR"):
        env_settings(monkeypatch, ACTION_EXECUTOR="carrier_pigeon")


def test_shadow_mode_property_tracks_agent_mode(monkeypatch) -> None:
    assert env_settings(monkeypatch, AGENT_MODE="shadow").shadow_mode is True
    assert env_settings(monkeypatch, AGENT_MODE="live").shadow_mode is False


# ── Derived values ─────────────────────────────────────────────────


def test_blank_api_key_means_deterministic_only(monkeypatch) -> None:
    """The "never crashes without a key" guarantee starts here."""
    assert env_settings(monkeypatch, GEMINI_API_KEY="").llm_configured is False
    assert env_settings(monkeypatch, GEMINI_API_KEY="   ").llm_configured is False
    assert env_settings(monkeypatch, GEMINI_API_KEY="AIza-real").llm_configured is True


def test_explicit_database_url_wins_over_the_default_path(monkeypatch) -> None:
    settings = env_settings(monkeypatch, DATABASE_URL="sqlite:////var/data/x.db")
    assert settings.resolved_database_url == "sqlite:////var/data/x.db"


def test_rate_limits_are_configurable_for_an_enterprise_model(monkeypatch) -> None:
    settings = env_settings(monkeypatch, LLM_MAX_RPM="5000", LLM_MAX_RPD="1000000")
    assert settings.llm_max_rpm == 5000
    assert settings.llm_max_rpd == 1_000_000


@pytest.mark.parametrize("field,value", [("LLM_MAX_RPM", "0"), ("LLM_MAX_RPD", "0")])
def test_nonsensical_limits_are_rejected(monkeypatch, field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="greater than or equal"):
        env_settings(monkeypatch, **{field: value})
