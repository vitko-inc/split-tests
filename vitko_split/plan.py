"""Deciding how many parts, and which tests go in which part."""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import statistics
from typing import Dict, List, Optional, Sequence

TIMINGS_VERSION = 1
#: With timings, don't make parts shorter than this: each copy of the job has a fixed start cost.
TARGET_PART_SECONDS = 30.0
DEFAULT_SECONDS = 1.0


def estimate(ids: Sequence[str], timings: Optional[Dict[str, float]]) -> List[float]:
    """Seconds per test: known timings, else the median of the known ones, else 1 s."""
    known = {i: float(timings[i]) for i in ids if timings and i in timings}
    fallback = statistics.median(known.values()) if known else DEFAULT_SECONDS
    return [known.get(i, fallback) for i in ids]


def lpt(ids: Sequence[str], weights: Sequence[float], parts: int) -> List[List[str]]:
    """Longest first, each onto the least-loaded part. Parts keep the input order of their ids.

    Within 4/3 of the best possible slowest part (Graham's bound), and much closer on the long
    lists of short tests that test suites are."""
    order = sorted(range(len(ids)), key=lambda i: (-weights[i], i))
    heap = [(0.0, p) for p in range(parts)]
    chosen: List[List[int]] = [[] for _ in range(parts)]
    for i in order:
        load, p = heapq.heappop(heap)
        chosen[p].append(i)
        heapq.heappush(heap, (load + weights[i], p))
    return [[ids[i] for i in sorted(idx)] for idx in chosen]


def round_robin(ids: Sequence[str], parts: int) -> List[List[str]]:
    return [list(ids[p::parts]) for p in range(parts)]


def partition(ids: Sequence[str], parts: int, timings: Optional[Dict[str, float]]) -> List[List[str]]:
    """LPT when any timing is known, else round robin (tests next to each other in a suite tend to
    cost the same, so interleaving spreads the slow neighbourhoods)."""
    if timings and any(i in timings for i in ids):
        return lpt(ids, estimate(ids, timings), parts)
    return round_robin(ids, parts)


def choose_parts(
    requested: Optional[int],
    max_parts: int,
    units: Optional[int],
    timings: Optional[Dict[str, float]] = None,
    ids: Sequence[str] = (),
) -> int:
    """How many parts to run. ``requested`` None means auto."""
    limit = max(1, max_parts)
    if units is not None:
        limit = min(limit, max(1, units))
    if requested is not None:
        return max(1, min(requested, limit))
    if timings and ids and sum(1 for i in ids if i in timings) * 2 >= len(ids):
        total = sum(estimate(ids, timings))
        limit = min(limit, max(1, int(total // TARGET_PART_SECONDS)))
    return limit


def load_timings(path: Optional[str]) -> Optional[Dict[str, float]]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return clean_timings(data)


def clean_timings(data: object) -> Optional[Dict[str, float]]:
    """The ``tests`` map of a timings document, or None if it isn't one."""
    if not isinstance(data, dict) or data.get("version") != TIMINGS_VERSION:
        return None
    tests = data.get("tests")
    if not isinstance(tests, dict):
        return None
    return {str(k): float(v) for k, v in tests.items() if isinstance(v, (int, float)) and v >= 0}


def timings_document(tool: str, tests: Dict[str, float]) -> dict:
    return {"version": TIMINGS_VERSION, "tool": tool, "tests": {k: round(v, 4) for k, v in sorted(tests.items())}}


def save_timings(path: str, tool: str, tests: Dict[str, float]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(timings_document(tool, tests), f)
    os.replace(tmp, path)


def timings_key(tool: str, argv: Sequence[str], repository: str) -> str:
    """Which stored timings belong to this command: same tool, same arguments, same repository."""
    digest = hashlib.sha256()
    for piece in [tool, repository, *argv]:
        digest.update(piece.encode("utf-8", "replace") + b"\0")
    return "sha256:" + digest.hexdigest()
