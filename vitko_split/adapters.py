"""Test tools: how to list a suite, run one part of it, and read what it reported."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .backends import Backend, PartOutcome, PartSpec
from .results import TestResult

TOOLS = ("pytest", "nextest", "jest", "vitest", "go", "command")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass
class Context:
    argv: List[str]
    cwd: str
    env: Dict[str, str]  # the declared environment
    plan_dir: str
    home: str  # the directory holding the vitko_split package
    backend: Backend
    uid: Optional[int] = None
    gid: Optional[int] = None


@dataclass
class Prepared:
    units: Optional[List[str]] = None  # what we split (tests or packages); None: the tool splits itself
    planned: Optional[List[str]] = None  # what must report for the run to be complete
    unit_count: Optional[int] = None  # at most this many parts make sense
    exit_code: Optional[int] = None  # preparation failed: stop with this status


@dataclass
class Parsed:
    results: List[TestResult]
    units: Set[str] = field(default_factory=set)
    problem: Optional[str] = None


class Adapter:
    tool = "command"
    #: Seconds per unit (test or package) assumed when there are no past timings, for
    #: ``--parts auto``'s short-suite rule; None: no guess (the tool shards by itself).
    unit_guess: Optional[float] = None
    #: Parts report per-test results, so a part that fails without any is broken.
    expect_results = True

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def prepare(self) -> Prepared:
        return Prepared()

    def specs(self, groups: Optional[List[List[str]]], parts: int) -> List[PartSpec]:
        raise NotImplementedError

    def parse(self, spec: PartSpec, outcome: PartOutcome) -> Parsed:
        raise NotImplementedError

    def timings(self, results: Sequence[TestResult]) -> Dict[str, float]:
        return {r.id: r.seconds for r in results if r.seconds > 0}

    def notes(self, report) -> List[str]:
        """Advice for the summary after a run (not problems: they don't fail the step)."""
        return []

    def extra_problems(self, results: Sequence[TestResult]) -> List[str]:
        return []

    def spec(
        self,
        part: int,
        parts: int,
        argv: List[str],
        env: Optional[Dict[str, str]] = None,
        result_files: Sequence[str] = (),
        collect_once: Optional[dict] = None,
    ) -> PartSpec:
        return PartSpec(
            part=part,
            parts=parts,
            argv=list(argv),
            cwd=self.ctx.cwd,
            env=dict(self.ctx.env if env is None else env),
            uid=self.ctx.uid,
            gid=self.ctx.gid,
            result_files=list(result_files),
            collect_once=collect_once,
        )

    def run_before_split(
        self, argv: List[str], env: Optional[Dict[str, str]] = None, show_output: bool = True
    ) -> Tuple[int, str]:
        """Run a listing/build command in the job, with the declared environment. Its stderr (the
        build) goes to the log as it happens; its stdout is returned."""
        try:
            proc = subprocess.run(
                argv,
                cwd=self.ctx.cwd,
                env=self.ctx.env if env is None else env,
                stdout=subprocess.PIPE,
                stderr=None if show_output else subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print("Command not found: %s" % argv[0], flush=True)
            return 127, ""
        return proc.returncode, proc.stdout.decode("utf-8", "replace")


# ---- detection --------------------------------------------------------------------------------


def _base(word: str) -> str:
    return os.path.basename(word)


#: Options of runners like ``uv run`` that take a value (``uv run --with X pytest``).
WRAPPER_VALUE_OPTIONS = frozenset([
    "--with", "--with-editable", "--with-requirements", "--python", "-p", "--package", "--extra",
    "--group", "--only-group", "--no-group", "--project", "--directory", "--env-file", "--index",
    "--default-index", "--index-url", "--extra-index-url", "--find-links", "-f", "--config-file",
])


def detect(argv: Sequence[str]) -> str:
    """Which tool a command runs (``command`` when we can't tell). Runner words and their options
    are skipped: ``uv run --locked pytest``, ``poetry run pytest`` and ``npx jest`` are found."""
    words = list(argv)
    wrapped = False
    while words:
        word = words[0]
        if _base(word) in ("npx", "pnpx", "bunx", "uv", "poetry", "pipenv", "env") or word in ("run", "exec", "--"):
            wrapped = True
            words = words[1:]
        elif "=" in word and not word.startswith("-"):
            words = words[1:]
        elif wrapped and word.startswith("-"):
            takes_value = "=" not in word and word in WRAPPER_VALUE_OPTIONS
            words = words[2:] if takes_value else words[1:]
        else:
            break
    if words and _base(words[0]) in ("yarn", "pnpm", "npm", "bun") and len(words) > 1:
        words = words[1:]
        if words and words[0] in ("exec", "run", "x", "dlx"):
            words = words[1:]
    if not words:
        return "command"
    head = _base(words[0])
    if head in ("pytest", "py.test"):
        return "pytest"
    if re.match(r"^python[0-9.]*$", head) and words[1:3] == ["-m", "pytest"]:
        return "pytest"
    if head == "cargo" or head == "cargo-nextest":
        rest = [w for w in words[1:] if not w.startswith("+")]
        if rest[:1] == ["nextest"] and len(rest) > 1 and rest[1] in ("run", "r"):
            return "nextest"
    if head == "jest" or (head == "node" and any(_base(w) == "jest" for w in words[1:3])):
        return "jest"
    if head == "vitest":
        return "vitest"
    if head == "go" and words[1:2] == ["test"]:
        return "go"
    return "command"


def make(tool: str, ctx: Context) -> Adapter:
    return {
        "pytest": PytestAdapter,
        "nextest": NextestAdapter,
        "jest": JestAdapter,
        "vitest": VitestAdapter,
        "go": GoAdapter,
        "command": CommandAdapter,
    }[tool](ctx)


# ---- command: the command splits itself -------------------------------------------------------


class CommandAdapter(Adapter):
    tool = "command"
    expect_results = False

    def specs(self, groups, parts):
        return [
            self.spec(k, parts, self.ctx.argv, dict(self.ctx.env, VITKO_PART=str(k), VITKO_PARTS=str(parts)))
            for k in range(1, parts + 1)
        ]

    def parse(self, spec, outcome):
        result = TestResult(
            id="part %d of %d" % (spec.part, spec.parts),
            outcome="passed" if outcome.exit == 0 else "failed",
            seconds=outcome.wall_ms / 1000.0,
            part=spec.part,
            suite="split-tests",
            output="" if outcome.exit == 0 else outcome.output[-20000:],
        )
        return Parsed([result])

    def timings(self, results):
        return {}


# ---- pytest: collect once ---------------------------------------------------------------------


class PytestAdapter(Adapter):
    tool = "pytest"
    unit_guess = 0.1  # seconds per test, when there are no past timings

    def __init__(self, ctx):
        super().__init__(ctx)
        self.dir = os.path.join(ctx.plan_dir, "pytest")
        os.makedirs(self.dir, exist_ok=True)
        self.argv = list(ctx.argv) + ["-p", "vitko_split.pytest_plugin"]
        path = [ctx.home] + ([ctx.env["PYTHONPATH"]] if ctx.env.get("PYTHONPATH") else [])
        self.env = dict(ctx.env, VITKO_SPLIT_DIR=self.dir, PYTHONPATH=os.pathsep.join(path))
        self.collector: Optional[dict] = None

    def _collected(self) -> Optional[List[str]]:
        path = os.path.join(self.dir, "collected.json")
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def prepare(self):
        if self.ctx.backend.copies_job:
            return self._prepare_collector()
        code, out = self.run_before_split(self.argv, dict(self.env, VITKO_SPLIT_COLLECT_ONLY="1"))
        ids = self._collected()
        if ids is None:
            print(out, end="", flush=True)
            return Prepared(exit_code=code or 1)
        return Prepared(units=ids, planned=ids, unit_count=len(ids))

    def _prepare_collector(self) -> Prepared:
        """Collect once in a process that the copies of the job inherit, waiting to be told its part."""
        log = os.path.join(self.dir, "collector.log")
        backend = self.ctx.backend
        pid = backend.start_collector(self.argv, self.ctx.cwd, self.env, log)
        self.collector = {"assignFile": os.path.join(self.dir, "assign"), "pid": pid, "logFile": log}
        while True:
            ids = self._collected()
            if ids is not None:
                return Prepared(units=ids, planned=ids, unit_count=len(ids))
            status = backend.collector_status(pid)
            if status is not None:
                if self._collected() is not None:
                    continue
                with open(log, errors="replace") as f:
                    print(f.read(), end="", flush=True)
                return Prepared(exit_code=status or 1)
            time.sleep(0.05)

    def specs(self, groups, parts):
        assert groups is not None
        with open(os.path.join(self.dir, "plan.json"), "w") as f:
            json.dump({"parts": groups, "workers": None}, f)
        specs = []
        for k in range(1, parts + 1):
            files = [os.path.join(self.dir, "results-%d.jsonl" % k), os.path.join(self.dir, "summary-%d.json" % k)]
            if self.collector:
                specs.append(self.spec(k, parts, self.argv, self.env, files, dict(self.collector)))
            else:
                specs.append(self.spec(k, parts, self.argv, dict(self.env, VITKO_SPLIT_ASSIGN=str(k)), files))
        return specs

    def parse(self, spec, outcome):
        data = outcome.files.get(spec.result_files[0], b"").decode("utf-8", "replace")
        results = []
        for line in data.splitlines():
            record = json.loads(line)
            nodeid = record["nodeid"]
            suite, _, name = nodeid.partition("::")
            results.append(
                TestResult(
                    id=nodeid,
                    outcome=record["outcome"],
                    seconds=float(record.get("seconds", 0)),
                    part=spec.part,
                    suite=suite,
                    name=name or nodeid,
                    output=record.get("output", ""),
                )
            )
        return Parsed(results, {r.id for r in results})


# ---- cargo nextest ----------------------------------------------------------------------------

#: `cargo nextest run` options that `cargo nextest list` doesn't take (value: takes an argument).
NEXTEST_RUN_ONLY = {
    "--fail-fast": False,
    "--no-fail-fast": False,
    "--hide-progress-bar": False,
    "--no-capture": False,
    "--nocapture": False,
    "--no-input-handler": False,
    "--no-output-indent": False,
    "--no-run": False,
    "--debugger": True,
    "--failure-output": True,
    "--final-status-level": True,
    "--flaky-result": True,
    "--max-progress-running": True,
    "--message-format": True,
    "--message-format-version": True,
    "--no-tests": True,
    "--retries": True,
    "--status-level": True,
    "--stress-count": True,
    "--stress-duration": True,
    "--success-output": True,
    "--tracer": True,
    "-R": True,
    "--rerun": True,
    "-j": True,
    "--test-threads": True,
    "--max-fail": True,
}
NEXTEST_FILTER_OPTIONS = ("-E", "--filterset", "--filter-expr")
NEXTEST_STATUS = re.compile(
    r"^\s*(PASS|FAIL|FLAKY|SIG[A-Z0-9]+|TIMEOUT|LEAK|LEAK-FAIL|ABORT|SKIP|EXIT)(?:\s+\d+/\d+)?\s+"
    r"\[\s*([\d.]+)s\]\s+(?:\(\s*\d+/\d+\)\s+)?(\S+)\s+(.+?)\s*$"
)
NEXTEST_SECTION_END = re.compile(r"^\s*(Summary|Cancelling|Starting)\s+\[")
PASSING_NEXTEST = {"PASS": "passed", "FLAKY": "passed", "LEAK": "passed", "SKIP": "skipped"}
MAX_FILTER_BYTES = 100 * 1024


def split_nextest_argv(argv: Sequence[str]) -> Tuple[List[str], List[str], List[str], List[str]]:
    """``cargo [+tc] nextest run ARGS [-- BINARY ARGS]`` -> (prefix up to ``nextest``, options
    without filtersets, the user's filtersets, binary args including the leading ``--``)."""
    words = list(argv)
    at = words.index("nextest")
    prefix, rest = words[: at + 1], words[at + 2 :]  # drop the `run` word; callers add run or list
    binary: List[str] = []
    if "--" in rest:
        cut = rest.index("--")
        rest, binary = rest[:cut], rest[cut:]
    options, filters = [], []
    i = 0
    while i < len(rest):
        word = rest[i]
        name, eq, value = word.partition("=")
        if word in NEXTEST_FILTER_OPTIONS and i + 1 < len(rest):
            filters.append(rest[i + 1])
            i += 2
            continue
        if eq and name in NEXTEST_FILTER_OPTIONS:
            filters.append(value)
        elif word.startswith("-E") and len(word) > 2 and not eq:
            filters.append(word[2:])
        else:
            options.append(word)
        i += 1
    return prefix, options, filters, binary


def list_options(options: Sequence[str]) -> List[str]:
    """The options `cargo nextest list` accepts (it rejects the runner's own)."""
    kept, i = [], 0
    while i < len(options):
        word = options[i]
        name = word.split("=", 1)[0]
        if name in NEXTEST_RUN_ONLY:
            i += 2 if NEXTEST_RUN_ONLY[name] and "=" not in word else 1
            continue
        kept.append(word)
        i += 1
    return kept


def _filter_string(text: str) -> str:
    return re.sub(r"([\\(),])", r"\\\1", text)


def nextest_filters(tests: Sequence[str], user_filters: Sequence[str], limit: int = MAX_FILTER_BYTES) -> List[str]:
    """Filtersets selecting exactly ``tests`` ("binary test" ids), each under ``limit`` bytes,
    AND-ed with the user's own. nextest runs a test matching any ``-E``, so chunks add up."""
    by_binary: Dict[str, List[str]] = {}
    for test in tests:
        binary, _, name = test.partition(" ")
        by_binary.setdefault(binary, []).append(name)
    user = " | ".join("(%s)" % f for f in user_filters)
    wrap = (lambda expr: "(%s) & (%s)" % (user, expr)) if user else (lambda expr: expr)
    chunks: List[str] = []
    terms: List[str] = []
    size = 0
    for binary, names in by_binary.items():
        for name in names:
            term = "(binary_id(=%s) & test(=%s))" % (_filter_string(binary), _filter_string(name))
            if terms and size + len(term) + 3 > limit - len(user) - 16:
                chunks.append(wrap(" | ".join(terms)))
                terms, size = [], 0
            terms.append(term)
            size += len(term) + 3
    if terms:
        chunks.append(wrap(" | ".join(terms)))
    return chunks


def parse_nextest_output(text: str, part: int) -> List[TestResult]:
    lines = ANSI.sub("", text).splitlines()
    final: Dict[str, TestResult] = {}
    first_line: Dict[str, int] = {}
    for n, line in enumerate(lines):
        m = NEXTEST_STATUS.match(line)
        if not m:
            continue
        status, seconds, binary, name = m.groups()
        test_id = "%s %s" % (binary, name)
        outcome = PASSING_NEXTEST.get(status, "failed")
        first_line.setdefault(test_id, n)
        final[test_id] = TestResult(
            id=test_id, outcome=outcome, seconds=float(seconds), part=part, suite=binary, name=name
        )
    for test_id, result in final.items():
        if result.failed:
            result.output = _section_after(lines, first_line[test_id])
    return list(final.values())


def _section_after(lines: List[str], start: int, limit: int = 200) -> str:
    out = [lines[start]]
    for line in lines[start + 1 : start + 1 + limit]:
        if NEXTEST_STATUS.match(line) or NEXTEST_SECTION_END.match(line):
            break
        out.append(line)
    return "\n".join(out)


class NextestAdapter(Adapter):
    tool = "nextest"
    unit_guess = 0.1  # seconds per test, when there are no past timings

    def __init__(self, ctx):
        super().__init__(ctx)
        self.prefix, self.options, self.filters, self.binary = split_nextest_argv(ctx.argv)

    def prepare(self):
        argv = self.prefix + ["list"] + list_options(self.options) + ["--message-format", "json"]
        for f in self.filters:
            argv += ["-E", f]
        argv += self.binary
        code, out = self.run_before_split(argv)
        if code != 0:
            return Prepared(exit_code=code)
        try:
            listing = json.loads(out[out.index("{") :])
        except ValueError:
            print("cargo nextest list printed no test list", flush=True)
            return Prepared(exit_code=1)
        ids = []
        for suite in listing.get("rust-suites", {}).values():
            for name, case in suite.get("testcases", {}).items():
                if case.get("filter-match", {}).get("status") == "matches":
                    ids.append("%s %s" % (suite["binary-id"], name))
        return Prepared(units=ids, planned=ids, unit_count=len(ids))

    def specs(self, groups, parts):
        assert groups is not None
        specs = []
        for k, tests in enumerate(groups, start=1):
            argv = self.prefix + ["run"] + self.options
            for expr in nextest_filters(tests, self.filters):
                argv += ["-E", expr]
            specs.append(self.spec(k, parts, argv + self.binary))
        return specs

    def parse(self, spec, outcome):
        results = parse_nextest_output(outcome.output, spec.part)
        return Parsed(results, {r.id for r in results})


# ---- jest and vitest: the tool's own sharding -------------------------------------------------


def parse_jest_json(data: dict, cwd: str, part: int) -> List[TestResult]:
    status_map = {
        "passed": "passed",
        "failed": "failed",
        "pending": "skipped",
        "skipped": "skipped",
        "todo": "skipped",
        "disabled": "skipped",
        "focused": "passed",
    }
    results = []
    for suite in data.get("testResults", []):
        path = (
            os.path.relpath(suite.get("name", "?"), cwd)
            if os.path.isabs(suite.get("name", ""))
            else suite.get("name", "?")
        )
        assertions = suite.get("assertionResults", [])
        for a in assertions:
            name = a.get("fullName") or " ".join(list(a.get("ancestorTitles", [])) + [a.get("title", "")]).strip()
            outcome = status_map.get(a.get("status", ""), "failed")
            results.append(
                TestResult(
                    id="%s > %s" % (path, name),
                    outcome=outcome,
                    seconds=(a.get("duration") or 0) / 1000.0,
                    part=part,
                    suite=path,
                    name=name,
                    output="\n".join(a.get("failureMessages") or []) if outcome == "failed" else "",
                )
            )
        if suite.get("status") == "failed" and not any(r.failed for r in results if r.suite == path):
            results.append(
                TestResult(
                    id=path,
                    outcome="error",
                    part=part,
                    suite=path,
                    name="(file)",
                    output=suite.get("message", "") or "the test file failed to run",
                )
            )
    return results


#: A test file this share of the whole run (and at least this long) bounds any split of it.
DOMINANT_FILE_SHARE = 0.4
DOMINANT_FILE_SECONDS = 60.0


def dominant_file_notes(file_seconds: Dict[str, float], parts: int) -> List[str]:
    """Jest and Vitest give each part whole files, so a part can't finish before its longest
    file: say so when one file is a large share of the run."""
    total = sum(file_seconds.values())
    if parts < 2 or total <= 0:
        return []
    name, longest = max(file_seconds.items(), key=lambda kv: kv[1])
    if longest < DOMINANT_FILE_SECONDS or longest < DOMINANT_FILE_SHARE * total:
        return []
    return ["%s took %ds of the %ds all test files took. Each test file runs whole in one part, so "
            "splitting can't make the run shorter than that file; splitting the file into smaller "
            "files would." % (name, round(longest), round(total))]


class JestAdapter(Adapter):
    tool = "jest"
    list_args = ["--listTests"]

    def __init__(self, ctx):
        super().__init__(ctx)
        self.file_seconds: Dict[str, float] = {}

    def result_file(self, k: int) -> str:
        return os.path.join(self.ctx.plan_dir, "%s-part-%d.json" % (self.tool, k))

    def part_args(self, k: int, parts: int) -> List[str]:
        return ["--shard=%d/%d" % (k, parts), "--passWithNoTests", "--json", "--outputFile=%s" % self.result_file(k)]

    def prepare(self):
        code, out = self.run_before_split(list(self.ctx.argv) + self.list_args, show_output=False)
        files = [line for line in out.splitlines() if line.strip()] if code == 0 else []
        return Prepared(unit_count=len(files) or None)

    def specs(self, groups, parts):
        return [
            self.spec(k, parts, list(self.ctx.argv) + self.part_args(k, parts), result_files=[self.result_file(k)])
            for k in range(1, parts + 1)
        ]

    def parse(self, spec, outcome):
        raw = outcome.files.get(spec.result_files[0])
        if raw is None:
            return Parsed([], problem="Part %d of %d did not write its results" % (spec.part, spec.parts))
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return Parsed([], problem="Part %d of %d wrote unreadable results" % (spec.part, spec.parts))
        for suite in data.get("testResults", []):
            start, end = suite.get("startTime"), suite.get("endTime")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
                name = suite.get("name", "?")
                name = os.path.relpath(name, self.ctx.cwd) if os.path.isabs(name) else name
                self.file_seconds[name] = (end - start) / 1000.0
        return Parsed(parse_jest_json(data, self.ctx.cwd, spec.part))

    def notes(self, report):
        return dominant_file_notes(self.file_seconds, len(report.parts))

    def extra_problems(self, results):
        return [] if results else ["No tests found in any part"]


class VitestAdapter(JestAdapter):
    tool = "vitest"
    list_args = ["list", "--filesOnly"]

    def part_args(self, k, parts):
        return [
            "--shard=%d/%d" % (k, parts),
            "--passWithNoTests",
            "--reporter=default",
            "--reporter=json",
            "--outputFile.json=%s" % self.result_file(k),
        ]

    def prepare(self):
        argv = list(self.ctx.argv)
        if "run" in argv:
            argv.remove("run")
        code, out = self.run_before_split(argv + self.list_args, show_output=False)
        files = [line for line in out.splitlines() if line.strip()] if code == 0 else []
        return Prepared(unit_count=len(files) or None)


# ---- go test ----------------------------------------------------------------------------------

#: `go test` flags that take a value (`-flag value`; `-flag=value` needs no table).
GO_VALUE_FLAGS = frozenset(
    """
-run -skip -count -timeout -p -parallel -cpu -bench -benchtime -benchmem -blockprofile -blockprofilerate
-coverprofile -covermode -coverpkg -cpuprofile -memprofile -memprofilerate -mutexprofile
-mutexprofilefraction -outputdir -trace -shuffle -tags -mod -modfile -o -exec -ldflags -gcflags
-asmflags -gccgoflags -buildmode -compiler -installsuffix -overlay -pgo -pkgdir -toolexec -vet
-fuzz -fuzztime -fuzzminimizetime -list -C
""".split()
)
GO_LIST_FLAGS = frozenset(["-tags", "-mod", "-modfile", "-race", "-C"])


def split_go_argv(argv: Sequence[str]) -> Tuple[List[str], List[str], List[str]]:
    """``go test FLAGS PACKAGES [-args ...]`` -> (flags, packages, trailing -args part)."""
    words = list(argv[2:])
    tail: List[str] = []
    if "-args" in words:
        cut = words.index("-args")
        words, tail = words[:cut], words[cut:]
    flags, packages = [], []
    i = 0
    while i < len(words):
        word = words[i]
        if word.startswith("-"):
            flags.append(word)
            name = "-" + word.lstrip("-")
            if "=" not in word and name in GO_VALUE_FLAGS and i + 1 < len(words):
                flags.append(words[i + 1])
                i += 1
        else:
            packages.append(word)
        i += 1
    return flags, packages or ["."], tail


def go_list_flags(flags: Sequence[str]) -> List[str]:
    kept, i = [], 0
    while i < len(flags):
        word = flags[i]
        name = "-" + word.split("=", 1)[0].lstrip("-")
        takes_value = "=" not in word and name in GO_VALUE_FLAGS
        if name in GO_LIST_FLAGS:
            kept.append(word)
            if takes_value and i + 1 < len(flags):
                kept.append(flags[i + 1])
        i += 2 if takes_value else 1
    return kept


def parse_go_events(text: str, part: int) -> Tuple[List[TestResult], Set[str], Dict[str, float]]:
    """Per-test results, the packages that finished, and each package's seconds."""
    tests: Dict[str, TestResult] = {}
    output: Dict[str, List[str]] = {}
    packages: Dict[str, str] = {}
    elapsed: Dict[str, float] = {}
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        pkg, test, action = event.get("Package", ""), event.get("Test"), event.get("Action")
        key = "%s %s" % (pkg, test) if test else pkg
        if action == "output":
            output.setdefault(key, []).append(event.get("Output", ""))
        elif action in ("pass", "fail", "skip"):
            outcome = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
            if test:
                tests[key] = TestResult(
                    id=key, outcome=outcome, seconds=float(event.get("Elapsed") or 0), part=part, suite=pkg, name=test
                )
            else:
                packages[pkg] = outcome
                elapsed[pkg] = float(event.get("Elapsed") or 0)
    results = list(tests.values())
    for r in results:
        if r.failed:
            r.output = "".join(output.get(r.id, []))
    for pkg, outcome in packages.items():
        if outcome == "failed" and not any(r.failed and r.suite == pkg for r in results):
            # Also the output of tests that never reported (a panic or a timeout ends the package).
            unfinished = [k for k in output if k.startswith(pkg + " ") and k not in tests]
            text = "".join(output.get(pkg, [])) + "".join("".join(output[k]) for k in unfinished)
            results.append(
                TestResult(
                    id=pkg,
                    outcome="error",
                    part=part,
                    suite=pkg,
                    name="(package)",
                    output=text or "the package failed to build or run",
                )
            )
    return results, set(packages), elapsed


class GoAdapter(Adapter):
    tool = "go"
    unit_guess = 5.0  # seconds per package, when there are no past timings

    def __init__(self, ctx):
        super().__init__(ctx)
        self.flags, self.packages, self.tail = split_go_argv(ctx.argv)
        self.go = ctx.argv[0]
        self.elapsed: Dict[str, float] = {}

    def prepare(self):
        code, out = self.run_before_split([self.go, "list"] + go_list_flags(self.flags) + self.packages)
        if code != 0:
            return Prepared(exit_code=code)
        packages = [line.strip() for line in out.splitlines() if line.strip()]
        return Prepared(units=packages, planned=packages, unit_count=len(packages))

    def events_file(self, k: int) -> str:
        return os.path.join(self.ctx.plan_dir, "go-part-%d.jsonl" % k)

    def specs(self, groups, parts):
        assert groups is not None
        flags = self.flags if "-json" in self.flags else self.flags + ["-json"]
        env = dict(self.ctx.env)
        env["PYTHONPATH"] = os.pathsep.join([self.ctx.home] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        return [
            self.spec(
                k,
                parts,
                [sys.executable, "-m", "vitko_split.gojson", self.events_file(k), "--", self.go, "test"]
                + flags
                + pkgs
                + self.tail,
                env,
                [self.events_file(k)],
            )
            for k, pkgs in enumerate(groups, start=1)
        ]

    def parse(self, spec, outcome):
        text = outcome.files.get(spec.result_files[0], b"").decode("utf-8", "replace")
        results, packages, elapsed = parse_go_events(text, spec.part)
        self.elapsed.update(elapsed)
        return Parsed(results, packages)

    def timings(self, results):
        return {pkg: seconds for pkg, seconds in self.elapsed.items() if seconds > 0}
