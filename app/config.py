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

    @property
    def effective_mode(self) -> str:
        return "replay" if not self.serpapi_key else self.mode


def load_settings() -> Settings:
    return Settings(
        serpapi_key=os.getenv("SERPAPI_API_KEY") or None,
        openai_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
        mode=os.getenv("DEALTECTIVE_MODE", "auto"),
        credits_per_check=int(os.getenv("DEALTECTIVE_CREDITS_PER_CHECK", "5")),
        cache_dir=Path(os.getenv("DEALTECTIVE_CACHE_DIR", ROOT / "data" / "cache")),
        fixtures_dir=ROOT / "data" / "fixtures",
        cache_ttl_hours=float(os.getenv("DEALTECTIVE_CACHE_TTL_HOURS", "24")),
        live_per_hour=int(os.getenv("DEALTECTIVE_LIVE_PER_HOUR", "40")),
    )
