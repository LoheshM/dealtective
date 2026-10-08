"""Run one Dealtective check from the terminal and print the event stream.

    uv run python -m scripts.check "https://www.amazon.in/dp/B0F8BVSK21"
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.config import load_settings
from app.main import build_deps
from app.pipeline import run


async def main(q: str) -> None:
    deps = build_deps(load_settings())

    async def emit(event: str, data: dict) -> None:
        if event == "serp_call":
            tag = "LIVE 1cr" if data["credit"] else data["source"]
            print(f"  [{tag:>8}] {data['engine']:<26} {data['ms']:>5}ms  {data['purpose']}  {data.get('error') or ''}")
        elif event == "step":
            print(f"- {data['status']:<7} {data['text']}")
        elif event == "match":
            for c in data["candidates"]:
                print(f"     {c['label']:<9} {c['store'][:22]:<22} {c['price'] or 0:>8,.0f}  {c['title'][:60]}  ({c['reason']})")
        elif event == "market":
            print(json.dumps({"market": data["market"], "verdict": data["verdict"]}, indent=1, ensure_ascii=False))
            for o in data["offers"]:
                print(f"     {o['store'][:26]:<26} {o['price']:>8,.0f} {o['kind']:<8} {o['flag'] or ''} {'(ref-excluded)' if not o['in_reference'] else ''}")
        elif event in {"summary", "notice", "error", "done", "anchor"}:
            print(f"* {event}: {json.dumps(data, ensure_ascii=False)[:600]}")

    await run(q, deps, emit)
    await deps.serp.aclose()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # ₹ on Windows consoles
    asyncio.run(main(" ".join(sys.argv[1:])))
