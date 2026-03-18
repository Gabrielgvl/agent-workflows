#!/usr/bin/env python3

from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    output_path = Path(sys.argv[1] if len(sys.argv) > 1 else "codex-review.txt")
    text = output_path.read_text(encoding="utf-8", errors="replace")
    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}

    for priority in re.findall(r"^\s*-\s*\[(P[0-3])\]", text, flags=re.MULTILINE):
        counts[priority] += 1

    for priority in re.findall(r'"priority"\s*:\s*"(P[0-3])"', text):
        counts[priority] += 1

    for raw in re.findall(r'"priority"\s*:\s*([0-3])', text):
        counts[f"P{raw}"] += 1

    review_marker = re.search(r"^\s*Review comment[s]?:", text, flags=re.MULTILINE)
    total = sum(counts.values())
    parse_inconclusive = 1 if review_marker and total == 0 else 0

    print(f"p0={counts['P0']}")
    print(f"p1={counts['P1']}")
    print(f"p2={counts['P2']}")
    print(f"p3={counts['P3']}")
    print(f"parse_inconclusive={parse_inconclusive}")
    print("parse_failed=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
