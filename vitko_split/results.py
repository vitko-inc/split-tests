"""Merging the parts' results into one answer for the job: logs, failures, JUnit, summary, exit."""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, TextIO

from .backends import PartOutcome

FAILED = frozenset(["failed", "error"])
OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed", "xpassed")
MAX_ANNOTATIONS = 10
MAX_DETAILED_FAILURES = 50
MAX_EXCERPT_LINES = 40
MAX_LISTED_MISSING = 20


@dataclass
class TestResult:
    id: str
    outcome: str  # one of OUTCOMES
    seconds: float = 0.0
    part: int = 0
    suite: str = ""  # JUnit classname: file, test binary or package
    name: str = ""  # JUnit name
    output: str = ""  # failures only: what the test printed, or its error

    @property
    def failed(self) -> bool:
        return self.outcome in FAILED


@dataclass
class PartReport:
    outcome: PartOutcome
    results: List[TestResult]
    units: Set[str] = field(default_factory=set)  # tests (or packages) this part finished
    problem: Optional[str] = None  # the part didn't finish cleanly, in plain words


@dataclass
class Report:
    tool: str
    parts: List[PartReport]
    planned: Optional[List[str]] = None  # every test (or package) that should report
    wall_ms: int = 0
    notes: List[str] = field(default_factory=list)
    extra_problems: List[str] = field(default_factory=list)

    @property
    def results(self) -> List[TestResult]:
        merged: Dict[str, TestResult] = {}
        for part in self.parts:
            for result in part.results:
                merged.setdefault(result.id, result)
        return list(merged.values())

    def outcomes(self) -> Dict[str, str]:
        return {r.id: r.outcome for r in self.results}

    def missing(self) -> List[str]:
        if self.planned is None:
            return []
        reported: Set[str] = set()
        for part in self.parts:
            reported |= part.units
        return [unit for unit in self.planned if unit not in reported]

    def duplicates(self) -> List[str]:
        seen, twice = set(), []
        for part in self.parts:
            for result in part.results:
                if result.id in seen:
                    twice.append(result.id)
                seen.add(result.id)
        return twice

    def failures(self) -> List[TestResult]:
        return [r for r in self.results if r.failed]

    def problems(self) -> List[str]:
        found = [p.problem for p in self.parts if p.problem] + list(self.extra_problems)
        missing = self.missing()
        if missing:
            found.append(
                "%d %s did not report a result: %s%s"
                % (
                    len(missing),
                    "test" if len(missing) == 1 else "tests",
                    ", ".join(missing[:MAX_LISTED_MISSING]),
                    " …" if len(missing) > MAX_LISTED_MISSING else "",
                )
            )
        return found

    def ok(self) -> bool:
        return not self.failures() and not self.problems()

    def counts(self, results: Optional[Sequence[TestResult]] = None) -> Dict[str, int]:
        counts = {o: 0 for o in OUTCOMES}
        for r in self.results if results is None else results:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
        return counts

    def timings(self) -> Dict[str, float]:
        return {r.id: r.seconds for r in self.results if r.seconds > 0}


def part_problem(
    outcome: PartOutcome, total: int, results: Sequence[TestResult], expect_results: bool
) -> Optional[str]:
    """Why a part counts as broken even if none of its tests failed, or None."""
    label = "Part %d of %d" % (outcome.part, total)
    if expect_results and not results and outcome.exit != 0:
        return "%s stopped with exit status %d before reporting any results" % (label, outcome.exit)
    if outcome.exit != 0 and not any(r.failed for r in results):
        return "%s ended with exit status %d without reporting a failed test" % (label, outcome.exit)
    return None


def describe_counts(counts: Dict[str, int]) -> str:
    words = [
        ("passed", "passed"),
        ("failed", "failed"),
        ("error", "errors"),
        ("skipped", "skipped"),
        ("xfailed", "expected failures"),
        ("xpassed", "unexpected passes"),
    ]
    shown = ["%d %s" % (counts[k], label) for k, label in words if counts.get(k)]
    return ", ".join(shown) if shown else "no tests"


def duration(ms: float) -> str:
    seconds = ms / 1000.0
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, rest = divmod(int(round(seconds)), 60)
    return "%dm%02ds" % (minutes, rest)


def in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _escape_command(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_command(text).replace(":", "%3A").replace(",", "%2C")


def excerpt(text: str, lines: int = MAX_EXCERPT_LINES) -> str:
    rows = text.rstrip("\n").splitlines()
    if len(rows) <= lines:
        return "\n".join(rows)
    return "\n".join(["… (%d earlier lines)" % (len(rows) - lines)] + rows[-lines:])


def print_report(report: Report, out: Optional[TextIO] = None) -> None:
    out = out or sys.stdout
    gha = in_github_actions()
    total = len(report.parts)
    for part in report.parts:
        o = part.outcome
        title = "Part %d of %d: %s (%s)" % (
            o.part,
            total,
            describe_counts(report.counts(part.results)),
            duration(o.wall_ms),
        )
        out.write(("::group::%s\n" % title) if gha else ("── %s ──\n" % title))
        out.write(o.output if o.output.endswith("\n") or not o.output else o.output + "\n")
        if gha:
            out.write("::endgroup::\n")
    failures = report.failures()
    if failures:
        out.write("\n%d %s failed:\n" % (len(failures), "test" if len(failures) == 1 else "tests"))
        for r in failures[:MAX_DETAILED_FAILURES]:
            out.write("\n✗ %s (part %d)\n" % (r.id, r.part))
            if r.output.strip():
                out.write(excerpt(r.output) + "\n")
        if len(failures) > MAX_DETAILED_FAILURES:
            out.write("\n… and %d more (see the part logs above)\n" % (len(failures) - MAX_DETAILED_FAILURES))
        if gha:
            for r in failures[:MAX_ANNOTATIONS]:
                out.write(
                    "::error title=%s::%s\n"
                    % (_escape_property("Test failed: " + r.id), _escape_command(excerpt(r.output, 10) or r.id))
                )
    for problem in report.problems():
        out.write(("::error::%s\n" % _escape_command(problem)) if gha else ("Problem: %s\n" % problem))
    for note in report.notes:
        out.write(note + "\n")
    verdict = "passed" if report.ok() else "failed"
    out.write(
        "\nSplit tests %s: %d %s, %s, in %s.\n"
        % (
            verdict,
            total,
            "part" if total == 1 else "parts",
            describe_counts(report.counts()),
            duration(report.wall_ms),
        )
    )
    out.flush()


def junit_xml(report: Report) -> bytes:
    results = report.results
    counts = report.counts(results)
    root = ET.Element(
        "testsuites",
        name="split-tests",
        tests=str(len(results)),
        failures=str(counts["failed"]),
        errors=str(counts["error"]),
        skipped=str(counts["skipped"] + counts["xfailed"]),
        time="%.3f" % (report.wall_ms / 1000.0),
    )
    suites: Dict[str, List[TestResult]] = {}
    for r in results:
        suites.setdefault(r.suite or r.id, []).append(r)
    for name, members in suites.items():
        c = report.counts(members)
        suite = ET.SubElement(
            root,
            "testsuite",
            name=name,
            tests=str(len(members)),
            failures=str(c["failed"]),
            errors=str(c["error"]),
            skipped=str(c["skipped"] + c["xfailed"]),
            time="%.3f" % sum(r.seconds for r in members),
        )
        for r in members:
            case = ET.SubElement(suite, "testcase", classname=name, name=r.name or r.id, time="%.3f" % r.seconds)
            if r.outcome == "failed":
                ET.SubElement(case, "failure", message="failed").text = _xml_safe(r.output)
            elif r.outcome == "error":
                ET.SubElement(case, "error", message="error").text = _xml_safe(r.output)
            elif r.outcome in ("skipped", "xfailed"):
                ET.SubElement(case, "skipped", message="expected failure" if r.outcome == "xfailed" else "skipped")
            ET.SubElement(case, "properties").append(ET.Element("property", name="part", value=str(r.part)))
    for problem in report.problems():
        suite = ET.SubElement(root, "testsuite", name="split-tests", tests="1", failures="0", errors="1", skipped="0")
        case = ET.SubElement(suite, "testcase", classname="split-tests", name="all tests reported")
        ET.SubElement(case, "error", message="incomplete").text = _xml_safe(problem)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _xml_safe(text: str) -> str:
    return "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 0x20)


def step_summary(report: Report) -> str:
    rows = [
        "### Split tests: %s" % ("passed" if report.ok() else "failed"),
        "",
        "| Part | Tests | Passed | Failed | Skipped | Time |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for part in report.parts:
        c = report.counts(part.results)
        rows.append(
            "| %d | %d | %d | %d | %d | %s |"
            % (
                part.outcome.part,
                len(part.results),
                c["passed"] + c["xpassed"],
                c["failed"] + c["error"],
                c["skipped"] + c["xfailed"],
                duration(part.outcome.wall_ms),
            )
        )
    c = report.counts()
    rows.append(
        "| **All** | **%d** | **%d** | **%d** | **%d** | **%s** |"
        % (
            len(report.results),
            c["passed"] + c["xpassed"],
            c["failed"] + c["error"],
            c["skipped"] + c["xfailed"],
            duration(report.wall_ms),
        )
    )
    failures = report.failures()
    if failures:
        rows += ["", "Failed tests:", ""] + [
            "- `%s` (part %d)" % (r.id, r.part) for r in failures[:MAX_DETAILED_FAILURES]
        ]
    for problem in report.problems():
        rows += ["", "**Problem:** " + problem]
    return "\n".join(rows) + "\n"


def publish(report: Report, junit_path: Optional[str]) -> None:
    """Write the JUnit file, the step summary and the step outputs (when running in Actions)."""
    if junit_path:
        with open(junit_path, "wb") as f:
            f.write(junit_xml(report))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(step_summary(report))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write("junit=%s\nparts=%d\nfailed=%d\n" % (junit_path or "", len(report.parts), len(report.failures())))
