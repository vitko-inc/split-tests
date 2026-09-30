"""The in-job helper for split tests on Vitko Runners.

The CLI starts it as root in its own service. It talks to the runner host on the job's behalf, and
in each copy of the job it prepares the copy and runs that copy's part of the tests.

Local protocol with the CLI (JSON lines on a Unix socket owned by the job's user):
  {"cmd": "start-collector", "argv": [...], "cwd": ..., "env": {...}, "uid": n, "gid": n,
   "log": path, "exitFile": path}  ->  {"ok": true, "pid": n}
  {"cmd": "split", "parts": n, "planDir": path, "timingsKey": "..."}  ->  the host's events,
   ending with {"type": "finished"} or {"type": "failed"}; the CLI may then send
   {"cmd": "timings", "key": ..., "data": {...}}.
  {"cmd": "stop"}  ->  {"ok": true}
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import pwd
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

PROTOCOL = "vitko-split-v2"
HOST_CID = 2
HOST_PORT = 5207
COPY_PORT = 5208
MAX_LINE = 1 << 20
MAX_FILE = 32 << 20
PAUSE_LIMIT_S = 120
COMMAND_LIMIT_S = 20
# Same rule as envpolicy.refused (this file runs on its own; a test keeps the two equal).
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|_KEY$|^ACTIONS_", re.IGNORECASE)
BENIGN_NAMES = frozenset(["TOKENIZERS_PARALLELISM"])

# <linux/random.h>
RNDADDENTROPY = 0x40085203
RNDRESEEDCRNG = 0x5207


# ---- pausing writes ---------------------------------------------------------------------------

_paused: List[str] = []
_pause_lock = threading.Lock()
_pause_generation = 0


def writable_block_filesystems() -> List[str]:
    """Mount points of read-write filesystems on block devices, each device once, root first."""
    seen, points = set(), []
    with open("/proc/self/mounts") as f:
        for line in f:
            dev, point, fstype, opts = line.split()[:4]
            if not dev.startswith("/dev/") or "rw" not in opts.split(","):
                continue
            if fstype not in ("ext4", "ext3", "xfs", "btrfs", "f2fs") or dev in seen:
                continue
            seen.add(dev)
            points.append(point.replace("\\040", " "))
    return sorted(points, key=lambda p: (p != "/", p))


def pause_writes() -> None:
    global _pause_generation
    with _pause_lock:
        _pause_generation += 1
        generation = _pause_generation
        subprocess.run(["sync"], check=False)
        for point in writable_block_filesystems():
            if subprocess.run(["fsfreeze", "-f", point], check=False).returncode == 0:
                _paused.append(point)
    timer = threading.Timer(PAUSE_LIMIT_S, resume_writes, kwargs={"generation": generation})
    timer.daemon = True
    timer.start()


def resume_writes(generation: Optional[int] = None) -> None:
    with _pause_lock:
        if generation is not None and generation != _pause_generation:
            return
        points = list(reversed(_paused)) or writable_block_filesystems()
        _paused.clear()
        for point in points:
            subprocess.run(["fsfreeze", "-u", point], check=False, stderr=subprocess.DEVNULL)


# ---- framing ----------------------------------------------------------------------------------


class Lines:
    """JSON lines over a socket, bounded."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""
        self.wlock = threading.Lock()

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self.wlock:
            self.sock.sendall(data)

    def recv(self) -> Optional[dict]:
        while b"\n" not in self.buf:
            if len(self.buf) > MAX_LINE:
                raise ValueError("line too long")
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)


# ---- in a copy --------------------------------------------------------------------------------


def refresh_randomness(seed_hex: str) -> None:
    seed = bytes.fromhex(seed_hex)
    if len(seed) < 32:
        raise ValueError("seed too short")
    words = (len(seed) + 3) // 4
    info = struct.pack("ii", len(seed) * 8, len(seed)) + seed.ljust(words * 4, b"\0")
    with open("/dev/random", "wb", buffering=0) as rnd:
        fcntl.ioctl(rnd, RNDADDENTROPY, info)
        fcntl.ioctl(rnd, RNDRESEEDCRNG)


def run_bounded(argv: List[str], report) -> None:
    started = time.monotonic()
    try:
        subprocess.run(argv, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=COMMAND_LIMIT_S)
    except subprocess.TimeoutExpired:
        report({"type": "progress", "step": " ".join(argv), "timedOut": True,
                "ms": int((time.monotonic() - started) * 1000)})


def _remove(paths: List[str]) -> None:
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _live_processes(names: List[str]) -> List[str]:
    found = set()
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open("/proc/%s/stat" % pid) as f:
                stat = f.read()
        except OSError:
            continue
        comm = stat[stat.find("(") + 1:stat.rfind(")")]
        state = stat[stat.rfind(")") + 2:stat.rfind(")") + 3]
        if comm in names and state != "Z":
            found.add(comm)
    return sorted(found)


def stop_runner(stop: dict, report=lambda message: None) -> Dict[str, object]:
    """Stop the job's runner in this copy and remove its credentials. The runner host names what
    to stop (``stop``: units, triggers, processes, files)."""
    units, triggers = list(stop.get("units", [])), list(stop.get("triggers", []))
    processes, files = list(stop.get("processes", [])), list(stop.get("files", []))
    for unit in triggers:
        run_bounded(["systemctl", "stop", unit], report)
    _remove(files)
    for name in processes:
        run_bounded(["pkill", "-KILL", "-x", name], report)
    for unit in units:
        for verb in (["kill", "--signal=SIGKILL"], ["stop"]):
            run_bounded(["systemctl"] + verb + [unit], report)
    _remove(files)
    state = "inactive"
    for unit in units:
        if "*" in unit:
            continue
        try:
            state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True,
                                   check=False, timeout=COMMAND_LIMIT_S).stdout.strip() or "unknown"
        except subprocess.TimeoutExpired:
            state = "unknown"
        break
    deadline = time.monotonic() + COMMAND_LIMIT_S
    left = _live_processes(processes)
    while left and time.monotonic() < deadline:
        time.sleep(0.1)
        left = _live_processes(processes)
    running = state in ("active", "activating", "reloading", "deactivating") or bool(left)
    return {"runner": "running" if running else "inactive", "runnerState": state, "runnerProcesses": left,
            "credentials": "gone" if not any(os.path.exists(p) for p in files) else "present"}


def secret_env_count(env: Dict[str, str]) -> int:
    return sum(1 for name in env if name not in BENIGN_NAMES and SECRET_NAME.search(name))


def demote(uid: Optional[int], gid: Optional[int]):
    """The job's user with all of its groups, as the step has them."""
    user = None
    if uid is not None:
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            pass

    def apply() -> None:
        os.setsid()
        if gid is not None:
            if user is not None:
                os.initgroups(user, gid)
            else:
                os.setgroups([gid])
            os.setgid(gid)
        if uid is not None:
            os.setuid(uid)

    return apply


def read_files(paths: List[str]) -> Dict[str, str]:
    out = {}
    for path in paths:
        try:
            with open(path, "rb") as f:
                data = f.read(MAX_FILE + 1)
        except OSError:
            continue
        if len(data) <= MAX_FILE:
            out[path] = base64.b64encode(data).decode()
    return out


def run_part(req: dict, host: Lines) -> None:
    """Prepare this copy, then run its part. Test code runs only after the preparation."""

    def step(name, action):
        started = time.monotonic()
        result = action()
        host.send({"type": "progress", "step": name, "ms": int((time.monotonic() - started) * 1000)})
        return result

    step("resume-writes", resume_writes)
    step("randomness", lambda: refresh_randomness(req["seed"]))
    step("clock", lambda: time.clock_settime(time.CLOCK_REALTIME, int(req["epochNs"]) / 1e9))
    checks = step("stop-runner", lambda: stop_runner(req.get("stop") or {}, host.send))
    part = int(req["part"])
    if not _plan_dir:
        raise RuntimeError("no plan for this copy")
    with open(os.path.join(_plan_dir, "parts", "%d.json" % part)) as f:
        spec = json.load(f)
    checks["secretEnvVars"] = secret_env_count(spec.get("env", {}))
    host.send({"type": "ready", "checks": checks})
    collect = spec.get("collectOnce")
    code = run_collected(collect, part, host) if collect else run_command(spec, host)
    host.send({"type": "done", "exit": code, "files": read_files(spec.get("resultFiles", []))})


def run_command(spec: dict, host: Lines) -> int:
    try:
        proc = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env=spec["env"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                preexec_fn=demote(spec.get("uid"), spec.get("gid")))
    except OSError as error:
        host.send({"type": "output", "data": "Could not start the tests: %s\n" % error})
        return 127
    assert proc.stdout is not None
    for chunk in iter(lambda: proc.stdout.read1(65536), b""):
        host.send({"type": "output", "data": chunk.decode("utf-8", "replace")})
    return proc.wait()


def run_collected(collect: dict, part: int, host: Lines) -> int:
    """Give the waiting pytest collection its part number and follow its log."""
    tmp = collect["assignFile"] + ".tmp"
    with open(tmp, "w") as f:
        f.write("%d\n" % part)
    os.replace(tmp, collect["assignFile"])
    proc = _collectors.get(int(collect["pid"]))
    with open(collect["logFile"], "rb") as log_file:
        while True:
            chunk = log_file.read(65536)
            if chunk:
                host.send({"type": "output", "data": chunk.decode("utf-8", "replace")})
                continue
            if proc is None or proc.poll() is not None:
                rest = log_file.read()
                if rest:
                    host.send({"type": "output", "data": rest.decode("utf-8", "replace")})
                break
            time.sleep(0.1)
    return proc.returncode if proc is not None else 1


def serve_copies() -> None:
    srv = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((socket.VMADDR_CID_ANY, COPY_PORT))
    srv.listen(4)
    while True:
        conn, (cid, _port) = srv.accept()
        if cid != HOST_CID:
            conn.close()
            continue
        threading.Thread(target=_copy_conn, args=(conn,), daemon=True).start()


def _copy_conn(conn: socket.socket) -> None:
    host = Lines(conn)
    try:
        req = host.recv()
        if not req or req.get("type") != "run":
            return
        global _is_copy
        _is_copy = True
        run_part(req, host)
    except Exception as error:
        try:
            host.send({"type": "failed", "reason": "%s: %s" % (type(error).__name__, error)})
        except OSError:
            pass
    finally:
        conn.close()


_is_copy = False
_plan_dir: Optional[str] = None


# ---- in the job -------------------------------------------------------------------------------

_collectors: Dict[int, subprocess.Popen] = {}


def start_collector(req: dict) -> dict:
    log_file = open(req["log"], "wb")
    os.fchown(log_file.fileno(), req["uid"] if req.get("uid") is not None else -1,
              req["gid"] if req.get("gid") is not None else -1)
    proc = subprocess.Popen(req["argv"], cwd=req["cwd"], env=req["env"], stdin=subprocess.DEVNULL,
                            stdout=log_file, stderr=subprocess.STDOUT,
                            preexec_fn=demote(req.get("uid"), req.get("gid")))
    log_file.close()
    _collectors[proc.pid] = proc
    if req.get("exitFile"):
        threading.Thread(target=_record_exit, args=(proc, req["exitFile"]), daemon=True).start()
    return {"ok": True, "pid": proc.pid}


def _record_exit(proc: subprocess.Popen, path: str) -> None:
    code = proc.wait()
    if _is_copy:
        return
    with open(path + ".tmp", "w") as f:
        f.write("%d\n" % code)
    os.replace(path + ".tmp", path)


def stop_collectors() -> None:
    for proc in _collectors.values():
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass


def split(req: dict, cli: Lines) -> None:
    """Ask the runner host to split the job and relay its events to the CLI."""
    global _plan_dir
    _plan_dir = str(req["planDir"])
    conn = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    conn.settimeout(30)
    try:
        conn.connect((HOST_CID, HOST_PORT))
    except OSError as error:
        cli.send({"type": "failed", "reason": "the runner host did not answer (%s)" % error})
        return
    conn.settimeout(None)
    host = Lines(conn)
    host.send({"type": "hello", "protocol": PROTOCOL, "timingsKey": req.get("timingsKey")})
    welcome = host.recv()
    if not welcome or welcome.get("type") != "welcome":
        cli.send({"type": "failed", "reason": (welcome or {}).get("reason", "the runner host refused")})
        return
    host.send({"type": "split", "parts": int(req["parts"]), "timingsKey": req.get("timingsKey")})
    while True:
        msg = host.recv()
        if msg is None:
            if _is_copy:
                return
            resume_writes()
            cli.send({"type": "failed", "reason": "the runner host closed the connection"})
            return
        kind = msg.get("type")
        if kind == "pause-writes":
            pause_writes()
            host.send({"type": "writes-paused"})
            continue
        if kind == "resume-writes":
            resume_writes()
        if _is_copy:
            return
        cli.send(msg)
        if kind in ("finished", "failed"):
            break
    stop_collectors()
    nxt = cli.recv()
    if nxt and nxt.get("cmd") == "timings":
        host.send({"type": "timings", "key": nxt.get("key"), "data": nxt.get("data")})
        try:
            conn.settimeout(30)
            host.recv()  # the host's receipt
        except (OSError, ValueError):
            pass
    conn.close()


def serve_control(path: str, owner_uid: int) -> None:
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if os.path.exists(path):
        os.remove(path)
    srv.bind(path)
    os.chown(path, owner_uid, -1)
    os.chmod(path, 0o600)
    srv.listen(4)
    while True:
        conn, _ = srv.accept()
        uid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if uid not in (owner_uid, 0):
            conn.close()
            continue
        threading.Thread(target=_control_conn, args=(conn,), daemon=True).start()


def _control_conn(conn: socket.socket) -> None:
    cli = Lines(conn)
    try:
        req = cli.recv()
        if not req:
            return
        cmd = req.get("cmd")
        if cmd == "start-collector":
            cli.send(start_collector(req))
        elif cmd == "split":
            split(req, cli)
        elif cmd == "stop":
            stop_collectors()
            cli.send({"ok": True})
            conn.close()
            if not _is_copy:
                os._exit(0)
        else:
            cli.send({"ok": False, "error": "unknown command"})
    except Exception as error:
        try:
            cli.send({"type": "failed", "ok": False, "reason": "%s: %s" % (type(error).__name__, error)})
        except OSError:
            pass
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="split tests: in-job helper")
    parser.add_argument("--control", required=True, help="Unix socket for the CLI")
    parser.add_argument("--owner-uid", type=int, required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("the helper must run as root", file=sys.stderr)
        return 2
    threading.Thread(target=serve_copies, daemon=True).start()
    serve_control(args.control, args.owner_uid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
