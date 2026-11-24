#!/usr/bin/env python3
"""Manual check: does a podman credential command outlive Sandbox.capture's timeout?

When `opencode auth import/export` hangs, `Sandbox.capture` times out and SIGKILLs
the podman *client*'s session. The suspicion is that conmon keeps the `--rm`
container running, so after a failed seed `Opencode._seed` deletes `<id>.db*`
while the import may still be writing.

This script runs a stand-in "hung credential command" (a loop that appends a
heartbeat line to a file every 0.5 s) under the real podman sandbox, then stops it
four ways and reports whether the container kept running:

  A  current code:  Sandbox.capture(timeout=3) -> SIGKILL to the client's session
  B  SIGTERM first: SIGTERM to the client's session (podman's sig-proxy forwards it)
  C  B + --init:    same, with `podman run --init` (the command is not PID 1, so
                    it doesn't ignore SIGTERM the way PID 1 does)
  D  by name:       SIGKILL the client, then `podman rm -f <name>`

Run on a host where podman works (not inside Agent Box), from the repo root:
    python3 test/manual/podman_capture_timeout.py
Needs the agent-box image (built automatically if missing, like a normal launch).
Takes about a minute. Every container it starts has a unique name and is removed
at the end; the scratch dir is created under $HOME (macOS podman only shares $HOME)
and deleted afterwards.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agtbox import core  # noqa: E402
from agtbox.context import RunContext  # noqa: E402
from agtbox.sandboxes.podman import Podman  # noqa: E402

TIMEOUT = 3      # stands in for CREDENTIAL_TIMEOUT (60 s in production)
SETTLE = 4       # seconds to watch the heartbeat after stopping
LOOP = 'echo started; while :; do date +%s >> "$1"; sleep 0.5; done'


def lines(path):
    try:
        return len(path.read_text().splitlines())
    except FileNotFoundError:
        return 0


def still_running(heartbeat, name):
    """(heartbeat grew while we watched, podman's status line for the container)."""
    before = lines(heartbeat)
    time.sleep(SETTLE)
    grew = lines(heartbeat) > before
    status = subprocess.run(["podman", "ps", "-a", "--filter", f"name=^{name}$",
                             "--format", "{{.Status}}"],
                            capture_output=True, text=True).stdout.strip()
    return grew, status or "gone"


def make_sandbox(name, init=False):
    """A Podman sandbox whose containers get a known name (and optionally --init)."""
    sandbox = Podman()
    original = sandbox.build_run_argv

    def build_run_argv(ctx, cmd=None, tty=None):
        argv = original(ctx, cmd, tty=tty)
        i = argv.index("run") + 1
        return argv[:i] + ["--name", name, *(["--init"] if init else [])] + argv[i:]

    sandbox.build_run_argv = build_run_argv
    return sandbox


def wait_for_heartbeat(heartbeat, deadline=60):
    end = time.time() + deadline
    while lines(heartbeat) == 0:
        if time.time() > end:
            raise SystemExit("the container never started writing its heartbeat")
        time.sleep(0.2)


def scenario_capture(ctx, work, name):
    """A: exactly what Opencode._seed/_push_back do today."""
    heartbeat = work / f"{name}.beat"
    sandbox = make_sandbox(name)
    start = time.time()
    code, _ = sandbox.capture(ctx, ["sh", "-c", LOOP, "sh", str(heartbeat)], timeout=TIMEOUT)
    took = time.time() - start
    return heartbeat, f"capture returned {code} after {took:.1f}s"


def scenario_signal(ctx, work, name, sig, init=False, remove=False):
    """B/C/D: start the same command, then stop it the way a fix would."""
    heartbeat = work / f"{name}.beat"
    sandbox = make_sandbox(name, init=init)
    argv = sandbox.build_run_argv(ctx, ["sh", "-c", LOOP, "sh", str(heartbeat)], tty=False)
    client = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              start_new_session=True)
    wait_for_heartbeat(heartbeat)
    time.sleep(1)
    os.killpg(client.pid, sig)
    try:
        client.wait(timeout=10)
        note = f"client exited {client.returncode}"
    except subprocess.TimeoutExpired:
        client.kill()
        client.wait()
        note = "client ignored the signal for 10s (then SIGKILLed)"
    if remove:
        rm = subprocess.run(["podman", "rm", "-f", name], capture_output=True, text=True)
        note += f"; podman rm -f exited {rm.returncode}"
    return heartbeat, note


def main():
    if not shutil.which("podman") or subprocess.run(
            ["podman", "info"], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode != 0:
        raise SystemExit("podman isn't usable here (`podman info` failed); "
                         "run this on a host where podman works.")

    work = Path(tempfile.mkdtemp(prefix="agtbox-podman-timeout-", dir=core.HOME))
    tag = uuid.uuid4().hex[:8]
    names = {key: f"agtbox-timeout-{key}-{tag}" for key in "ABCD"}
    ctx = RunContext(agent=None, binds=[], env=[("HOME", core.HOME)], app_dir=str(work))
    Podman().prepare(ctx)  # builds the image if missing, like a normal launch

    scenarios = {
        "A": ("current code: capture timeout -> SIGKILL client session",
              lambda: scenario_capture(ctx, work, names["A"])),
        "B": ("SIGTERM to client session",
              lambda: scenario_signal(ctx, work, names["B"], signal.SIGTERM)),
        "C": ("SIGTERM to client session, podman run --init",
              lambda: scenario_signal(ctx, work, names["C"], signal.SIGTERM, init=True)),
        "D": ("SIGKILL client session, then podman rm -f <name>",
              lambda: scenario_signal(ctx, work, names["D"], signal.SIGKILL, remove=True)),
    }
    results = {}
    try:
        for key, (title, run) in scenarios.items():
            print(f"[{key}] {title} ...", flush=True)
            heartbeat, note = run()
            grew, status = still_running(heartbeat, names[key])
            results[key] = grew
            verdict = ("STILL RUNNING after the stop" if grew
                       else "stopped")
            print(f"    {note}\n    container: {verdict} (podman ps: {status})", flush=True)
    finally:
        for name in names.values():
            subprocess.run(["podman", "rm", "-f", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(work, ignore_errors=True)

    print()
    if results.get("A"):
        print("PROBLEM CONFIRMED: after capture's timeout the container keeps running, "
              "so a failed seed's DB delete can race a still-running import.")
        fixes = [k for k in "BCD" if k in results and not results[k]]
        print("Stops it: " + (", ".join(fixes) if fixes else "none of B/C/D"))
    else:
        print("No problem: the container stops when capture times out; nothing to fix.")


if __name__ == "__main__":
    main()
