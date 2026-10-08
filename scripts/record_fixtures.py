"""Record the example queries into data/fixtures/ so the app runs offline (replay mode).

    uv run python -m scripts.record_fixtures            # all examples
    uv run python -m scripts.record_fixtures "<query>"  # one extra query

Uses the disk cache when warm, so re-recording is free. Responses are scrubbed of api_key.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys

from app.config import load_settings
from app.main import EXAMPLES, build_deps
from app.pipeline import run


async def main(queries: list[str]) -> None:
    s = load_settings()
    deps = build_deps(s)
    fx = s.fixtures_dir
    (fx / "llm").mkdir(parents=True, exist_ok=True)

    async def emit(event: str, data: dict) -> None:
        if event == "serp_call":
            print(f"   {data['source']:>7} {data['engine']}")
        elif event in {"done", "error", "notice"}:
            print(f"   {event}: {data}")

    for q in queries:
        print(f"> {q}")
        await run(q, deps, emit)
    n = 0
    for key in deps.serp.used_keys:
        src = s.cache_dir / f"{key}.json"
        if src.exists():
            rec = json.loads(src.read_text(encoding="utf-8"))
            assert "api_key" not in json.dumps(rec), "refusing to write a fixture containing api_key"
            shutil.copyfile(src, fx / src.name)
            n += 1
    for key in deps.llm.used_keys:
        src = s.cache_dir / "llm" / f"{key}.json"
        if src.exists():
            shutil.copyfile(src, fx / "llm" / src.name)
            n += 1
    (fx / "llm" / "MODEL").write_text(deps.llm.model, encoding="utf-8")  # replay keys LLM fixtures by this model
    print(f"wrote {n} fixture files to {fx}")
    await deps.serp.aclose()


if __name__ == "__main__":
    qs = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else [e["query"] for e in EXAMPLES]
    asyncio.run(main(qs))
