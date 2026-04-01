#!/usr/bin/env python3
"""Check that admin-related env vars are documented in .env.example."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
TARGET_FILES = [
    ROOT / "admin_main.py",
    ROOT / "src" / "security.py",
]

ENV_RE = re.compile(r'os\.getenv\(\s*"([A-Z0-9_]+)"')


def parse_documented_vars() -> set[str]:
    documented: set[str] = set()
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        documented.add(line.split("=", 1)[0].strip())
    return documented


def parse_used_vars() -> set[str]:
    used: set[str] = set()
    for path in TARGET_FILES:
        text = path.read_text(encoding="utf-8")
        used.update(ENV_RE.findall(text))
    # DATABASE_URL is intentionally runtime-only, not exposed as example variable.
    used.discard("DATABASE_URL")
    return used


def main() -> int:
    documented = parse_documented_vars()
    used = parse_used_vars()
    missing = sorted(v for v in used if v not in documented)
    if missing:
        print("Missing env vars in .env.example:")
        for item in missing:
            print(f" - {item}")
        return 1
    print("env-sync check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
