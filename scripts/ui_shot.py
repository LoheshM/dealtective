"""Playwright screenshots of a full check (desktop + mobile, light + dark).

    uv run python -m scripts.ui_shot <base_url> <out_dir> [query]
"""

import sys

from playwright.sync_api import sync_playwright

base, out = sys.argv[1], sys.argv[2]
q = sys.argv[3] if len(sys.argv) > 3 else "Fire-Boltt Ninja Call Pro Plus"
errors: list[str] = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge")
    for name, vp, scheme in [("desktop-light", (1366, 900), "light"), ("desktop-dark", (1366, 900), "dark"), ("mobile", (390, 844), "light")]:
        pg = b.new_page(viewport={"width": vp[0], "height": vp[1]}, color_scheme=scheme)
        # Third-party images (store favicons from gstatic, product photos) can 404; that's not an app error.
        # Failures from our own server still are.
        pg.on("console", lambda m, n=name: errors.append(f"{n}: {m.type}: {m.text}")
              if m.type in ("error", "warning") and not m.text.startswith("Failed to load resource") else None)
        pg.on("response", lambda r, n=name: errors.append(f"{n}: HTTP {r.status} {r.url}")
              if r.status >= 400 and r.url.startswith(base) else None)
        pg.on("pageerror", lambda e, n=name: errors.append(f"{n}: pageerror: {e}"))
        pg.goto(base + "/")
        pg.wait_for_timeout(600)
        if name == "desktop-light":
            pg.screenshot(path=f"{out}/landing.png")
        pg.fill("#q", q)
        pg.click("#go")
        pg.wait_for_selector(".step:has-text('Done')", timeout=120000)
        pg.wait_for_timeout(500)
        pg.screenshot(path=f"{out}/{name}.png", full_page=True)
        sw = pg.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        if sw:
            errors.append(f"{name}: horizontal overflow")
        pg.close()
    b.close()
print("\n".join(errors) or "no console errors")
