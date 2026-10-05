"""``parts: auto`` by cost: how many parts to run, from how long this step took before.

Splitting is billed as the time every copy runs, in place of the step's own time. Each copy pays
a fixed start cost (the copy itself, the test tool starting, loading and transforming the code
again), so more parts buy wall time with billed time. This module keeps a short history of the
step's runs (how many parts, the billed seconds, the wall seconds) and picks the part count with
the lowest

    billed(k) + weight x wall(k)

where ``weight`` is how many billed seconds one second of wall time is worth:

- ``cost``: 0.1. Split only when it costs about the same or less than one part.
- ``balanced`` (default): 1. Split when every extra billed second saves at least a second of wall time.
- ``speed``: 10. Split for wall time, paying up to 10 billed seconds per second saved.

The model, from the history (medians of the latest runs):

- ``U``: the step's time in one part (runs that ran unsplit in the job).
- ``o``: the extra billed seconds per extra part, ``(billed(k) - U) / (k - 1)`` from split runs,
  never below 0 when extrapolating.
- ``billed(k) = billed(j) + (k - j) * o`` from the nearest part count j < k seen (or U), and ``wall(k) = billed(k) / k * r``, where ``r`` is how far the
  split step's wall time was above the average part (copy start, uneven parts; 1.15 by default).
  A part count seen before uses what was seen.

With no history the step first runs unsplit once to measure ``U``, then once in two parts to
measure ``o``, then follows the model. Both measurements are kept, so a step that stopped
splitting keeps knowing what splitting costs.
"""

from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Sequence, Tuple

MODES = ("cost", "balanced", "speed")
DEFAULT_MODE = "balanced"
#: Billed seconds that one second of wall time is worth, per mode.
WALL_WEIGHT = {"cost": 0.1, "balanced": 1.0, "speed": 10.0}
#: Runs kept in the history (the latest run of each part count is always kept as well).
RUNS_KEPT = 20
#: Medians over at most this many of the latest runs of one part count.
RECENT = 5
#: Split step wall time over the average part's time, before any split was seen.
DEFAULT_WALL_RATIO = 1.15
#: The part count used to measure the per-part cost the first time.
PROBE_PARTS = 2


def clean_runs(data: object) -> List[dict]:
    """The valid run records of a timings document (oldest first)."""
    runs = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return []
    out = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        try:
            parts, billed, wall = int(run["parts"]), float(run["billed"]), float(run["wall"])
        except (KeyError, TypeError, ValueError):
            continue
        if parts >= 1 and billed > 0 and wall > 0:
            out.append({"parts": parts, "billed": billed, "wall": wall})
    return out


def add_run(runs: Sequence[dict], parts: int, billed: float, wall: float) -> List[dict]:
    """The history with one more run, trimmed to RUNS_KEPT, never dropping the latest run of a part
    count (so the cost of splitting is remembered while the step runs unsplit)."""
    runs = list(runs) + [{"parts": int(parts), "billed": round(float(billed), 2), "wall": round(float(wall), 2)}]
    while len(runs) > RUNS_KEPT:
        for i, run in enumerate(runs):
            if any(later["parts"] == run["parts"] for later in runs[i + 1:]):
                del runs[i]
                break
        else:
            break
    return runs


class Model:
    """What the history says about this step."""

    def __init__(self, runs: Sequence[dict]) -> None:
        self.by_parts: Dict[int, List[dict]] = {}
        for run in runs:
            self.by_parts.setdefault(run["parts"], []).append(run)
        one = self._recent(1)
        self.unsplit: Optional[float] = statistics.median(r["billed"] for r in one) if one else None
        split = [r for k, rs in self.by_parts.items() if k > 1 for r in rs[-RECENT:]]
        self.per_part: Optional[float] = None
        self.wall_ratio = DEFAULT_WALL_RATIO
        if split:
            self.wall_ratio = statistics.median(r["wall"] / (r["billed"] / r["parts"]) for r in split)
            if self.unsplit is not None:
                self.per_part = statistics.median((r["billed"] - self.unsplit) / (r["parts"] - 1) for r in split)

    def _recent(self, parts: int) -> List[dict]:
        return self.by_parts.get(parts, [])[-RECENT:]

    def ready(self) -> bool:
        return self.unsplit is not None and self.per_part is not None

    def billed(self, parts: int) -> float:
        seen = self._recent(parts)
        if seen:
            return statistics.median(r["billed"] for r in seen)
        assert self.unsplit is not None and self.per_part is not None
        # From the nearest part count seen below this one (or one part), adding o per part.
        below = [k for k in self.by_parts if k < parts]
        anchor = max(below) if below else 1
        start = statistics.median(r["billed"] for r in self._recent(anchor)) if below else self.unsplit
        return start + (parts - anchor) * max(0.0, self.per_part)

    def wall(self, parts: int) -> float:
        seen = self._recent(parts)
        if seen:
            return statistics.median(r["wall"] for r in seen)
        return self.billed(parts) / parts * self.wall_ratio

    def best(self, limit: int, mode: str) -> int:
        weight = WALL_WEIGHT[mode]
        scores = [(self.billed(k) + weight * self.wall(k), k) for k in range(1, max(1, limit) + 1)]
        return min(scores)[1]  # ties: fewer parts


#: Why the cost model chose one part (the action's ``unsplit-reason``).
LEARNING = "learning"
COSTS_MORE = "costs-more"


def decide(runs: Sequence[dict], limit: int, mode: str) -> Tuple[Optional[int], str]:
    """(parts, why one part) by the step's history, or (None, "") to fall back to the size rule.

    ``limit``: the most parts allowed here. Speed mode falls back to the size rule until the
    unsplit time is known (it splits from the first run)."""
    model = Model(runs)
    if limit <= 1:
        return None, ""
    if model.unsplit is None:
        if mode == "speed":
            return None, ""
        return 1, LEARNING
    if model.per_part is None:
        # Measure splitting once, also for a short step: some steps cost less split (a copy of a
        # warm job skips start-up work the step does), and only a measurement tells.
        return min(PROBE_PARTS, limit), ""
    parts = model.best(limit, mode)
    return parts, (COSTS_MORE if parts == 1 else "")


def explain(runs: Sequence[dict], parts: int, limit: int, mode: str) -> str:
    """One line for the log: what the model expected for the chosen part count and for one part."""
    model = Model(runs)
    if not model.ready():
        if model.unsplit is None:
            return ("Running in one part this time to measure the unsplit time; later runs choose the "
                    "number of parts by cost (optimize: %s)." % mode)
        return ("Running in %d parts this time to measure what each extra part costs (unsplit: about %ds)."
                % (parts, round(model.unsplit)))
    one = "unsplit about %ds" % round(model.unsplit or 0)
    if parts == 1:
        k = min(limit, max(2, PROBE_PARTS))
        return ("Running in one part: splitting would cost more than it saves (optimize: %s). %s; "
                "%d parts about %ds billed, %ds wall. Set parts to a number to split anyway."
                % (mode, one[0].upper() + one[1:], k, round(model.billed(k)), round(model.wall(k))))
    return ("Chose %d parts (optimize: %s): about %ds billed, %ds wall; %s."
            % (parts, mode, round(model.billed(parts)), round(model.wall(parts)), one))
