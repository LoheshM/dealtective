"""Fail if anything that looks like a secret is tracked (or staged) in git.

    uv run python -m scripts.secret_scan
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "openai key": re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "serpapi-style 64-hex key": re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])"),
    "api_key param": re.compile(r"api_key=[0-9a-zA-Z]{16,}"),
    "generic secret assignment": re.compile(r"(?i)(secret|password|token)\s*[=:]\s*['\"][^'\"\s]{12,}['\"]"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
ALLOW = {"uv.lock"}


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                           cwd=root, capture_output=True, text=True, check=True).stdout.split()
    secrets = set()
    env = root / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip().strip("'\"")
                if len(v) >= 12:
                    secrets.add(v)
    bad = []
    for f in files:
        if f in ALLOW or f == ".env":
            continue
        p = root / f
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for s in secrets:
            if s in text:
                bad.append(f"{f}: contains a value from .env")
        for name, rx in PATTERNS.items():
            if rx.search(text):
                bad.append(f"{f}: matches {name}")
    if ".env" in files:
        bad.append(".env is tracked by git!")
    print("\n".join(bad) or f"secret scan clean ({len(files)} files)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
