"""The Vitko Runners backend: each part runs in its own copy of this job, made by the runner host.

The CLI asks the runner host whether it can make copies, then starts the in-job helper
(``helper.py``) as root in its own service; the helper does the rest.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zlib
from typing import Dict, List, Optional

from .backends import Backend, EventSink, PartOutcome, PartSpec, _pid_alive, write_plan_dir

PROTOCOL = "vitko-split-v2"
HOST_CID = 2
JOB_PORT = 5207
PROBE_TIMEOUT_S = 3
HELPER_START_TIMEOUT_S = 20
MAX_LINE = 1 << 20
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helper.py")


def _pack_timings(data: dict) -> dict:
    """Timings are stored as sent; send them compressed (test ids repeat long prefixes)."""
    raw = json.dumps(data, separators=(",", ":")).encode()
    return {"zlib": base64.b64encode(zlib.compress(raw, 9)).decode()}


def _unpack_timings(stored: Optional[dict]) -> Optional[dict]:
    if not isinstance(stored, dict) or "zlib" not in stored:
        return stored
    try:
        return json.loads(zlib.decompress(base64.b64decode(stored["zlib"])))
    except (ValueError, zlib.error):
        return None


class HostUnavailable(Exception):
    """This job can't be copied (not a Vitko runner, no host service, refused). Run unsplit."""


class HostFailed(Exception):
    """The host started making copies and then failed: the step fails, with this reason."""


class _Lines:
    def __init__(self, sock: socket.socket) -> None:
        self.sock, self.buf = sock, b""

    def send(self, obj: dict) -> None:
        self.sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    def recv(self) -> Optional[dict]:
        while b"\n" not in self.buf:
            if len(self.buf) > MAX_LINE:
                raise HostFailed("the host sent an oversized message")
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)


def _probe(timings_key: Optional[str]) -> dict:
    if not hasattr(socket, "AF_VSOCK"):
        raise HostUnavailable("splitting needs a Vitko runner")
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.settimeout(PROBE_TIMEOUT_S)
    try:
        sock.connect((HOST_CID, JOB_PORT))
        lines = _Lines(sock)
        lines.send({"type": "hello", "protocol": PROTOCOL, "timingsKey": timings_key})
        welcome = lines.recv()
    except (OSError, ValueError):
        raise HostUnavailable("splitting needs a Vitko runner") from None
    finally:
        sock.close()
    if not welcome or welcome.get("type") != "welcome":
        reason = (welcome or {}).get("reason") or "this runner can't make copies of the job"
        raise HostUnavailable(reason)
    return welcome


class HostBackend(Backend):
    name = "host"
    copies_job = True

    def __init__(self, welcome: dict, workdir: str, control: str, unit: str) -> None:
        self._max_parts = max(1, int(welcome.get("maxParts") or 1))
        self._timings = welcome.get("timings")
        self._workdir, self._control, self._unit = workdir, control, unit
        self._split: Optional[_Lines] = None
        self._collector_logs: Dict[int, str] = {}
        self.timings_key: Optional[str] = None
        self._closed = False

    # -- setup ------------------------------------------------------------------------------

    @classmethod
    def connect(cls, timings_key: Optional[str] = None) -> "HostBackend":
        welcome = _probe(timings_key)
        if shutil.which("sudo") is None or shutil.which("systemd-run") is None:
            raise HostUnavailable("splitting needs sudo and systemd in the job")
        workdir = tempfile.mkdtemp(prefix="vitko-split-", dir=os.environ.get("RUNNER_TEMP") or None)
        control = os.path.join(workdir, "helper.sock")
        unit = "vitko-split-helper-%s" % os.urandom(4).hex()
        start = subprocess.run(
            ["sudo", "-n", "systemd-run", "--quiet", "--collect", "--unit", unit,
             "--property=KillMode=control-group", sys.executable, "-I", HELPER,
             "--control", control, "--owner-uid", str(os.getuid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        if start.returncode != 0:
            shutil.rmtree(workdir, ignore_errors=True)
            raise HostUnavailable("splitting needs passwordless sudo in the job (%s)"
                                  % start.stdout.decode(errors="replace").strip()[:200])
        backend = cls(welcome, workdir, control, unit)
        backend.timings_key = timings_key
        deadline = time.monotonic() + HELPER_START_TIMEOUT_S
        while not os.path.exists(control):
            if time.monotonic() > deadline:
                backend.close()
                raise HostUnavailable("the helper that makes copies of the job did not start")
            time.sleep(0.05)
        return backend

    def _request(self, obj: dict) -> _Lines:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._control)
        lines = _Lines(sock)
        lines.send(obj)
        return lines

    # -- Backend ----------------------------------------------------------------------------

    def max_parts(self) -> int:
        return self._max_parts

    def timings(self) -> Optional[dict]:
        return _unpack_timings(self._timings)

    def start_collector(self, argv: List[str], cwd: str, env: Dict[str, str], log_path: str) -> int:
        lines = self._request({"cmd": "start-collector", "argv": argv, "cwd": cwd, "env": env,
                               "uid": os.getuid(), "gid": os.getgid(), "log": log_path,
                               "exitFile": log_path + ".exit"})
        reply = lines.recv() or {}
        lines.sock.close()
        if not reply.get("ok"):
            raise HostUnavailable("could not start the test collection (%s)" % reply.get("reason", "?"))
        pid = int(reply["pid"])
        self._collector_logs[pid] = log_path
        return pid

    def collector_status(self, pid: int) -> Optional[int]:
        if _pid_alive(pid):
            return None
        path = self._collector_logs.get(pid, "") + ".exit"
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return 1

    def run_parts(self, plan_dir: str, specs: List[PartSpec], on_event: EventSink) -> List[PartOutcome]:
        write_plan_dir(plan_dir, specs)
        lines = self._request({"cmd": "split", "parts": len(specs), "planDir": os.path.abspath(plan_dir),
                               "timingsKey": self.timings_key})
        self._split = lines
        outputs: Dict[int, List[str]] = {s.part: [] for s in specs}
        done: Dict[int, PartOutcome] = {}
        started_any = False
        while True:
            msg = lines.recv()
            if msg is None:
                if not started_any:
                    raise HostUnavailable("the helper stopped before any copy was made")
                raise HostFailed("the connection to the host was lost")
            kind = msg.get("type")
            if kind == "resume-writes":
                on_event({"type": "copies", "parts": len(specs), "copyMs": msg.get("copyMs")})
            elif kind == "started":
                started_any = True
                on_event({"type": "started", "part": int(msg["part"])})
            elif kind == "output":
                part = int(msg["part"])
                data = str(msg.get("data", ""))
                outputs.setdefault(part, []).append(data)
                on_event({"type": "output", "part": part, "data": data})
            elif kind == "part":
                part = int(msg["part"])
                files = {name: base64.b64decode(blob) for name, blob in (msg.get("files") or {}).items()}
                if msg.get("reason"):  # the host could not run this part: say why, in its log
                    outputs.setdefault(part, []).append("\nThis part did not run: %s\n" % msg["reason"])
                outcome = PartOutcome(part, int(msg.get("exit", 1)), "".join(outputs.get(part, [])), files,
                                      int(msg.get("wallMs") or 0), msg.get("cpuMs"), msg.get("checks") or {})
                done[part] = outcome
                on_event({"type": "part", "part": part, "exit": outcome.exit, "wallMs": outcome.wall_ms})
            elif kind == "finished":
                break
            elif kind == "failed":
                reason = msg.get("reason") or "unknown"
                if not started_any:
                    raise HostUnavailable("the host could not make copies of this job (%s)" % reason)
                raise HostFailed(reason)
        for spec in specs:  # a part the host never reported is a failed part, never a silent pass
            if spec.part not in done:
                done[spec.part] = PartOutcome(spec.part, 1, "".join(outputs.get(spec.part, []))
                                              + "\nThis part did not finish.\n", {}, 0)
        return [done[s.part] for s in specs]

    def store_timings(self, key: str, data: dict) -> None:
        if self._split is None:
            return
        try:
            self._split.send({"cmd": "timings", "key": key, "data": _pack_timings(data)})
        except OSError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._split is not None:
            self._split.sock.close()
            self._split = None
        try:
            lines = self._request({"cmd": "stop"})
            lines.recv()
            lines.sock.close()
        except OSError:
            subprocess.run(["sudo", "-n", "systemctl", "stop", self._unit], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(self._workdir, ignore_errors=True)


__all__ = ["HostBackend", "HostFailed", "HostUnavailable"]
