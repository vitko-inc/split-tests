"""pytest plugin for split tests: collect once, then run one part of the plan.

Loaded with ``-p vitko_split.pytest_plugin`` and active only when VITKO_SPLIT_DIR is set.

1. After collection it writes ``<dir>/collected.json`` (node ids, in collection order).
2. With VITKO_SPLIT_COLLECT_ONLY=1 it stops there (exit 0).
3. Otherwise it waits for its part number: VITKO_SPLIT_ASSIGN, or the file ``<dir>/assign``,
   which is written in each copy of the job. That wait is where the job is copied, so the
   (possibly slow) collection happens once.
4. It keeps the items of its part (``<dir>/plan.json``: ``{"parts": [[ids of part 1], ...]}``),
   starts ``w`` local worker processes from the collected session when pytest-xdist's ``-n`` asked for
   them (xdist itself is switched off), and records one outcome per test: the worst of its
   setup, call and teardown.
5. It writes ``<dir>/results-<k>.jsonl`` and ``<dir>/summary-<k>.json`` and exits non-zero when a
   test failed or when the results don't cover exactly the tests of the part.
"""

import json
import os
import random
import time

import pytest

OUTCOME_RANK = {"passed": 0, "skipped": 1, "xfailed": 2, "xpassed": 3, "failed": 4, "error": 5}
EXCERPT_CHARS = 20000

_state = {}


def _dir():
    return os.environ.get("VITKO_SPLIT_DIR")


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _wait_for_part(d):
    assigned = os.environ.get("VITKO_SPLIT_ASSIGN")
    if assigned:
        return int(assigned)
    path = os.path.join(d, "assign")
    while not os.path.exists(path):
        time.sleep(0.05)
    while True:  # the writer may not have finished the line yet
        text = open(path).read().strip()
        if text:
            return int(text)
        time.sleep(0.05)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Take pytest-xdist's worker count for our own local workers and switch xdist off.

    xdist's own configure hook runs last, so clearing its options here keeps it from starting."""
    if not _dir():
        return
    workers = getattr(config.option, "numprocesses", None)
    _state["xdist_workers"] = workers if isinstance(workers, int) and workers > 0 else 1
    if workers:
        config.option.numprocesses = 0
        config.option.dist = "no"
        config.option.tx = []


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    d = _dir()
    if not d or config.getoption("collectonly"):
        return
    os.makedirs(d, exist_ok=True)
    _write_json(os.path.join(d, "collected.json"), [item.nodeid for item in items])
    if os.environ.get("VITKO_SPLIT_COLLECT_ONLY") == "1":
        pytest.exit("collected", returncode=0)
    k = _wait_for_part(d)
    # Each part gets its own random state.
    random.seed(os.urandom(32))
    with open(os.path.join(d, "plan.json")) as f:
        plan = json.load(f)
    wanted = plan["parts"][k - 1]
    wanted_set = set(wanted)
    mine = [item for item in items if item.nodeid in wanted_set]
    workers = max(1, min(plan.get("workers") or _state.get("xdist_workers", 1), len(mine) or 1))
    j = 0
    for sub in range(1, workers):
        if os.fork() == 0:
            j = sub
            break
    keep = mine[j::workers]
    _state.update(
        k=k,
        j=j,
        workers=workers,
        planned=wanted,
        started=time.time(),
        final={},
        seconds={},
        excerpt={},
        out=open(os.path.join(d, "results-%d-%d.jsonl" % (k, j)), "w"),
    )
    kept = set(id(item) for item in keep)
    deselected = [item for item in items if id(item) not in kept]
    items[:] = keep
    if deselected:
        config.hook.pytest_deselected(items=deselected)


def _outcome(report):
    if hasattr(report, "wasxfail"):
        return "xfailed" if report.skipped else "xpassed"
    if report.when != "call" and report.failed:
        return "error"
    return report.outcome  # passed, failed or skipped


def pytest_runtest_logreport(report):
    if "k" not in _state:
        return
    nodeid = report.nodeid
    _state["seconds"][nodeid] = _state["seconds"].get(nodeid, 0.0) + (report.duration or 0.0)
    if report.when != "call" and report.passed:
        return  # a passing setup or teardown says nothing about the outcome
    outcome = _outcome(report)
    previous = _state["final"].get(nodeid)
    if previous is None or OUTCOME_RANK[outcome] > OUTCOME_RANK[previous]:
        _state["final"][nodeid] = outcome
    if outcome in ("failed", "error"):
        text = getattr(report, "longreprtext", "") or ""
        _state["excerpt"][nodeid] = (_state["excerpt"].get(nodeid, "") + text)[-EXCERPT_CHARS:]


def pytest_runtest_logfinish(nodeid, location):
    if "k" not in _state:
        return
    record = {
        "nodeid": nodeid,
        "outcome": _state["final"].get(nodeid, "error"),
        "seconds": round(_state["seconds"].get(nodeid, 0.0), 4),
    }
    if nodeid in _state["excerpt"]:
        record["output"] = _state["excerpt"][nodeid]
    _state["out"].write(json.dumps(record) + "\n")
    _state["out"].flush()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if "k" not in _state:
        return
    _state["out"].close()
    if _state["j"] != 0:
        os._exit(0 if exitstatus in (0, 1, 5) else 3)
    worst = 0
    for _ in range(_state["workers"] - 1):
        _, status = os.wait()
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
        worst = max(worst, code)
    d, k = _dir(), _state["k"]
    merged, seen, counts = [], set(), {}
    for j in range(_state["workers"]):
        with open(os.path.join(d, "results-%d-%d.jsonl" % (k, j))) as f:
            for line in f:
                record = json.loads(line)
                if record["nodeid"] in seen:
                    continue
                seen.add(record["nodeid"])
                merged.append(record)
                counts[record["outcome"]] = counts.get(record["outcome"], 0) + 1
    tmp = os.path.join(d, "results-%d.jsonl.tmp" % k)
    with open(tmp, "w") as f:
        f.writelines(json.dumps(record) + "\n" for record in merged)
    os.replace(tmp, os.path.join(d, "results-%d.jsonl" % k))
    missing = [nodeid for nodeid in _state["planned"] if nodeid not in seen]
    summary = {
        "part": k,
        "workers": _state["workers"],
        "planned": len(_state["planned"]),
        "reported": len(merged),
        "missing": missing[:100],
        "complete": not missing,
        "counts": counts,
        "seconds": round(time.time() - _state["started"], 2),
    }
    _write_json(os.path.join(d, "summary-%d.json" % k), summary)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    line = "Part %d: %s in %.1fs" % (
        k,
        ", ".join("%d %s" % (v, o) for o, v in sorted(counts.items())) or "no tests",
        summary["seconds"],
    )
    if missing:
        line += "; %d planned tests did not run" % len(missing)
    if reporter is not None:
        reporter.write("\n")  # pytest's progress line has no newline of its own
        reporter.write_line(line)
    if worst or missing or counts.get("failed") or counts.get("error"):
        session.exitstatus = 1
    elif not merged:
        session.exitstatus = 5 if not _state["planned"] else 1
    else:
        session.exitstatus = 0
