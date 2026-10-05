"""``vitko runners split-tests``: run a test command as several parts, each in its own copy of the job."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import adapters, cost, plan, results
from .backends import unsplit_code, Backend, LocalBackend, PartOutcome, PartSpec, SerialBackend
from .envpolicy import declared_env

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHELL_OUTSIDE_QUOTES = set("\n;&|<>`$(){}*?!~#\\")


def has_shell_syntax(script: str) -> bool:
    """Whether a run line needs a shell: operators, expansions or globs outside quotes, or a
    leading VAR=value. Quoted text is literal (``-m "not network"``, ``-skip 'A|B'``); inside
    double quotes ``$`` and backquotes still expand."""
    if re.match(r"^\s*\w+=", script):
        return True
    quote = None
    for char in script:
        if quote == "'":
            if char == "'":
                quote = None
        elif quote == '"':
            if char == '"':
                quote = None
            elif char in "$`\\":
                return True
        elif char in "'\"":
            quote = char
        elif char in _SHELL_OUTSIDE_QUOTES:
            return True
    return quote is not None  # an unclosed quote: let the shell report it
USAGE = "vitko runners split-tests [options] -- <test command>"
#: How long to wait for room on a busy runner before running unsplit. The host stops idle
#: standbys to make room itself, so a refusal means there is no room to take: don't wait long.
DEFAULT_WAIT_S = 15
#: Pauses between asking again: short at first, then every 30 s.
WAIT_STEPS_S = (5, 10, 20, 30)


class UsageError(Exception):
    pass


def parse_args(argv: Sequence[str]) -> Tuple[argparse.Namespace, List[str]]:
    words = list(argv)
    if words[:1] == ["split-tests"]:
        words = words[1:]
    command: List[str] = []
    if "--" in words:
        cut = words.index("--")
        words, command = words[:cut], words[cut + 1 :]
    parser = argparse.ArgumentParser(
        prog="vitko runners split-tests",
        usage=USAGE,
        description="Run a test command as several parts, each in its own copy of the job.",
    )
    parser.add_argument("--parts", default="auto", help="auto, or how many parts")
    parser.add_argument("--tool", default="auto", choices=("auto",) + adapters.TOOLS)
    parser.add_argument(
        "--optimize",
        default=cost.DEFAULT_MODE,
        choices=cost.MODES,
        help="with --parts auto: what to choose the number of parts for (default %s)" % cost.DEFAULT_MODE,
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME",
        help="an environment variable the tests may see (repeat, or separate with spaces)",
    )
    parser.add_argument("--junit", metavar="PATH", help="write a JUnit XML report here")
    parser.add_argument("--timings", metavar="PATH", help="read and update test timings in this file")
    parser.add_argument("--backend", default="auto", choices=("auto", "host", "local", "serial"))
    parser.add_argument("--working-directory", default=".", metavar="DIR")
    parser.add_argument("--shell", metavar="SCRIPT", help="the test command as one string (the action's run input)")
    parser.add_argument(
        "--wait-for-capacity",
        type=int,
        default=DEFAULT_WAIT_S,
        metavar="SECONDS",
        help="when the runner has no room to split right now, keep asking for this long before "
        "running the tests unsplit (default %d; 0 to not wait)" % DEFAULT_WAIT_S,
    )
    parser.add_argument(
        "--prepare",
        metavar="SCRIPT",
        help="a shell command to run first, in this job, with network (install dependencies, build); "
        "the parts can't reach the network",
    )
    parser.add_argument("--results", metavar="PATH", help="write every test's outcome as JSON (for parity checks)")
    args = parser.parse_args(words)
    if args.parts != "auto" and not re.fullmatch(r"[1-9][0-9]{0,2}", args.parts):
        raise UsageError("--parts must be auto or a whole number from 1 to 999")
    if bool(command) == bool(args.shell):
        raise UsageError("give the test command after --, or with --shell")
    return args, command


def env_names(values: Sequence[str]) -> List[str]:
    return [name for value in values for name in re.split(r"[\s,]+", value) if name]


def resolve_command(args: argparse.Namespace, command: List[str]) -> Tuple[Optional[List[str]], Optional[str]]:
    """The command's argv, or (None, why it can't be split) for a shell script."""
    if command:
        return command, None
    script = args.shell.strip()
    if not has_shell_syntax(script):
        return shlex.split(script), None
    if args.tool == "command":
        return ["bash", "-c", script], None
    return None, "the command uses shell syntax; to split it, put a single test command in run"


def requested_parts(args: argparse.Namespace) -> Optional[int]:
    return None if args.parts == "auto" else int(args.parts)


def choose_backend(name: str, timings_key: str, args: argparse.Namespace, sleep=time.sleep,
                   clock=time.monotonic) -> Backend:
    """The backend; a LocalBackend (with the reason) when splitting isn't available here. A busy
    runner is asked again, with pauses, for up to ``--wait-for-capacity`` seconds."""
    requested = requested_parts(args)
    if name == "local":
        return LocalBackend("splitting was turned off", "turned-off", requested)
    if name == "serial":
        return SerialBackend()
    from .host import HostBackend, HostUnavailable

    deadline = clock() + max(0, args.wait_for_capacity)
    attempt = 0
    while True:
        try:
            return HostBackend.connect(timings_key=timings_key)
        except HostUnavailable as error:
            reason = str(error) or "splitting needs a Vitko runner"
            code = unsplit_code(reason)
            if name == "host":
                raise UsageError("no Vitko host to split tests on: %s" % reason) from None
            left = deadline - clock()
            if code != "host-busy" or left <= 0:
                if code == "host-busy" and args.wait_for_capacity > 0:
                    reason += "; waited %ds for room" % args.wait_for_capacity
                return LocalBackend(reason, code, requested, 0 if code == "host-busy" else None)
            pause = min(WAIT_STEPS_S[min(attempt, len(WAIT_STEPS_S) - 1)], left)
            print("This runner has no room to split the tests right now; asking again in %ds "
                  "(waiting up to %ds)." % (round(pause), args.wait_for_capacity), flush=True)
            sleep(pause)
            attempt += 1


def run_prepare(script: str, cwd: str) -> int:
    """The setup command, here in the job (with network), before anything is split."""
    print("Preparing: %s" % script, flush=True)
    code = subprocess.run(["bash", "-c", script], cwd=cwd, check=False).returncode
    if code != 0:
        message = "The prepare command failed with exit status %d; the tests did not run." % code
        print("::error::%s" % message if results.in_github_actions() else message, flush=True)
    return code


def main(argv: Sequence[str]) -> int:
    try:
        args, command = parse_args(argv)
        argv_resolved, unsplittable = resolve_command(args, command)
        cwd = os.path.abspath(args.working_directory)
        if args.prepare and args.prepare.strip():
            code = run_prepare(args.prepare, cwd)
            if code != 0:
                return code
        if argv_resolved is None:
            return LocalBackend(unsplittable or "", "shell-script", requested_parts(args)).run_unsplit(
                ["bash", "-c", args.shell], cwd)
        tool = adapters.detect(argv_resolved) if args.tool == "auto" else args.tool
        key = plan.timings_key(tool, argv_resolved, os.environ.get("GITHUB_REPOSITORY", ""))
        backend = choose_backend(args.backend, key, args)
    except UsageError as error:
        print("vitko runners split-tests: %s" % error, file=sys.stderr)
        return 2
    if isinstance(backend, LocalBackend):
        return backend.run_unsplit(argv_resolved, cwd)
    from .host import HostFailed, HostUnavailable

    try:
        return split(args, argv_resolved, cwd, tool, key, backend)
    except HostUnavailable as error:
        backend.close()
        return LocalBackend(str(error) or "the host could not split the tests", None, requested_parts(args),
                            backend.max_parts()).run_unsplit(argv_resolved, cwd)
    except HostFailed as error:
        print(
            "::error::Split tests failed: %s" % error
            if results.in_github_actions()
            else "Split tests failed: %s" % error,
            flush=True,
        )
        return 1
    finally:
        backend.close()


def stored_document(args: argparse.Namespace, backend: Backend) -> Optional[dict]:
    """The whole stored timings document (test timings and the step's run history)."""
    if args.timings:
        try:
            with open(args.timings) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    offered = backend.timings()
    return offered if isinstance(offered, dict) else None


def billed_seconds(parts: int, outcomes: List[PartOutcome], step_wall: float) -> float:
    """What the run is billed: the step's own time in one part, every copy's time when split."""
    if parts <= 1:
        return step_wall
    return sum(o.wall_ms for o in outcomes) / 1000.0 or step_wall


def load_timings(args: argparse.Namespace, backend: Backend) -> Optional[Dict[str, float]]:
    if args.timings:
        return plan.load_timings(args.timings)
    offered = backend.timings()
    if isinstance(offered, dict) and "version" in offered:
        return plan.clean_timings(offered)
    if isinstance(offered, dict):
        return plan.clean_timings({"version": plan.TIMINGS_VERSION, "tests": offered})
    return None


def on_event(total: int):
    def handle(event: dict) -> None:
        if event.get("type") == "copies":
            copy_ms = event.get("copyMs")
            print(
                "Made %d copies of this job%s."
                % (event.get("parts", total), " in %s" % results.duration(copy_ms) if copy_ms is not None else ""),
                flush=True,
            )
        elif event.get("type") == "started":
            print("Part %d of %d started." % (event["part"], total), flush=True)
        elif event.get("type") == "part":
            status = "finished" if event.get("exit") == 0 else "finished with exit status %s" % event.get("exit")
            print(
                "Part %d of %d %s after %s."
                % (event["part"], total, status, results.duration(event.get("wallMs") or 0)),
                flush=True,
            )

    return handle


def split(args: argparse.Namespace, argv: List[str], cwd: str, tool: str, key: str, backend: Backend) -> int:
    started = time.monotonic()
    names = env_names(args.env)
    env, rejected = declared_env(os.environ, names)
    for name in rejected:
        print("Not passing %s to the tests: variables named like this usually hold secrets." % name, flush=True)
    base = os.environ.get("RUNNER_TEMP") if os.path.isdir(os.environ.get("RUNNER_TEMP", "")) else None
    plan_dir = tempfile.mkdtemp(prefix="vitko-split-", dir=base)
    os.chmod(plan_dir, 0o755)
    try:
        ctx = adapters.Context(
            argv=argv, cwd=cwd, env=env, plan_dir=plan_dir, home=HOME, backend=backend, uid=os.getuid(), gid=os.getgid()
        )
        adapter = adapters.make(tool, ctx)
        stored = stored_document(args, backend)
        timings = load_timings(args, backend)
        prepared = adapter.prepare()
        if prepared.exit_code is not None:
            return prepared.exit_code
        requested = None if args.parts == "auto" else int(args.parts)
        asked, one_part_reason, notes = requested, "", []
        if requested is None and tool == "command" and not uses_part_variables(argv):
            one_part_reason = "command-not-split"
            notes.append("Running in one part: the command doesn't use VITKO_PART or VITKO_PARTS, so every part "
                         "would run all of it. Run a test tool this action splits (pytest, cargo nextest, jest, "
                         "vitest, go test), or split with those variables and set parts.")
            # Every part would run the whole command: more time and money, nothing split.
            requested = 1
        runs = cost.clean_runs(stored)
        parts, by_cost = None, False
        if requested is None and backend.stores_unsplit():
            limit = plan.choose_parts(None, backend.max_parts(), prepared.unit_count)
            parts, why = cost.decide(runs, limit, args.optimize)
            if parts is not None:
                by_cost = True
                one_part_reason = why if parts == 1 else ""
                note = cost.explain(runs, parts, limit, args.optimize)
                print(note, flush=True)
                if parts == 1:
                    notes.append(note)
        if parts is None:
            parts = plan.choose_parts(requested, backend.max_parts(), prepared.unit_count, timings,
                                      prepared.units or (), adapter.unit_guess)
        groups = plan.partition(prepared.units, parts, timings) if prepared.units is not None else None
        announce(tool, parts, prepared, timings)
        if requested is None and parts == 1 and backend.max_parts() > 1 and not by_cost:
            found = plan.suite_estimate(prepared.units or (), timings, adapter.unit_guess)
            if found is not None and found[0] < plan.SPLIT_MIN_SECONDS:
                one_part_reason = "short-suite"
                notes.append(short_suite_notice(tool, found, len(prepared.units or ())))
        if requested is not None and requested > parts and backend.max_parts() < requested:
            capped = "Asked for %d parts; this runner allowed %d." % (requested, backend.max_parts())
            print(capped, flush=True)
            notes.append(capped)
        specs = adapter.specs(groups, parts)
        runner = backend if parts > 1 else SerialBackend()
        running = time.monotonic()
        outcomes = runner.run_parts(plan_dir, specs, on_event(parts))
        step_wall = time.monotonic() - running
        report = build_report(adapter, prepared, specs, outcomes, int((time.monotonic() - started) * 1000))
        report.notes.extend(notes + adapter.notes(report))
        report.parts_requested, report.parts_allowed, report.one_part_reason = asked, backend.max_parts(), one_part_reason
        results.print_report(report)
        results.publish(report, args.junit)
        if args.results:
            with open(args.results, "w") as f:
                json.dump(report.outcomes(), f, indent=1, sort_keys=True)
        if report.ok():
            runs = cost.add_run(runs, parts, billed_seconds(parts, outcomes, step_wall), step_wall)
        keep_timings(args, backend, tool, key, timings, adapter.timings(report.results), runs)
        return 0 if report.ok() else 1
    finally:
        shutil.rmtree(plan_dir, ignore_errors=True)


def uses_part_variables(argv: Sequence[str]) -> bool:
    """Whether a command for ``--tool command`` splits itself with VITKO_PART / VITKO_PARTS."""
    return any("VITKO_PART" in word for word in argv)


def short_suite_notice(tool: str, found: Tuple[float, str], count: int) -> str:
    seconds, source = found
    noun = "packages" if tool == "go" else "tests"
    basis = "from past timings" if source == "timings" else "from the number of %s (%d)" % (noun, count)
    return ("Running in one part: these tests take about %ds (%s), and splitting pays off from about %ds. "
            "Use --parts N to split anyway." % (round(seconds), basis, round(plan.SPLIT_MIN_SECONDS)))


def announce(tool: str, parts: int, prepared: adapters.Prepared, timings: Optional[Dict[str, float]]) -> None:
    if prepared.units is None:
        what = "the test files"
    else:
        noun = "packages" if tool == "go" else "tests"
        what = "%d %s" % (len(prepared.units), noun)
    how = " by past timings" if timings and prepared.units else ""
    print("Splitting %s into %d %s%s." % (what, parts, "part" if parts == 1 else "parts", how), flush=True)


def build_report(
    adapter: adapters.Adapter,
    prepared: adapters.Prepared,
    specs: List[PartSpec],
    outcomes: List[PartOutcome],
    wall_ms: int,
) -> results.Report:
    by_part = {o.part: o for o in outcomes}
    parts = []
    for spec in specs:
        outcome = by_part.get(spec.part) or PartOutcome(spec.part, 1, "This part did not run.\n", {}, 0)
        parsed = adapter.parse(spec, outcome)
        problem = parsed.problem or results.part_problem(outcome, len(specs), parsed.results, adapter.expect_results)
        parts.append(results.PartReport(outcome, parsed.results, parsed.units, problem))
    report = results.Report(tool=adapter.tool, parts=parts, planned=prepared.planned, wall_ms=wall_ms)
    report.extra_problems = adapter.extra_problems(report.results)
    return report


def keep_timings(
    args: argparse.Namespace,
    backend: Backend,
    tool: str,
    key: str,
    old: Optional[Dict[str, float]],
    new: Dict[str, float],
    runs: Optional[List[dict]] = None,
) -> None:
    if not new and not runs:
        return
    merged = dict(old or {})
    merged.update(new)
    if args.timings:
        plan.save_timings(args.timings, tool, merged, runs)
    backend.store_timings(key, plan.timings_document(tool, merged, runs))
