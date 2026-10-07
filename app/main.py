"""FastAPI app: SSE endpoints for the pipeline plus the static UI."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import pipeline
from .config import ROOT, Settings, load_settings
from .llm import LLM
from .serp import SerpClient

log = logging.getLogger("aslidaam")
WEB = ROOT / "web"

EXAMPLES = [
    {"label": "boAt Airdopes 141 Gen 2", "query": "https://www.amazon.in/dp/B0F8BVSK21",
     "hint": "Amazon link · 80% off claim"},
    {"label": "Redmi 13 5G", "query": "Redmi 13 5G 8GB 128GB", "hint": "Phone · typed search"},
    {"label": "Philips air fryer", "query": "Philips HD9252/70 air fryer", "hint": "Thin data · honest answer"},
]


# Lens runs only on product images the app itself surfaced (Amazon / Google image CDNs), so the
# endpoint can't be used as a free reverse-image-search proxy on our SerpApi key.
LENS_HOSTS = ("m.media-amazon.com", "images-na.ssl-images-amazon.com", "images-eu.ssl-images-amazon.com",
              ".gstatic.com", ".googleusercontent.com", ".ggpht.com")


def lens_host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or (h.startswith(".") and host.endswith(h)) for h in LENS_HOSTS)


def build_deps(s: Settings) -> pipeline.Deps:
    mode = s.effective_mode
    serp = SerpClient(s.serpapi_key, s.cache_dir, s.fixtures_dir, mode=mode, ttl_hours=s.cache_ttl_hours,
                      live_per_hour=s.live_per_hour)
    llm = LLM(s.openai_key, s.openai_model, s.cache_dir, s.fixtures_dir, replay=(mode == "replay"))
    return pipeline.Deps(serp=serp, llm=llm, credits_per_check=s.credits_per_check)


def create_app(settings: Settings | None = None, deps: pipeline.Deps | None = None) -> FastAPI:
    s = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.deps = deps or build_deps(s)
        yield
        await app.state.deps.serp.aclose()

    app = FastAPI(title="AsliDaam", lifespan=lifespan)

    @app.middleware("http")
    async def same_origin_api(request: Request, call_next):
        # Credit-spending endpoints must not be triggerable by other websites (<img src=…>, fetch).
        if request.url.path.startswith("/api/") and request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "cross-site requests are not allowed"}, status_code=403)
        return await call_next(request)

    def stream(runner) -> EventSourceResponse:
        queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()

        async def emit(event: str, data: dict) -> None:
            await queue.put((event, data))

        async def work() -> None:
            try:
                await runner(emit)
            except Exception:
                log.exception("pipeline failed")
                await queue.put(("error", {"text": "Something went wrong while checking this product."}))
            finally:
                await queue.put(None)

        async def gen():
            task = asyncio.create_task(work())
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    event, data = item
                    yield {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}
            finally:
                if not task.done():
                    task.cancel()

        return EventSourceResponse(gen(), ping=15)

    @app.get("/api/check/stream")
    async def check(q: str = Query(..., min_length=2, max_length=600)):
        d = app.state.deps
        return stream(lambda emit: pipeline.run(q, d, emit))

    @app.get("/api/lens/stream")
    async def lens(image: str = Query(..., min_length=8, max_length=1200), title: str = Query("", max_length=400)):
        if not image.startswith("https://") or not lens_host_allowed(image):
            return JSONResponse({"error": "image must be an https product image from Amazon or Google"},
                                status_code=400)
        d = app.state.deps
        return stream(lambda emit: pipeline.run_lens(image, title, d, emit))

    @app.get("/api/status")
    async def status():
        d: pipeline.Deps = app.state.deps
        acct = await d.serp.account() if d.serp.mode != "replay" else None
        return {
            "mode": d.serp.mode,
            "llm": d.llm.model if d.llm.available else None,
            "credits_per_check": d.credits_per_check,
            "account": acct,
        }

    @app.get("/api/examples")
    async def examples():
        return EXAMPLES

    @app.get("/")
    async def index():
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app


app = create_app()
