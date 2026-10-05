"""Where the parts run.

- ``LocalBackend``: not splitting at all. The command runs once, in the step, with the step's
  environment, and its exit status is the step's. Used off Vitko Runners.
- ``SerialBackend``: the parts run one after another on this machine, with the same plan,
  parsing and merge as on a Vitko runner. Used by tests, ``--backend serial`` and to run a single
  part in place.
- ``HostBackend`` (``host.py``): each part runs in its own copy of the job, made by the host.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

EventSink = Callable[[dict], None]


@dataclass
class PartSpec:
    """How to run one part. Serialised to ``<plan>/parts/<k>.json`` (the host protocol, vitko-split-v1)."""

    part: int
    parts: int
    argv: List[str]
    cwd: str
    env: Dict[str, str]
    uid: Optional[int] = None
    gid: Optional[int] = None
    result_files: List[str] = field(default_factory=list)
    collect_once: Optional[dict] = None  # {"assignFile": ..., "pid": ..., "logFile": ...}

    def to_json(self) -> dict:
        return {
            "argv": self.argv,
            "cwd": self.cwd,
            "env": self.env,
            "uid": self.uid,
            "gid": self.gid,
            "resultFiles": self.result_files,
            "collectOnce": self.collect_once,
            "part": self.part,
            "parts": self.parts,
        }


@dataclass
class PartOutcome:
    part: int
    exit: int
    output: str
    files: Dict[str, bytes]
    wall_ms: int
    cpu_ms: Optional[int] = None
    checks: dict = field(default_factory=dict)


class Backend:
    name = "backend"
    #: True when parts run in copies of this job made after the adapter's preparation, so a
    #: process started before the split (pytest's collection) continues in every part.
    copies_job = False

    def max_parts(self) -> int:
        raise NotImplementedError

    def timings(self) -> Optional[dict]:
        return None

    def start_collector(self, argv: List[str], cwd: str, env: Dict[str, str], log_path: str) -> int:
        raise NotImplementedError

    def run_parts(self, plan_dir: str, specs: List[PartSpec], on_event: EventSink) -> List[PartOutcome]:
        raise NotImplementedError

    def collector_status(self, pid: int) -> Optional[int]:
        """None while the collector started by ``start_collector`` runs, else its exit status."""
        return None if _pid_alive(pid) else 1

    def store_timings(self, key: str, data: dict) -> None:
        return None

    def stores_unsplit(self) -> bool:
        """Whether timings of a run that wasn't split are kept (the cost model needs them)."""
        return False

    def close(self) -> None:
        return None


def write_plan_dir(plan_dir: str, specs: List[PartSpec]) -> None:
    os.makedirs(os.path.join(plan_dir, "parts"), exist_ok=True)
    for spec in specs:
        with open(os.path.join(plan_dir, "parts", "%d.json" % spec.part), "w") as f:
            json.dump(spec.to_json(), f)


#: Why a run went unsplit, as the ``unsplit-reason`` output (stable words for workflows).
UNSPLIT_REASONS = {
    "host-busy": "this runner had no room to split the tests",
    "not-vitko": "splitting needs a Vitko runner",
    "setup": "this job can't start the helper that makes copies",
    "host-error": "the runner couldn't make copies of this job",
    "turned-off": "splitting was turned off",
    "shell-script": "the run line is a shell script",
}


def unsplit_code(reason: str) -> str:
    """A stable word for a reason the host or the setup gave."""
    text = reason.lower()
    if "busy" in text or "the limit" in text:
        return "host-busy"
    if "needs a vitko runner" in text:
        return "not-vitko"
    if "sudo" in text or "systemd" in text or "helper" in text or "collection" in text:
        return "setup"
    if "turned off" in text:
        return "turned-off"
    if "shell syntax" in text:
        return "shell-script"
    return "host-error"


class LocalBackend(Backend):
    """Runs the whole command once, unsplit, exactly as the step would have, and says why."""

    name = "local"

    def __init__(self, reason: str, code: Optional[str] = None, requested: Optional[int] = None,
                 allowed: Optional[int] = None) -> None:
        self.reason = reason
        self.code = code or unsplit_code(reason)
        self.requested, self.allowed = requested, allowed

    def run_unsplit(self, argv: List[str], cwd: str) -> int:
        print("Running all tests in this job (%s)." % self.reason, flush=True)
        report_unsplit(self.code, self.reason, self.requested, self.allowed)
        try:
            code = subprocess.call(argv, cwd=cwd)
        except FileNotFoundError:
            print("Command not found: %s" % argv[0], file=sys.stderr, flush=True)
            code = 127
        _unsplit_outputs(self.code, self.requested, self.allowed)
        return code


def report_unsplit(code: str, reason: str, requested: Optional[int], allowed: Optional[int]) -> None:
    """Say, where people look (annotations and the run's summary), that the tests ran unsplit."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    asked = "; parts asked for: %s" % (requested if requested else "auto")
    room = ", allowed: %d" % allowed if allowed is not None else ""
    print("::notice title=Tests ran unsplit::All tests ran in this job: %s (%s%s%s)."
          % (reason, code, asked, room), flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Split tests: ran unsplit\n\nAll tests ran in this job, in one part.\n\n"
                    "| Reason | Code | Parts asked for | Parts allowed |\n|---|---|---:|---:|\n"
                    "| %s | `%s` | %s | %s |\n\n" % (reason, code, requested or "auto",
                                                   allowed if allowed is not None else "-"))


def _unsplit_outputs(code: str = "", requested: Optional[int] = None, allowed: Optional[int] = None) -> None:
    """Step outputs for an unsplit run: one part, why, and no report (the tests' own output is it)."""
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write("junit=\nparts=1\nfailed=\nunsplit-reason=%s\nparts-requested=%s\nparts-allowed=%s\n"
                    % (code, requested or "auto", "" if allowed is None else allowed))


def _children_cpu_ms() -> int:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return int((usage.ru_utime + usage.ru_stime) * 1000)


def _read_files(paths: List[str]) -> Dict[str, bytes]:
    files = {}
    for path in paths:
        try:
            with open(path, "rb") as f:
                files[path] = f.read()
        except OSError:
            pass
    return files


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return False


class SerialBackend(Backend):
    """Runs the parts one after another on this machine."""

    name = "serial"

    def __init__(self, max_parts: int = 8) -> None:
        self._max_parts = max_parts
        self._collectors: Dict[int, subprocess.Popen] = {}

    def max_parts(self) -> int:
        return self._max_parts

    def start_collector(self, argv: List[str], cwd: str, env: Dict[str, str], log_path: str) -> int:
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
        self._collectors[proc.pid] = proc
        return proc.pid

    def collector_status(self, pid: int) -> Optional[int]:
        proc = self._collectors.get(pid)
        return super().collector_status(pid) if proc is None else proc.poll()

    def run_parts(self, plan_dir: str, specs: List[PartSpec], on_event: EventSink) -> List[PartOutcome]:
        write_plan_dir(plan_dir, specs)
        outcomes = []
        for spec in specs:
            on_event({"type": "started", "part": spec.part})
            outcome = self._run_collect_once(spec) if spec.collect_once else self._run(spec, on_event)
            on_event({"type": "part", "part": spec.part, "exit": outcome.exit, "wallMs": outcome.wall_ms})
            outcomes.append(outcome)
        return outcomes

    def _run(self, spec: PartSpec, on_event: EventSink) -> PartOutcome:
        started, cpu = time.monotonic(), _children_cpu_ms()
        chunks = []
        try:
            proc = subprocess.Popen(
                spec.argv, cwd=spec.cwd, env=spec.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
        except FileNotFoundError:
            text = "Command not found: %s\n" % spec.argv[0]
            return PartOutcome(spec.part, 127, text, {}, 0, 0)
        with proc:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                text = raw.decode("utf-8", "replace")
                chunks.append(text)
                on_event({"type": "output", "part": spec.part, "data": text})
            code = proc.wait()
        return PartOutcome(
            spec.part,
            code,
            "".join(chunks),
            _read_files(spec.result_files),
            int((time.monotonic() - started) * 1000),
            _children_cpu_ms() - cpu,
        )

    def _run_collect_once(self, spec: PartSpec) -> PartOutcome:
        """Hand the waiting collector its part and wait for it (a single part run in place)."""
        info = spec.collect_once or {}
        started = time.monotonic()
        with open(info["assignFile"] + ".tmp", "w") as f:
            f.write("%d\n" % spec.part)
        os.replace(info["assignFile"] + ".tmp", info["assignFile"])
        pid = int(info["pid"])
        proc = self._collectors.pop(pid, None)
        if proc is not None:
            code = proc.wait()
        else:
            while _pid_alive(pid):
                time.sleep(0.1)
            code = _exit_code_from(info)
        with open(info["logFile"], "rb") as f:
            output = f.read().decode("utf-8", "replace")
        return PartOutcome(
            spec.part, code, output, _read_files(spec.result_files), int((time.monotonic() - started) * 1000)
        )


def _exit_code_from(info: dict) -> int:
    """The exit status of a collector that isn't our child (the helper started it)."""
    path = info.get("exitFile")
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except ValueError:
            pass
    return 0


__all__ = ["Backend", "EventSink", "LocalBackend", "PartOutcome", "PartSpec", "SerialBackend", "write_plan_dir"]
