"""Shared fixtures. Every test runs with the network blocked: no SerpApi, no OpenAI."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import respx

from app import pipeline
from app.config import ROOT
from app.llm import LLM
from app.serp import SerpClient, cache_key

# Model name the recorded LLM cache was produced with (part of the LLM cache key).
RECORDED_MODEL = "gpt-5.4-mini"

# Where recorded SerpApi / LLM responses may live: committed fixtures first, then the dev cache.
RECORDED_DIRS = [ROOT / "data" / "fixtures", ROOT / "data" / "cache"]

FAKE_KEY = "test-secret-key-do-not-leak"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Block every real httpx request (SerpApi, OpenAI, anything) and hide real keys."""
    for var in ("SERPAPI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def find_recorded(engine: str, params: dict) -> Path | None:
    key = cache_key(engine, params)
    for d in RECORDED_DIRS:
        p = d / f"{key}.json"
        if p.exists():
            return p
    return None


def recorded_llm_dir() -> Path | None:
    for d in RECORDED_DIRS:
        if (d / "llm").is_dir() and any((d / "llm").glob("*.json")):
            return d / "llm"
    return None


def make_serp(tmp_path: Path, fixtures_dir: Path | None = None) -> SerpClient:
    return SerpClient(None, tmp_path / "cache", fixtures_dir, mode="replay")


def make_llm(tmp_path: Path, fixtures_dir: Path | None = None, model: str = RECORDED_MODEL) -> LLM:
    return LLM(None, model, tmp_path / "cache", fixtures_dir, replay=True)


def build_deps(base: Path, fixtures_dir: Path | None = None, llm_fixtures_dir: Path | None = None) -> pipeline.Deps:
    """Replay-only pipeline deps: no SerpApi key, no OpenAI key."""
    return pipeline.Deps(serp=make_serp(base, fixtures_dir), llm=make_llm(base, llm_fixtures_dir), credits_per_check=4)


@pytest.fixture
async def make_deps(tmp_path):
    """Factory for replay-only pipeline deps; closes the http clients afterwards."""
    made: list[pipeline.Deps] = []

    def _make(fixtures_dir: Path | None = None, llm_fixtures_dir: Path | None = None) -> pipeline.Deps:
        d = build_deps(tmp_path / f"run{len(made)}", fixtures_dir, llm_fixtures_dir)
        made.append(d)
        return d

    yield _make
    for d in made:
        await d.serp.aclose()


class Recorder:
    """Collects (event, data) pairs emitted by the pipeline."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event: str, data: dict) -> None:
        # Events must be JSON-serialisable: the SSE endpoint dumps them.
        json.dumps(data, ensure_ascii=False, default=str)
        self.events.append((event, data))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]

    def of(self, name: str) -> list[dict]:
        return [d for e, d in self.events if e == name]

    def one(self, name: str) -> dict:
        found = self.of(name)
        assert len(found) == 1, f"expected one {name!r} event, got {len(found)}"
        return found[0]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


def copy_records(dest: Path, records: list[Path]) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for p in records:
        shutil.copy2(p, dest / p.name)
    return dest
