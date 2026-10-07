from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import EXAMPLES, create_app

from .conftest import build_deps


def settings(tmp_path: Path) -> Settings:
    return Settings(serpapi_key=None, openai_key=None, openai_model="gpt-test", mode="auto", credits_per_check=4,
                    cache_dir=tmp_path / "cache", fixtures_dir=tmp_path / "fixtures", cache_ttl_hours=24)


@pytest.fixture
def client(tmp_path):
    app = create_app(settings(tmp_path), build_deps(tmp_path / "deps"))
    with TestClient(app) as c:
        yield c


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if name:
            events.append((name, json.loads("\n".join(data)) if data else {}))
    return events


def test_settings_effective_mode_is_replay_without_key(tmp_path):
    assert settings(tmp_path).effective_mode == "replay"


def test_status_replay_without_keys(client, _no_network):
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.json() == {"mode": "replay", "llm": None, "credits_per_check": 4, "account": None}
    assert _no_network.calls.call_count == 0


def test_status_with_default_deps_built_from_settings(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as c:
        body = c.get("/api/status").json()
    assert body["mode"] == "replay"
    assert body["llm"] is None


def test_examples(client):
    r = client.get("/api/examples")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and body == EXAMPLES
    assert all({"label", "query", "hint"} <= e.keys() for e in body)


def test_check_stream_emits_events_and_done(client):
    r = client.get("/api/check/stream", params={"q": "Sony WH-1000XM5 headphones"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: done" in r.text
    events = parse_sse(r.text)
    names = [n for n, _ in events]
    assert names[0] == "step"
    assert names[-1] == "done"
    assert "notice" in names  # nothing recorded in the temp fixtures dir
    assert names.index("market") < names.index("voices") < names.index("summary") < names.index("done")
    assert dict(events)["done"]["credits_used"] == 0


def test_check_stream_bad_link_emits_error(client):
    r = client.get("/api/check/stream", params={"q": "https://amzn.in/d/abc123X"})
    assert r.status_code == 200
    assert [n for n, _ in parse_sse(r.text)] == ["error"]


def test_check_stream_pipeline_crash_becomes_error_event(client, monkeypatch):
    from app import pipeline

    async def boom(q, deps, emit):
        await emit("step", {"id": "parse"})
        raise RuntimeError("secret internals")

    monkeypatch.setattr(pipeline, "run", boom)
    r = client.get("/api/check/stream", params={"q": "anything"})
    events = parse_sse(r.text)
    assert [n for n, _ in events] == ["step", "error"]
    assert "secret internals" not in r.text


@pytest.mark.parametrize("q", ["", "x"])
def test_check_query_too_short_is_422(client, q):
    assert client.get("/api/check/stream", params={"q": q}).status_code == 422


def test_check_query_missing_or_too_long_is_422(client):
    assert client.get("/api/check/stream").status_code == 422
    assert client.get("/api/check/stream", params={"q": "x" * 601}).status_code == 422


@pytest.mark.parametrize("image", [
    "http://example.com/a.jpg", "ftp://example.com/a.jpg", "javascript:alert(1)",
    "https://example.com/a.jpg",  # https but not an Amazon/Google product image
    "https://gstatic.com.evil.example/a.jpg",
])
def test_lens_rejects_disallowed_image(client, image):
    r = client.get("/api/lens/stream", params={"image": image})
    assert r.status_code == 400
    assert "https product image" in r.json()["error"]


def test_api_rejects_cross_site_requests(client):
    r = client.get("/api/status", headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert client.get("/api/status", headers={"sec-fetch-site": "same-origin"}).status_code == 200


def test_lens_image_too_short_is_422(client):
    assert client.get("/api/lens/stream", params={"image": "https:"}).status_code == 422


def test_lens_unrecorded_in_replay_emits_error(client, _no_network):
    r = client.get("/api/lens/stream", params={"image": "https://m.media-amazon.com/images/I/none.jpg", "title": "boAt Airdopes"})
    assert r.status_code == 200
    events = parse_sse(r.text)
    names = [n for n, _ in events]
    assert names == ["serp_call", "error"]
    assert events[1][1]["text"].startswith("Lens unavailable")
    assert _no_network.calls.call_count == 0


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<html" in r.text.lower()
