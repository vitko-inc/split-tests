"""Run ``go test -json``, print its normal output, and keep a compact events file.

usage: python -m vitko_split.gojson <events file> -- go test ... -json ...

The events file is what the part sends back as its result, so it holds only what the report
needs: each test's and package's outcome and time, and the output of what failed (or never
finished, such as a test that panicked). Passing tests' output is printed to the part's log as it
runs and is not kept, which keeps a large suite's result far smaller than ``go test -json``'s full
event stream."""

import json
import subprocess
import sys
from typing import Dict, List, TextIO

KEEP_ACTIONS = ("pass", "fail", "skip")


class Compactor:
    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.pending: Dict[str, List[str]] = {}  # output of tests and packages not finished yet

    def _write(self, event: dict) -> None:
        self.out.write(json.dumps(event, separators=(",", ":")) + "\n")

    def _flush(self, pkg: str, key: str) -> None:
        lines = self.pending.pop(key, None)
        if lines:
            test = key[len(pkg) + 1:] if key != pkg else None
            event = {"Action": "output", "Package": pkg, "Output": "".join(lines)}
            if test:
                event["Test"] = test
            self._write(event)

    def event(self, event: dict) -> None:
        pkg, test, action = event.get("Package", ""), event.get("Test"), event.get("Action")
        key = "%s %s" % (pkg, test) if test else pkg
        if action == "output":
            self.pending.setdefault(key, []).append(event.get("Output", ""))
            return
        if action not in KEEP_ACTIONS:
            return  # run, pause, cont, start, bench: not needed for the report
        if action == "fail":
            self._flush(pkg, key)
        else:
            self.pending.pop(key, None)
        if not test and action == "fail":
            # A failed package: keep the output of its tests that never reported (a panic or a
            # timeout ends the package without a result for the running test).
            for other in [k for k in self.pending if k.startswith(pkg + " ")]:
                self._flush(pkg, other)
        if not test:
            for other in [k for k in self.pending if k.startswith(pkg + " ")]:
                self.pending.pop(other, None)
        kept = {"Action": action, "Package": pkg}
        if test:
            kept["Test"] = test
        if event.get("Elapsed") is not None:
            kept["Elapsed"] = event["Elapsed"]
        self._write(kept)

    def close(self) -> None:
        # Whatever never finished (the go command itself died): keep it, it explains the failure.
        for key in list(self.pending):
            pkg = key.split(" ", 1)[0]
            self._flush(pkg, key)


def main(argv):
    events_path, argv = argv[0], argv[2:]
    with open(events_path, "w") as events:
        compactor = Compactor(events)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE)
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace")
            try:
                event = json.loads(line)
            except ValueError:
                sys.stdout.write(line)  # build errors and anything else that isn't an event
            else:
                if event.get("Action") == "output":
                    sys.stdout.write(event.get("Output", ""))
                if isinstance(event, dict):
                    compactor.event(event)
            sys.stdout.flush()
        compactor.close()
        proc.stdout.close()
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
