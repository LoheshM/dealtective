from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    serpapi_key: str | None
    openai_key: str | None
    openai_model: str
    mode: str  # auto | replay | live
    credits_per_check: int
    cache_dir: Path
    fixtures_dir: Path
    cache_ttl_hours: float
    live_per_hour: int = 40
    llm_provider: str = "openai"  # openai | gemini (Gemini via its OpenAI-compatible endpoint)
    llm_base_url: str | None = None

    @property
    def effective_mode(self) -> str:
        return "replay" if not self.serpapi_key else self.mode


GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODELS = {"openai": "gpt-5.4-mini", "gemini": "gemini-2.5-flash"}


def load_settings() -> Settings:
    openai_key = os.getenv("OPENAI_API_KEY") or None
    gemini_key = os.getenv("GEMINI_API_KEY") or None
    # LLM_PROVIDER picks explicitly; otherwise use whichever key is present (OpenAI first).
    provider = (os.getenv("LLM_PROVIDER") or ("openai" if openai_key or not gemini_key else "gemini")).lower()
    if provider not in DEFAULT_MODELS:
        provider = "openai"
    key = gemini_key if provider == "gemini" else openai_key
    model = os.getenv("LLM_MODEL") or (os.getenv("OPENAI_MODEL") if provider == "openai" else None) or DEFAULT_MODELS[provider]
    return Settings(
        serpapi_key=os.getenv("SERPAPI_API_KEY") or None,
        openai_key=key,
        openai_model=model,
        llm_provider=provider,
        llm_base_url=GEMINI_OPENAI_BASE if provider == "gemini" else None,
        mode=os.getenv("DEALTECTIVE_MODE", "auto"),
        credits_per_check=int(os.getenv("DEALTECTIVE_CREDITS_PER_CHECK", "5")),
        cache_dir=Path(os.getenv("DEALTECTIVE_CACHE_DIR", ROOT / "data" / "cache")),
        fixtures_dir=ROOT / "data" / "fixtures",
        cache_ttl_hours=float(os.getenv("DEALTECTIVE_CACHE_TTL_HOURS", "24")),
        live_per_hour=int(os.getenv("DEALTECTIVE_LIVE_PER_HOUR", "40")),
    )
