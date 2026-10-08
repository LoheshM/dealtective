"""SerpApi client with disk cache, replay fixtures and a per-run credit budget.

Every call is recorded as a `SerpCall` so the UI can show exactly which engines
the agent used, why, and whether the call cost a credit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# httpx logs full request URLs (including api_key) at INFO; keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)

SERPAPI_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"

# Params that never affect the result and must never be written to disk.
_SECRET_PARAMS = {"api_key"}


class BudgetExceeded(RuntimeError):
    pass


class ReplayMiss(RuntimeError):
    """Raised in replay mode when no recorded response exists for a query."""


class SerpError(RuntimeError):
    pass


@dataclass
class SerpCall:
    engine: str
    purpose: str
    params: dict[str, Any]
    source: str  # "live" | "cache" | "fixture"
    ms: int
    ok: bool
    error: str | None = None
    result_count: int = 0

    def to_event(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "purpose": self.purpose,
            "params": self.params,
            "source": self.source,
            "ms": self.ms,
            "ok": self.ok,
            "error": self.error,
            "result_count": self.result_count,
            "credit": self.source == "live" and self.ok,
        }


@dataclass
class CreditBudget:
    limit: int
    spent: int = 0

    def reserve(self) -> None:
        if self.spent >= self.limit:
            raise BudgetExceeded(f"credit budget of {self.limit} reached")
        self.spent += 1

    def refund(self) -> None:
        self.spent = max(0, self.spent - 1)


def cache_key(engine: str, params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if k not in _SECRET_PARAMS}
    blob = json.dumps({"engine": engine, **clean}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _count_results(data: dict[str, Any]) -> int:
    for key in ("organic_results", "shopping_results", "visual_matches", "video_results", "news_results"):
        if isinstance(data.get(key), list):
            return len(data[key])
    stores = (data.get("product_results") or {}).get("stores")
    if isinstance(stores, list):
        return len(stores)
    return 1 if data.get("product_results") else 0


def scrub(data: dict[str, Any]) -> dict[str, Any]:
    """Remove anything that could carry the API key before persisting."""
    data = dict(data)
    sp = dict(data.get("search_parameters") or {})
    for k in _SECRET_PARAMS:
        sp.pop(k, None)
    if "search_parameters" in data:
        data["search_parameters"] = sp
    return data


class SerpClient:
    """Async SerpApi client.

    mode:
      - "auto":   cache/fixture first, then live (default)
      - "replay": cache/fixture only; never spends credits (demo without a key)
      - "live":   always call the API (still writes the cache)
    """

    def __init__(
        self,
        api_key: str | None,
        cache_dir: Path,
        fixtures_dir: Path | None = None,
        mode: str = "auto",
        ttl_hours: float = 24.0,
        timeout: float = 90.0,  # Google web searches with site: can take >45 s
        transport: httpx.AsyncBaseTransport | None = None,
        live_per_hour: int = 0,
    ) -> None:
        if mode not in {"auto", "replay", "live"}:
            raise ValueError(f"unknown mode {mode!r}")
        if mode != "replay" and not api_key:
            mode = "replay"
        self.api_key = api_key
        self.mode = mode
        self.cache_dir = cache_dir
        self.fixtures_dir = fixtures_dir
        self.ttl_s = ttl_hours * 3600
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.used_keys: set[str] = set()  # for fixture recording
        # Server-wide cap on billed searches per rolling hour (0 = unlimited). Protects the
        # account from anyone hammering a public deployment with novel queries.
        self.live_per_hour = live_per_hour
        self._live_times: deque[float] = deque()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- storage -----------------------------------------------------------
    def _read(self, key: str) -> tuple[dict[str, Any], str] | None:
        p = self.cache_dir / f"{key}.json"
        if p.exists():
            rec = json.loads(p.read_text(encoding="utf-8"))
            if self.mode == "replay" or time.time() - rec.get("ts", 0) < self.ttl_s:
                return rec["response"], "cache"
        if self.fixtures_dir is not None:
            f = self.fixtures_dir / f"{key}.json"
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8"))["response"], "fixture"
        return None

    def _write(self, key: str, engine: str, params: dict[str, Any], data: dict[str, Any]) -> None:
        rec = {
            "ts": time.time(),
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "engine": engine,
            "params": {k: v for k, v in params.items() if k not in _SECRET_PARAMS},
            "response": scrub(data),
        }
        # atomic: concurrent checks of the same product must never read a half-written file
        dst = self.cache_dir / f"{key}.json"
        tmp = dst.with_suffix(f".{os.getpid()}.{id(rec)}.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, dst)

    # -- api ---------------------------------------------------------------
    async def search(
        self,
        engine: str,
        params: dict[str, Any],
        *,
        purpose: str,
        budget: CreditBudget | None = None,
        on_call: Callable[[SerpCall], Awaitable[None] | None] | None = None,
    ) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None}
        key = cache_key(engine, params)
        self.used_keys.add(key)
        t0 = time.perf_counter()
        call: SerpCall
        try:
            hit = None if self.mode == "live" else self._read(key)
            if hit is not None:
                data, source = hit
                call = SerpCall(engine, purpose, params, source, _ms(t0), True, result_count=_count_results(data))
                return data
            if self.mode == "replay":
                raise ReplayMiss(f"no recorded response for {engine} {params}")
            if budget is not None:
                budget.reserve()
            try:
                self._check_hourly()
            except BudgetExceeded:
                if budget is not None:
                    budget.refund()
                raise
            try:
                # shielded: once SerpApi has billed the search, finish and cache it even if the
                # browser disconnects, so a retry is free.
                data = await asyncio.shield(self._live_and_store(key, engine, params))
            except Exception:
                if budget is not None:
                    budget.refund()  # failed searches are not billed by SerpApi
                raise
            call = SerpCall(engine, purpose, params, "live", _ms(t0), True, result_count=_count_results(data))
            return data
        except Exception as e:
            call = SerpCall(engine, purpose, params, "live" if self.mode != "replay" else "fixture", _ms(t0), False, str(e)[:200])
            raise
        finally:
            if on_call is not None:
                res = on_call(call)
                if asyncio.iscoroutine(res):
                    await res

    def _check_hourly(self) -> None:
        if not self.live_per_hour:
            return
        now = time.time()
        while self._live_times and now - self._live_times[0] > 3600:
            self._live_times.popleft()
        if len(self._live_times) >= self.live_per_hour:
            raise BudgetExceeded("hourly live-search limit reached on this server; cached results only")
        self._live_times.append(now)

    async def _live_and_store(self, key: str, engine: str, params: dict[str, Any]) -> dict[str, Any]:
        data = await self._live(engine, params)
        self._write(key, engine, params, data)
        return data

    async def _live(self, engine: str, params: dict[str, Any]) -> dict[str, Any]:
        q = {"engine": engine, **params, "api_key": self.api_key}
        for attempt in range(2):
            try:
                r = await self._http.get(SERPAPI_URL, params=q)
            except httpx.TransportError as e:
                if attempt == 0:
                    continue
                raise SerpError(f"network error: {e.__class__.__name__}") from None
            try:
                data = r.json()
            except ValueError:
                raise SerpError(f"HTTP {r.status_code}: non-JSON response") from None
            if r.status_code >= 400 or data.get("error"):
                msg = data.get("error") or f"HTTP {r.status_code}"
                # "hasn't returned any results" is a valid, billed, empty answer
                if "hasn't returned any results" in str(msg):
                    return data
                raise SerpError(str(msg))
            return data
        raise SerpError("unreachable")

    async def account(self) -> dict[str, Any] | None:
        """Free endpoint: remaining credits. Never counted against quota."""
        if not self.api_key:
            return None
        try:
            r = await self._http.get(ACCOUNT_URL, params={"api_key": self.api_key})
            d = r.json()
            return {
                "plan": d.get("plan_name"),
                "searches_left": d.get("total_searches_left"),
                "this_hour": d.get("this_hour_searches"),
                "hourly_limit": d.get("account_rate_limit_per_hour"),
            }
        except Exception:  # noqa: BLE001 - informational only
            return None


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
