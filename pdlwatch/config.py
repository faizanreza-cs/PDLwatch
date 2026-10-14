"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(_env("PDLWATCH_DB", "data/pdlwatch.duckdb")))
    # duckdb (local file, default) | snowflake (credentials from SNOWFLAKE_* variables)
    backend: str = field(default_factory=lambda: _env("PDLWATCH_BACKEND", "duckdb"))
    # auto | anthropic | openai | azure | mock. "auto" uses Anthropic when a key and model are set.
    provider: str = field(default_factory=lambda: _env("PDLWATCH_PROVIDER", "auto"))
    # model id (Anthropic, OpenAI) or deployment name (Azure OpenAI); unused by the mock
    model: str = field(default_factory=lambda: _env("PDLWATCH_MODEL", ""))
    effort: str = field(default_factory=lambda: _env("PDLWATCH_EFFORT", "medium"))
    max_agent_steps: int = field(default_factory=lambda: int(_env("PDLWATCH_MAX_STEPS", "8")))
    sql_row_limit: int = field(default_factory=lambda: int(_env("PDLWATCH_SQL_LIMIT", "200")))
    # seconds before an agent-written query is stopped
    sql_timeout: float = field(default_factory=lambda: float(_env("PDLWATCH_SQL_TIMEOUT", "30")))
    # opt in to Anthropic's server-side refusal fallback on models that support it
    fallbacks: bool = field(
        default_factory=lambda: _env("PDLWATCH_FALLBACKS", "0").lower() in {"1", "true", "yes"}
    )


def get_settings() -> Settings:
    return Settings()


def results_path(db_path: str | Path) -> Path:
    return Path(db_path).with_name("results.json")


def cache_path(db_path: str | Path) -> Path:
    """Extraction cache and lookups, kept next to the warehouse they feed."""
    return Path(db_path).with_name("cache")


def corpus_path(db_path: str | Path) -> Path:
    return Path(db_path).with_name("corpus")
