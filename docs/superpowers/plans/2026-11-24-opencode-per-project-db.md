# OpenCode Per-Project Databases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On network filesystems, give each project its own OpenCode database and lock, and carry API-key logins between projects (and CPU architectures) through a host-only master credential file.

**Architecture:** `Opencode.run`'s network branch sets `OPENCODE_DB=projects/<id>.db` and locks `projects/<id>.lock` beside it. A new `SignalGuard` keeps the launcher's signal handling in place from lock to release. A new `Sandbox.capture` runs `opencode auth import/export` without a TTY, piping JSON in and out. A new `agtbox/opencode_store.py` holds project IDs and the master-credential file logic. `launch_lock` raises `LockHeld` so each caller chooses between exiting and warning.

**Tech Stack:** Python 3 stdlib only (`hashlib`, `json`, `tempfile`, `subprocess`, `signal`), `unittest` + `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-11-24-opencode-per-project-db-design.md`

## Global Constraints

- Stdlib only; no new dependencies. Tests: `python3 -m unittest discover -s test` from the repo root (Python 3.13 here; keep to ≥3.11 syntax).
- Override variable name: `AGTBOX_OPENCODE_NETFS` (`1` network path, `0` local path, unset detect, other → warn + detect). Read on the host only; never forwarded.
- Project ID: `<base>-<hash>`; `<base>` = basename of `realpath(app_dir)` with `[^A-Za-z0-9._-]` → `_`, `root` if empty; `<hash>` = first 12 hex of SHA-256 of `os.fsencode(realpath)`.
- Per-project files in `${AGENT_TOOLS}/opencode/projects/`: `<id>.db`, `<id>.path`, `<id>.lock`. Sandbox env: `OPENCODE_DB=projects/<id>.db`.
- Master: `~/.local/share/agent-box/opencode/credentials.json` (= `Path(core.AGENT_TOOLS).parent / "opencode"`), dir `0700` (chmod even if it exists), file `0600` via `mkstemp` + `fsync` + `os.replace`; lock `credentials.lock` beside it; no retry.
- Credential commands: `opencode auth import --standalone` (master JSON on stdin) and `opencode auth export --standalone` (stdout), timeout 60 s.
- Warnings: `Agent Box: warning: …` on stderr, best-effort (an `OSError` on write is swallowed); none change the exit status. The launcher's exit status is the agent's.
- Local-filesystem path is unchanged: shared `opencode.db`, no lock, `run_setup` then exec.
- Project-lock contention message is unchanged: "Agent Box: OpenCode is already locked by another launch." + `Owner: …` + `Lock file: …` + recovery text + `rm -- <quoted lock>`.

## Review Focus

- A hand-edited or corrupted master file (not JSON, not an array, entries without `id`) must warn, start the agent without seeding, and never be overwritten by the push-back. Test: Task 4 `test_corrupt_master_warns_and_is_not_overwritten`.
- A session that only used unauthenticated providers (a local LM Studio) exports `[]`: no master file, no master lock, no warning. Test: Task 4 `test_empty_export_creates_no_master`.
- Launching through a symlinked path (`-a ~/link-to-project`) must use the same database and lock as the real path. Test: Task 4 `test_symlinked_project_shares_database_and_lock`.
- Credentials carrying fields we don't model (OAuth `refresh`/`expires`, `metadata`) must round-trip through the merge verbatim. Test: Task 3 `test_unknown_fields_round_trip`.
- A hung `opencode auth export` must not hang the launcher or keep the lock forever. Test: Task 2 `test_capture_timeout_kills_child`.

---

### Task 1: `LockHeld` exception; unchanged project-lock message

**Files:**
- Modify: `agtbox/locking.py` (the `launch_lock` `except FileExistsError` block, ~lines 55-69)
- Modify: `agtbox/agents/opencode.py`
- Test: `test/test_opencode.py`

**Interfaces:**
- Produces: `agtbox.locking.LockHeld(path, details)` — `Exception` subclass with attributes `.path` (the `Path` passed to `launch_lock`) and `.details` (str: `"host H, launcher PID P, since T, project X"` or `"owner information unavailable"`); `str(err)` contains `details`.
- Produces: `agtbox.agents.opencode.lock_held_exit(err: LockHeld) -> SystemExit` carrying today's exact message.

- [ ] **Step 1: Update the tests to the new contract**

In `test/test_opencode.py`:

Change the top-level imports at the head of the file (not the copies of `from agtbox.locking import …` inside the subprocess script strings further down):

```python
from agtbox.agents.opencode import NETWORK_FSTYPES, Opencode, lock_held_exit
from agtbox.context import RunContext
from agtbox.locking import LockHeld, filesystem_type, launch_lock
```

In `SharedStorage.test_nfs_holds_global_lock_through_setup_and_exit`, replace

```python
            with self.assertRaisesRegex(SystemExit, "already locked"), \
                 launch_lock(self.lock, "/other/project"):
```

with

```python
            with self.assertRaises(LockHeld), launch_lock(self.lock, "/other/project"):
```

Replace `LaunchLock.test_duplicate_launch_reports_owner_and_recovery` with:

```python
    def test_duplicate_launch_reports_owner_and_recovery(self):
        with mock.patch("agtbox.locking.socket.gethostname", return_value="frontend-1"), \
             launch_lock(self.lock, "/project"):
            owner = json.loads(self.lock.read_text())
            self.assertEqual(owner["host"], "frontend-1")
            self.assertEqual(owner["pid"], os.getpid())
            self.assertEqual(self.lock.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(LockHeld) as failure, launch_lock(self.lock, "/other-project"):
                self.fail("second owner entered")
            self.assertEqual(failure.exception.path, self.lock)
            self.assertIn("frontend-1", failure.exception.details)
            message = str(lock_held_exit(failure.exception))
            self.assertIn("Agent Box: OpenCode is already locked by another launch.", message)
            self.assertIn("frontend-1", message)
            self.assertIn("project /project", message)
            self.assertIn(str(os.getpid()), message)
            self.assertIn(f"Lock file: {self.lock}", message)
            self.assertIn("If the original run has stopped or its node is down", message)
            self.assertIn(f"rm -- {shlex.quote(str(self.lock))}", message)
            self.assertEqual(json.loads(self.lock.read_text()), owner)
        self.assertFalse(self.lock.exists())
```

In `test_partial_owner_file_still_blocks_launch`, change `self.assertRaisesRegex(SystemExit, "owner information unavailable")` to `self.assertRaisesRegex(LockHeld, "owner information unavailable")`.

In `test_process_lock_survives_crash_until_explicitly_removed`, replace the two lock-contention blocks:

```python
                with self.assertRaises(LockHeld), launch_lock(self.lock, "/project"):
                    self.fail("another process already owns this lock")
                child.send_signal(signal.SIGKILL)
                child.wait(timeout=5)
                with self.assertRaises(LockHeld) as stale, launch_lock(self.lock, "/project"):
                    self.fail("stale lock was automatically broken")
                self.assertIn("node is down", str(lock_held_exit(stale.exception)))
```

In `SharedStorage.test_other_project_is_blocked_before_setup`, after the `with` block add the message check by catching it:

```python
    def test_other_project_is_blocked_before_setup(self):
        ctx = self.context("/other/project")
        sandbox = mock.Mock()
        with launch_lock(self.lock, "/first/project"), \
             mock.patch("agtbox.agents.opencode.filesystem_type", return_value="nfs"), \
             self.assertRaises(SystemExit) as blocked:
            ctx.agent.run(sandbox, ctx)
        message = str(blocked.exception.code)
        self.assertIn("Agent Box: OpenCode is already locked by another launch.", message)
        self.assertIn("project /first/project", message)
        self.assertIn(f"rm -- {shlex.quote(str(self.lock))}", message)
        self.assertEqual(sandbox.method_calls, [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test.test_opencode -v`
Expected: ERROR at import — `ImportError: cannot import name 'lock_held_exit'` (and `LockHeld`).

- [ ] **Step 3: Implement `LockHeld`**

In `agtbox/locking.py`, add above `launch_lock`:

```python
class LockHeld(Exception):
    """Another owner holds a launch lock. Callers decide whether that is fatal."""

    def __init__(self, path, details):
        super().__init__(f"{path} is held: {details}")
        self.path = path
        self.details = details
```

and replace the `except FileExistsError:` block in `launch_lock` with:

```python
    except FileExistsError:
        try:
            owner = json.loads(path.read_text())
            details = (f"host {owner['host']}, launcher PID {owner['pid']}, "
                       f"since {owner['started']}, project {owner['project']}")
        except (OSError, ValueError, KeyError, TypeError):
            details = "owner information unavailable"
        raise LockHeld(path, details) from None
```

Remove `import shlex` from `locking.py` if nothing else uses it (it won't).

- [ ] **Step 4: Move the message to the OpenCode agent**

In `agtbox/agents/opencode.py`, add `import shlex`, import `LockHeld` (`from agtbox.locking import LockHeld, filesystem_type, launch_lock`), and add at module level:

```python
def lock_held_exit(err):
    """The project-lock contention message; its wording is kept stable."""
    return SystemExit(
        "Agent Box: OpenCode is already locked by another launch.\n"
        f"Owner: {err.details}\nLock file: {err.path}\n"
        "If the original run has stopped or its node is down, remove the lock "
        "and retry:\n"
        f"  rm -- {shlex.quote(str(err.path))}")
```

In `Opencode.run`, wrap the existing lock use:

```python
        try:
            with launch_lock(lock, ctx.app_dir):
                print(f"Agent Box: OpenCode launch lock: {lock}", file=sys.stderr)
                sandbox.run_setup(ctx)
                # exec would discard the host-side lock cleanup. Wait for the sandbox
                # instead, preserving its terminal and exit status.
                code = sandbox.wait(ctx)
        except LockHeld as err:
            raise lock_held_exit(err) from None
        raise SystemExit(code)
```

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest discover -s test`
Expected: `OK` (142 tests).

- [ ] **Step 6: Commit**

```bash
git add agtbox/locking.py agtbox/agents/opencode.py test/test_opencode.py
git commit -m "locking: raise LockHeld; OpenCode turns it into the same exit message"
```

---

### Task 2: `SignalGuard`, `Sandbox.capture`, and a `tty` override

**Files:**
- Modify: `agtbox/sandboxes/base.py` (`build_run_argv` abstract signature; `wait`; new `SignalGuard`, `capture`, `CAPTURE_TIMEOUT_CODE`)
- Modify: `agtbox/sandboxes/bwrap.py:92` (`build_run_argv` signature)
- Modify: `agtbox/sandboxes/podman.py:82-87` (`build_run_argv` signature + `tty`)
- Test: `test/test_sandboxes.py`

**Interfaces:**
- Produces: `agtbox.sandboxes.base.SignalGuard` — context manager; `__enter__` returns itself; `.attach(child, deliver_pending: bool)`, `.detach()`, `.pending: list[int]`.
- Produces: `Sandbox.wait(ctx, guard=None) -> int` (unchanged without `guard`).
- Produces: `Sandbox.capture(ctx, cmd, stdin=None, timeout=None, guard=None) -> tuple[int, str]`; `stdin` is a `str`; on timeout returns `(CAPTURE_TIMEOUT_CODE, "")` where `CAPTURE_TIMEOUT_CODE = 124`.
- Produces: `build_run_argv(ctx, cmd=None, tty=None)` on every sandbox; podman omits `-t` when `tty=False`.

- [ ] **Step 1: Write the failing tests**

In `test/test_sandboxes.py`, add `import select`, `import subprocess`, `import sys`, `import time` to the imports and change `FakeSandbox.build_run_argv` to accept and record `tty`:

```python
    def build_run_argv(self, ctx, cmd=None, tty=None):
        self.last_tty = tty
        return ["fake", *self.bind_args(ctx), *(cmd or self.command(ctx))]
```

Add a subclass and new test classes after `class Wait`:

```python
class ScriptSandbox(FakeSandbox):
    """Runs a Python snippet instead of a real sandbox."""
    def __init__(self, script):
        self.script = script
        self.last_tty = "unset"

    def build_run_argv(self, ctx, cmd=None, tty=None):
        self.last_tty = tty
        return [sys.executable, "-c", self.script]


class Capture(unittest.TestCase):
    def test_capture_pipes_stdin_and_returns_stdout_without_tty(self):
        sandbox = ScriptSandbox("import sys; print(sys.stdin.read().upper(), end='')")
        self.assertEqual(sandbox.capture(None, ["x"], stdin="hello"), (0, "HELLO"))
        self.assertIs(sandbox.last_tty, False)

    def test_capture_without_stdin_reads_devnull(self):
        sandbox = ScriptSandbox("import sys; print(repr(sys.stdin.read()), end='')")
        self.assertEqual(sandbox.capture(None, ["x"]), (0, "''"))

    def test_capture_reports_nonzero_exit(self):
        sandbox = ScriptSandbox("import sys; print('partial'); sys.exit(3)")
        self.assertEqual(sandbox.capture(None, ["x"]), (3, "partial\n"))

    def test_capture_timeout_kills_child(self):
        sandbox = ScriptSandbox("import time; time.sleep(30)")
        start = time.monotonic()
        self.assertEqual(sandbox.capture(None, ["x"], timeout=0.5), (124, ""))
        self.assertLess(time.monotonic() - start, 10)

    def test_capture_runs_in_its_own_session(self):
        sandbox = ScriptSandbox("import os; print(os.getsid(0) == os.getpid(), end='')")
        self.assertEqual(sandbox.capture(None, ["x"]), (0, "True"))


class Guard(unittest.TestCase):
    def test_pending_signals_go_only_to_a_child_that_takes_them(self):
        guard = sandbox_base.SignalGuard()
        guard._forward(signal.SIGTERM, None)          # no child attached: recorded
        self.assertEqual(guard.pending, [signal.SIGTERM])
        capture_child = mock.Mock()
        guard.attach(capture_child, deliver_pending=False)
        capture_child.send_signal.assert_not_called()  # earlier signals stay for the agent
        guard._forward(signal.SIGHUP, None)           # forwarded AND kept for the agent
        capture_child.send_signal.assert_called_once_with(signal.SIGHUP)
        self.assertEqual(guard.pending, [signal.SIGTERM, signal.SIGHUP])
        guard.detach()
        agent = mock.Mock()
        guard.attach(agent, deliver_pending=True)
        self.assertEqual(agent.send_signal.call_args_list,
                         [mock.call(signal.SIGTERM), mock.call(signal.SIGHUP)])
        self.assertEqual(guard.pending, [])
        guard._forward(signal.SIGTERM, None)          # to the agent only, not recorded
        self.assertEqual(agent.send_signal.call_count, 3)
        self.assertEqual(guard.pending, [])

    def test_wait_uses_callers_guard_without_restoring_handlers(self):
        child = mock.MagicMock()
        child.__enter__.return_value = child
        child.wait.return_value = 0
        guard = mock.Mock()
        with mock.patch("agtbox.sandboxes.base.subprocess.Popen", return_value=child), \
             mock.patch("agtbox.sandboxes.base.signal.signal") as install, \
             mock.patch.object(FakeSandbox, "build_run_argv", return_value=["fake"]):
            self.assertEqual(FakeSandbox().wait(None, guard=guard), 0)
        install.assert_not_called()
        guard.attach.assert_called_once_with(child, deliver_pending=True)
        guard.detach.assert_called_once_with()

    def _launcher(self, signal_to_send):
        script = """
import sys
from agtbox.sandboxes.base import SignalGuard
from agtbox.sandboxes.bwrap import Bwrap
child = '''
import sys, time
print("ready", file=sys.stderr, flush=True)
time.sleep(1)
print("done")
'''
sandbox = Bwrap()
sandbox.build_run_argv = lambda ctx, cmd=None, tty=None: [sys.executable, "-c", child]
with SignalGuard() as guard:
    code, out = sandbox.capture(None, ["x"], timeout=10, guard=guard)
print(code, out.strip(), flush=True)
"""
        with subprocess.Popen([sys.executable, "-c", script],
                              cwd=Path(__file__).resolve().parents[1],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True) as launcher:
            try:
                self.assertTrue(select.select([launcher.stderr], [], [], 5)[0])
                self.assertEqual(launcher.stderr.readline(), "ready\n")
                if signal_to_send == signal.SIGINT:
                    os.killpg(launcher.pid, signal.SIGINT)   # what terminal Ctrl-C does
                else:
                    launcher.send_signal(signal_to_send)
                out, err = launcher.communicate(timeout=10)
                return launcher.returncode, out, err
            finally:
                if launcher.poll() is None:
                    launcher.kill()
                    launcher.wait(timeout=5)

    def test_ctrl_c_during_capture_does_not_interrupt_it(self):
        code, out, err = self._launcher(signal.SIGINT)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "0 done\n")

    def test_sigterm_during_capture_stops_child_not_launcher(self):
        code, out, err = self._launcher(signal.SIGTERM)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.split()[0], str(-signal.SIGTERM))
```

Also add `import agtbox.sandboxes.base as sandbox_base` near the other imports.

In `class PodmanArgv`, add:

```python
    def test_tty_false_never_allocates_a_tty(self):
        from unittest import mock

        from agtbox.sandboxes.podman import Podman
        with mock.patch("agtbox.sandboxes.podman.sys.stdin.isatty", return_value=True), \
             mock.patch("agtbox.sandboxes.podman.sys.stdout.isatty", return_value=True):
            argv = Podman().build_run_argv(self._ctx(), ["X"], tty=False)
        self.assertNotIn("-t", argv)
        self.assertIn("-i", argv)
```

In `class BwrapArgv`, add:

```python
    def test_tty_argument_is_accepted_and_ignored(self):
        from agtbox.sandboxes.bwrap import Bwrap
        self.assertEqual(Bwrap().build_run_argv(self._ctx(), ["X"], tty=False),
                         Bwrap().build_run_argv(self._ctx(), ["X"]))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test.test_sandboxes -v`
Expected: FAIL/ERROR — `AttributeError: ... has no attribute 'capture'` / `'SignalGuard'`, `TypeError: ... unexpected keyword argument 'tty'`.

- [ ] **Step 3: Implement in `agtbox/sandboxes/base.py`**

Add `from contextlib import nullcontext` to the imports. Add above `class Sandbox`:

```python
CAPTURE_TIMEOUT_CODE = 124  # like timeout(1)


class SignalGuard:
    """The launcher's signal handling for a sandbox lifetime that spans several
    children (setup, credential commands, the agent).

    SIGTERM/SIGHUP go to the attached child. Unless that child is the agent
    (attached with deliver_pending=True) they are also recorded in `pending`, and
    the agent receives every recorded signal when it is attached: a SIGTERM that
    stops setup or a credential command must still stop the agent that would
    start next. Credential commands never receive signals sent before they ran.
    A terminal hangup can signal only the launcher as session leader, so SIGHUP
    must be forwarded. SIGINT runs a Python no-op: terminal Ctrl-C already reaches
    the foreground process group, and unlike SIG_IGN a Python handler resets on
    exec, so children keep default Ctrl-C handling.
    """

    def __init__(self):
        self.child = None
        self.pending = []
        self._child_is_agent = False
        self._previous = {}

    def _forward(self, signum, frame):
        if self.child is not None:
            self.child.send_signal(signum)
        if self.child is None or not self._child_is_agent:
            self.pending.append(signum)

    @staticmethod
    def _ignore(signum, frame):
        pass

    def __enter__(self):
        handlers = {signal.SIGTERM: self._forward, signal.SIGINT: self._ignore,
                    signal.SIGHUP: self._forward}
        self._previous = {s: signal.signal(s, h) for s, h in handlers.items()}
        return self

    def __exit__(self, *exc):
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        return False

    def attach(self, child, deliver_pending):
        self.child = child
        self._child_is_agent = deliver_pending
        if deliver_pending:
            pending, self.pending = self.pending, []
            for signum in pending:
                child.send_signal(signum)

    def detach(self):
        self.child = None
        self._child_is_agent = False
```

Change the abstract signature:

```python
    @abstractmethod
    def build_run_argv(self, ctx, cmd=None, tty=None): ...
```

Replace `wait` with:

```python
    def wait(self, ctx, guard=None):
        """Run without exec so the caller can clean up; returns the exit status.
        A caller whose cleanup also runs children passes its own SignalGuard."""
        with (SignalGuard() if guard is None else nullcontext(guard)) as active:
            with subprocess.Popen(self.build_run_argv(ctx)) as child:
                active.attach(child, deliver_pending=True)
                try:
                    code = child.wait()
                finally:
                    active.detach()
        return code if code >= 0 else 128 - code

    def capture(self, ctx, cmd, stdin=None, timeout=None, guard=None):
        """Run `cmd` in the sandbox without a TTY; return (returncode, stdout).

        `stdin` (str) is piped in, else /dev/null; stderr passes through. The child
        gets its own session so terminal Ctrl-C cannot interrupt it; with `guard`,
        SIGTERM/SIGHUP received while it runs are forwarded to it. A timeout kills
        it and returns (CAPTURE_TIMEOUT_CODE, "").
        """
        argv = self.build_run_argv(ctx, cmd, tty=False)
        with subprocess.Popen(argv, text=True, start_new_session=True,
                              stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                              stdout=subprocess.PIPE) as child:
            if guard is not None:
                guard.attach(child, deliver_pending=False)
            try:
                out, _ = child.communicate(stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                return CAPTURE_TIMEOUT_CODE, ""
            finally:
                if guard is not None:
                    guard.detach()
        return child.returncode, out
```

- [ ] **Step 4: Implement the `tty` override in both sandboxes**

`agtbox/sandboxes/bwrap.py`:

```python
    def build_run_argv(self, ctx, cmd=None, tty=None):
        # tty is accepted for interface parity: bwrap never allocates a terminal.
        bw = self.base_args(ctx.env)
```

(rest of the method unchanged).

`agtbox/sandboxes/podman.py`, replace the head of `build_run_argv`:

```python
    def build_run_argv(self, ctx, cmd=None, tty=None):
        # -t (allocate a TTY) only when actually attached to one, so an interactive
        # TUI works in a real terminal AND `agtbox bash -- -c ...` works in a pipe/CI
        # (podman -it in a non-TTY dies "the input device is not a TTY"). -i always.
        # tty=False forces no TTY: captured commands pipe stdin/stdout, and a TTY
        # would merge stderr into stdout and turn newlines into CRLF.
        if tty is None:
            tty = sys.stdin.isatty() and sys.stdout.isatty()
        pd = ["run", "-i", *(["-t"] if tty else []), "--rm", "--security-opt", "label=disable"]
```

(rest unchanged).

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest discover -s test`
Expected: `OK`. The existing `Wait` tests must still pass unchanged (handlers restored, SIGTERM/SIGHUP forwarded, SIGINT handler callable).

- [ ] **Step 6: Commit**

```bash
git add agtbox/sandboxes/base.py agtbox/sandboxes/bwrap.py agtbox/sandboxes/podman.py test/test_sandboxes.py
git commit -m "sandboxes: SignalGuard, capture without a TTY, tty override"
```

---

### Task 3: `agtbox/opencode_store.py` — project IDs and master credentials

**Files:**
- Create: `agtbox/opencode_store.py`
- Test: `test/test_opencode_store.py`

**Interfaces:**
- Produces (all in `agtbox.opencode_store`):
  - `project_id(app_dir: str) -> str`
  - `master_dir() -> Path`, `master_path() -> Path`, `master_lock_path() -> Path` (computed from `core.AGENT_TOOLS` at call time)
  - `ensure_master_dir() -> Path` (mkdir -p, then chmod 0700)
  - `parse_credentials(text: str) -> list[dict]` (raises `ValueError`)
  - `read_master() -> list[dict] | None` (`None` when absent; raises `ValueError`/`OSError`)
  - `write_master(credentials: list[dict]) -> None`
  - `merge_credentials(master: list[dict], exported: list[dict]) -> list[dict]` (pure; inputs untouched)

- [ ] **Step 1: Write the failing tests**

Create `test/test_opencode_store.py`:

```python
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agtbox import core
from agtbox import opencode_store as store


def cred(cid, integration="openai", active=False, key="sk", **extra):
    return {"id": cid, "integrationID": integration, "label": "API key",
            "active": active, "value": {"type": "key", "key": key}, **extra}


class ProjectId(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_basename_is_sanitized_and_hash_is_sha256_of_realpath(self):
        project = self.root / "my proj!"
        project.mkdir()
        digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
        self.assertEqual(store.project_id(str(project)), f"my_proj_-{digest}")

    def test_filesystem_root(self):
        self.assertRegex(store.project_id("/"), r"^root-[0-9a-f]{12}$")

    def test_symlink_resolves_to_the_same_id(self):
        project = self.root / "project"
        project.mkdir()
        link = self.root / "link"
        link.symlink_to(project, target_is_directory=True)
        self.assertEqual(store.project_id(str(link)), store.project_id(str(project)))

    def test_same_basename_in_different_dirs_differs(self):
        a, b = self.root / "a" / "app", self.root / "b" / "app"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        self.assertNotEqual(store.project_id(str(a)), store.project_id(str(b)))
        self.assertTrue(store.project_id(str(a)).startswith("app-"))

    def test_id_is_stable(self):
        self.assertEqual(store.project_id(str(self.root)), store.project_id(str(self.root)))


class ParseCredentials(unittest.TestCase):
    def test_accepts_an_array_of_credentials(self):
        self.assertEqual(store.parse_credentials(json.dumps([cred("a")])), [cred("a")])
        self.assertEqual(store.parse_credentials("[]"), [])

    def test_rejects_malformed_input(self):
        for text in ("not json", '{"id": "a"}', '[1]', '[{"integrationID": "openai"}]',
                     '[{"id": 5, "integrationID": "openai"}]', '[{"id": "a"}]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                store.parse_credentials(text)


class MasterFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patch = mock.patch.object(core, "AGENT_TOOLS", str(self.home / "agent-box" / "x86_64"))
        patch.start()
        self.addCleanup(patch.stop)
        self.dir = self.home / "agent-box" / "opencode"

    def test_paths_are_outside_the_arch_toolchain(self):
        self.assertEqual(store.master_dir(), self.dir)
        self.assertEqual(store.master_path(), self.dir / "credentials.json")
        self.assertEqual(store.master_lock_path(), self.dir / "credentials.lock")

    def test_absent_master_reads_as_none(self):
        self.assertIsNone(store.read_master())

    def test_corrupt_master_raises(self):
        self.dir.mkdir(parents=True)
        store.master_path().write_text("garbage")
        with self.assertRaises(ValueError):
            store.read_master()

    def test_write_round_trips_with_private_modes(self):
        self.dir.mkdir(parents=True, mode=0o755)
        self.dir.chmod(0o755)
        store.write_master([cred("a", active=True)])
        self.assertEqual(store.read_master(), [cred("a", active=True)])
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(store.master_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["credentials.json"])

    def test_failed_write_keeps_previous_master_and_no_temp_file(self):
        store.write_master([cred("a")])
        with mock.patch("agtbox.opencode_store.os.replace", side_effect=OSError("disk full")), \
             self.assertRaisesRegex(OSError, "disk full"):
            store.write_master([cred("b")])
        self.assertEqual(store.read_master(), [cred("a")])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["credentials.json"])


class Merge(unittest.TestCase):
    def test_new_credentials_are_inserted_inactive(self):
        merged = store.merge_credentials([], [cred("a")])
        self.assertEqual(merged, [cred("a", active=False)])

    def test_replace_updates_value_but_not_active(self):
        master = [cred("a", active=True, key="old")]
        merged = store.merge_credentials(master, [cred("a", active=False, key="new")])
        self.assertEqual(merged, [cred("a", active=True, key="new")])

    def test_master_only_credentials_are_kept(self):
        master = [cred("a"), cred("b", "anthropic")]
        self.assertEqual(store.merge_credentials(master, [cred("a", key="new")]),
                         [cred("a", key="new"), cred("b", "anthropic")])

    def test_first_active_export_credential_wins_per_integration(self):
        master = [cred("a", active=True), cred("x", "anthropic", active=True)]
        exported = [cred("b", active=True), cred("c", active=True)]
        merged = store.merge_credentials(master, exported)
        self.assertEqual({c["id"]: c["active"] for c in merged},
                         {"a": False, "x": True, "b": True, "c": False})

    def test_integration_without_active_export_keeps_master_flags(self):
        master = [cred("a", active=True)]
        merged = store.merge_credentials(master, [cred("b", active=False)])
        self.assertEqual({c["id"]: c["active"] for c in merged}, {"a": True, "b": False})

    def test_empty_export_is_a_no_op(self):
        master = [cred("a", active=True)]
        self.assertEqual(store.merge_credentials(master, []), master)

    def test_unknown_fields_round_trip(self):
        oauth = {"id": "o", "integrationID": "openai", "label": "ChatGPT", "active": True,
                 "value": {"type": "oauth", "refresh": "r", "access": "t", "expires": 1},
                 "metadata": {"plan": "plus"}}
        merged = store.merge_credentials([], [oauth])
        self.assertEqual(merged, [oauth])

    def test_inputs_are_not_mutated(self):
        master, exported = [cred("a", active=True)], [cred("a", key="new")]
        snapshot = json.dumps([master, exported])
        store.merge_credentials(master, exported)
        self.assertEqual(json.dumps([master, exported]), snapshot)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test.test_opencode_store -v`
Expected: ERROR — `ImportError: cannot import name 'opencode_store' from 'agtbox'`.

- [ ] **Step 3: Implement `agtbox/opencode_store.py`**

```python
"""OpenCode per-project storage: project IDs and the master credential file.

The master lives beside the per-arch toolchain dirs (one login serves every
arch) and outside every sandbox bind (agents cannot read or change it).
"""
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from agtbox import core


def project_id(app_dir):
    """`<basename>-<12 hex of sha256(realpath)>`: readable, unique per path."""
    real = os.path.realpath(app_dir)
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(real)) or "root"
    return f"{base}-{hashlib.sha256(os.fsencode(real)).hexdigest()[:12]}"


def master_dir():
    return Path(core.AGENT_TOOLS).parent / "opencode"


def master_path():
    return master_dir() / "credentials.json"


def master_lock_path():
    return master_dir() / "credentials.lock"


def ensure_master_dir():
    """Create the master dir and make it private even if it already existed
    (mkdir honours umask and leaves an existing dir's mode alone)."""
    path = master_dir()
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def parse_credentials(text):
    """Parse `opencode auth export` output; ValueError unless it is a list of
    objects with string `id` and `integrationID`."""
    data = json.loads(text)
    if not isinstance(data, list) or not all(
            isinstance(c, dict) and isinstance(c.get("id"), str)
            and isinstance(c.get("integrationID"), str) for c in data):
        raise ValueError("expected a JSON array of credentials with string "
                         "id and integrationID")
    return data


def read_master():
    """The master credentials, or None when there is no master yet."""
    try:
        text = master_path().read_text()
    except FileNotFoundError:
        return None
    return parse_credentials(text)


def write_master(credentials):
    """Replace the master atomically: a private temp file, fsync, then rename."""
    directory = ensure_master_dir()
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".credentials-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(credentials, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, master_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def merge_credentials(master, exported):
    """Fold one project's export into the master (see the design spec).

    By `id`: insert new credentials inactive; replace existing ones' fields but
    keep their `active`. Then, per integration that has an active credential in
    the export, the first such credential becomes the only active one. Master
    credentials missing from the export are kept (logouts don't propagate).
    """
    merged = copy.deepcopy(master)
    index = {c["id"]: i for i, c in enumerate(merged)}
    for cred in exported:
        if cred["id"] in index:
            entry = merged[index[cred["id"]]]
            active = entry.get("active", False)
            entry.clear()
            entry.update(copy.deepcopy(cred))
            entry["active"] = active
        else:
            index[cred["id"]] = len(merged)
            merged.append({**copy.deepcopy(cred), "active": False})
    chosen = {}
    for cred in exported:
        if cred.get("active") is True:
            chosen.setdefault(cred["integrationID"], cred["id"])
    for entry in merged:
        if entry["integrationID"] in chosen:
            entry["active"] = entry["id"] == chosen[entry["integrationID"]]
    return merged
```

Note `test_unknown_fields_round_trip`: the OAuth credential is new, so it's inserted inactive and then made active by the per-integration rule, so it equals the input exactly.

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest test.test_opencode_store -v`
Expected: all PASS. Then `python3 -m unittest discover -s test` → `OK`.

- [ ] **Step 5: Commit**

```bash
git add agtbox/opencode_store.py test/test_opencode_store.py
git commit -m "opencode_store: project IDs and the master credential file"
```

---

### Task 4: Per-project database, lock, seed and push-back in `Opencode.run`

**Files:**
- Modify: `agtbox/agents/opencode.py` (rewrite `run`; add `print_status`, `warn`, `uses_network_path`, `_locked_run`, `_seed`, `_push_back`)
- Test: `test/test_opencode.py` (rewrite `SharedStorage`; add `NetworkLaunch` process test)
- Test: `test/test_agtbox.py` (`LauncherTest._run`; the two `Tools.test_opencode_*` lock/storage tests, ~lines 80-90 and 235-275)

**Interfaces:**
- Consumes: `LockHeld`, `launch_lock`, `filesystem_type` (Task 1, `agtbox.locking`); `lock_held_exit` (Task 1); `SignalGuard`, `Sandbox.wait(ctx, guard=)`, `Sandbox.capture(ctx, cmd, stdin=, timeout=, guard=)` (Task 2); everything in `agtbox.opencode_store` (Task 3).
- Produces: `agtbox.agents.opencode.NETFS_OVERRIDE = "AGTBOX_OPENCODE_NETFS"`, `CREDENTIAL_TIMEOUT = 60`, `warn(message)`, `uses_network_path(data: Path) -> bool`.

- [ ] **Step 1: Rewrite the `SharedStorage` tests**

In `test/test_opencode.py`, update the imports:

```python
from agtbox import core
from agtbox import opencode_store as store
from agtbox.agents.opencode import NETWORK_FSTYPES, Opencode, lock_held_exit
from agtbox.context import RunContext
from agtbox.locking import LockHeld, filesystem_type, launch_lock
from agtbox.sandboxes.base import SignalGuard
```

Replace the whole `class SharedStorage` with:

```python
CRED = {"id": "cred_1", "integrationID": "openai", "label": "API key",
        "active": True, "value": {"type": "key", "key": "sk-test"}}


class SharedStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        patch = mock.patch.multiple(
            core, HOME=str(self.home), AGENT_STATE=str(self.home / "state"),
            AGENT_TOOLS=str(self.home / "tools"), AGENT_CONFIG=str(self.home / "config"),
            AGENT_CACHE=str(self.home / "cache"))
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGTBOX_OPENCODE_NETFS", None)
        self.data = self.home / "tools/opencode"
        self.projects = self.data / "projects"
        for bind in Opencode().binds:
            Path(bind.src).mkdir(parents=True)
        self.project = self.home / "project"
        self.project.mkdir()

    def context(self, path=None):
        agent = Opencode()
        return RunContext(agent=agent, binds=agent.binds,
                          env=core.resolve_env(agent.env_forward, agent.env_literal),
                          app_dir=str(path or self.project))

    def files(self, path=None):
        pid = store.project_id(str(path or self.project))
        return pid, self.projects / f"{pid}.db", self.projects / f"{pid}.lock"

    def sandbox(self, exported="[]", export_code=0, import_code=0, agent_code=0,
                creates_db=True):
        """A stub sandbox: the agent and a failed/successful import create the DB."""
        sandbox = mock.Mock()

        def capture(ctx, cmd, stdin=None, timeout=None, guard=None):
            db = self.data / dict(ctx.env)["OPENCODE_DB"]
            if cmd[1:3] == ["auth", "import"]:
                for suffix in ("", "-wal", "-shm"):
                    Path(f"{db}{suffix}").touch()
                return import_code, ""
            return export_code, exported

        def wait(ctx, guard=None):
            if creates_db:
                (self.data / dict(ctx.env)["OPENCODE_DB"]).touch()
            return agent_code

        sandbox.capture.side_effect = capture
        sandbox.wait.side_effect = wait
        return sandbox

    def launch(self, sandbox, ctx=None, fstype="nfs"):
        """Run the agent; return (exit code, stderr text)."""
        ctx = ctx or self.context()
        with mock.patch("agtbox.agents.opencode.filesystem_type", return_value=fstype), \
             mock.patch("agtbox.agents.opencode.sys.stderr") as err, \
             self.assertRaises(SystemExit) as result:
            ctx.agent.run(sandbox, ctx)
        return result.exception.code, "".join(c.args[0] for c in err.write.call_args_list)

    def commands(self, sandbox):
        return [c.args[1][1:3] for c in sandbox.capture.call_args_list]

    def test_projects_share_existing_data_state_config_and_cache(self):
        first, second = self.context("/one"), self.context("/two")
        self.assertEqual(first.binds, second.binds)
        sources = {bind.dst: bind.src for bind in first.binds}
        self.assertEqual(sources, {
            f"{self.home}/.config/opencode": f"{core.AGENT_CONFIG}/opencode",
            f"{self.home}/.local/share/opencode": f"{core.AGENT_TOOLS}/opencode",
            f"{self.home}/.local/state/opencode": f"{core.AGENT_STATE}/opencode",
            f"{self.home}/.cache/opencode": f"{core.AGENT_CACHE}/opencode",
        })

    def test_master_is_outside_every_sandbox_bind(self):
        mounted = [core.AGENT_TOOLS, core.AGENT_CACHE,
                   *(b.src for b in (*core.SHARED_BINDS, *Opencode().binds))]
        for path in (store.master_path(), store.master_lock_path()):
            for src in mounted:
                self.assertFalse(path.is_relative_to(src), (path, src))

    def test_host_database_override_is_not_forwarded(self):
        with mock.patch.dict(os.environ, {"OPENCODE_DB": "/different.db"}):
            ctx = self.context()
            self.assertNotIn("OPENCODE_DB", dict(ctx.env))
            self.launch(self.sandbox(), ctx)
        pid, _, _ = self.files()
        self.assertEqual(dict(ctx.env)["OPENCODE_DB"], f"projects/{pid}.db")

    def test_local_storage_uses_exec_without_lock(self):
        for fstype in ("ext2/ext3", "xfs", "apfs", "tmpfs"):
            ctx = self.context()
            sandbox = mock.Mock()
            with self.subTest(fstype=fstype), \
                 mock.patch("agtbox.agents.opencode.filesystem_type", return_value=fstype) as detect:
                ctx.agent.run(sandbox, ctx)
                detect.assert_called_once_with(self.data)
                self.assertEqual(sandbox.method_calls, [mock.call.run_setup(ctx), mock.call.run(ctx)])
                self.assertNotIn("OPENCODE_DB", dict(ctx.env))
                self.assertFalse(self.projects.exists())

    def test_network_path_uses_per_project_database_and_lock(self):
        pid, db, lock = self.files()
        for fstype in (*sorted(NETWORK_FSTYPES), None):
            ctx = self.context()
            sandbox = self.sandbox(agent_code=7)

            def while_locked(ctx, guard=None):
                self.assertTrue(lock.exists())
                self.assertIsInstance(guard, SignalGuard)
                with self.assertRaises(LockHeld), launch_lock(lock, "/other/project"):
                    self.fail("same project entered twice")
                db.touch()
                return 7

            sandbox.wait.side_effect = while_locked
            with self.subTest(fstype=fstype):
                code, err = self.launch(sandbox, ctx, fstype)
                self.assertEqual(code, 7)
                self.assertEqual(dict(ctx.env)["OPENCODE_DB"], f"projects/{pid}.db")
                self.assertEqual((self.projects / f"{pid}.path").read_text(),
                                 f"{self.project}\n")
                self.assertFalse(lock.exists())
                sandbox.run.assert_not_called()  # exec would discard cleanup
                self.assertIn(str(lock), err)
                self.assertEqual("cannot determine filesystem type" in err, fstype is None)

    def test_signal_guard_spans_setup_agent_and_export(self):
        seen = []
        sandbox = self.sandbox()
        sandbox.run_setup.side_effect = lambda ctx: seen.append(signal.getsignal(signal.SIGTERM))
        wait, capture = sandbox.wait.side_effect, sandbox.capture.side_effect

        def record_wait(ctx, guard=None):
            seen.append(signal.getsignal(signal.SIGTERM))
            return wait(ctx, guard)

        def record_capture(ctx, cmd, **kwargs):
            seen.append(signal.getsignal(signal.SIGTERM))
            return capture(ctx, cmd, **kwargs)

        sandbox.wait.side_effect, sandbox.capture.side_effect = record_wait, record_capture
        before = signal.getsignal(signal.SIGTERM)
        self.launch(sandbox)
        self.assertEqual(len(seen), 3)                      # setup, agent, export
        self.assertTrue(all(h == seen[0] for h in seen))
        self.assertIsInstance(seen[0].__self__, SignalGuard)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_different_projects_run_concurrently(self):
        other = self.home / "other"
        other.mkdir()
        _, _, other_lock = self.files(other)
        self.projects.mkdir(parents=True)
        with launch_lock(other_lock, str(other)):
            code, _ = self.launch(self.sandbox())
        self.assertEqual(code, 0)

    def test_same_project_is_blocked_with_owner_message(self):
        _, _, lock = self.files()
        self.projects.mkdir(parents=True)
        sandbox = self.sandbox()
        with launch_lock(lock, "/first/project"):
            code, _ = self.launch(sandbox)
        self.assertIn("Agent Box: OpenCode is already locked by another launch.", code)
        self.assertIn("project /first/project", code)
        self.assertIn(f"rm -- {shlex.quote(str(lock))}", code)
        self.assertEqual(sandbox.method_calls, [])

    def test_symlinked_project_shares_database_and_lock(self):
        link = self.home / "link"
        link.symlink_to(self.project, target_is_directory=True)
        self.assertEqual(self.files(link), self.files())
        _, _, lock = self.files()
        self.projects.mkdir(parents=True)
        with launch_lock(lock, str(self.project)):
            code, _ = self.launch(self.sandbox(), self.context(link))
        self.assertIn("already locked", code)

    def test_new_project_is_seeded_from_master_on_stdin(self):
        store.write_master([CRED])
        sandbox = self.sandbox()
        self.launch(sandbox)
        self.assertEqual(self.commands(sandbox), [["auth", "import"], ["auth", "export"]])
        seed = sandbox.capture.call_args_list[0]
        self.assertEqual(seed.args[1][1:], ["auth", "import", "--standalone"])
        self.assertEqual(json.loads(seed.kwargs["stdin"]), [CRED])
        self.assertEqual(seed.kwargs["timeout"], 60)
        self.assertEqual([c[0] for c in sandbox.method_calls],
                         ["run_setup", "capture", "wait", "capture"])

    def test_existing_project_is_not_seeded(self):
        store.write_master([CRED])
        _, db, _ = self.files()
        self.projects.mkdir(parents=True)
        db.touch()
        sandbox = self.sandbox()
        self.launch(sandbox)
        self.assertEqual(self.commands(sandbox), [["auth", "export"]])

    def test_no_master_and_no_database_means_no_credential_commands(self):
        sandbox = self.sandbox(creates_db=False)
        code, err = self.launch(sandbox)
        self.assertEqual(code, 0)
        sandbox.capture.assert_not_called()
        self.assertNotIn("warning", err)
        self.assertFalse(self.files()[1].exists())

    def test_failed_import_deletes_its_files_before_the_agent_and_is_not_retried(self):
        store.write_master([CRED])
        _, db, _ = self.files()
        sandbox = self.sandbox(import_code=1)
        agent_saw = []
        wait = sandbox.wait.side_effect

        def record(ctx, guard=None):
            agent_saw.extend(Path(f"{db}{s}").exists() for s in ("", "-wal", "-shm"))
            return wait(ctx, guard)

        sandbox.wait.side_effect = record
        code, err = self.launch(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(agent_saw, [False, False, False])  # deleted before the agent ran
        self.assertIn("`opencode auth import` failed", err)
        self.assertIn("log in", err)
        self.assertNotIn("tries again", err)
        self.assertTrue(db.exists())                          # the agent made a fresh one
        again = self.sandbox()
        self.launch(again)
        self.assertEqual(self.commands(again), [["auth", "export"]])  # not re-seeded

    def test_credential_command_spawn_failure_warns_and_keeps_exit_status(self):
        store.write_master([CRED])
        sandbox = self.sandbox(agent_code=6)
        sandbox.capture.side_effect = OSError("cannot start podman")
        code, err = self.launch(sandbox)
        self.assertEqual(code, 6)
        self.assertEqual(err.count("cannot start podman"), 2)  # seed and push-back
        self.assertEqual(store.read_master(), [CRED])

    def test_export_is_merged_into_master_after_nonzero_exit(self):
        code, _ = self.launch(self.sandbox(exported=json.dumps([CRED]), agent_code=3))
        self.assertEqual(code, 3)
        self.assertEqual(store.read_master(), [CRED])

    def test_empty_export_creates_no_master(self):
        code, err = self.launch(self.sandbox(exported="[]"))
        self.assertEqual(code, 0)
        self.assertFalse(store.master_dir().exists())
        self.assertNotIn("warning", err)

    def test_export_failure_or_bad_output_warns_and_keeps_master(self):
        store.write_master([CRED])
        _, db, _ = self.files()
        self.projects.mkdir(parents=True)
        db.touch()
        for kwargs in ({"export_code": 1}, {"exported": "not json"}, {"export_code": 124}):
            with self.subTest(**kwargs):
                code, err = self.launch(self.sandbox(agent_code=4, **kwargs))
                self.assertEqual(code, 4)
                self.assertIn("Agent Box: warning:", err)
                self.assertEqual(store.read_master(), [CRED])

    def test_corrupt_master_warns_and_is_not_overwritten(self):
        store.ensure_master_dir()
        store.master_path().write_text("not json")
        sandbox = self.sandbox(exported=json.dumps([CRED]), agent_code=2)
        code, err = self.launch(sandbox)
        self.assertEqual(code, 2)
        self.assertEqual(self.commands(sandbox), [["auth", "export"]])  # no seed
        self.assertIn("cannot read OpenCode master credentials", err)
        self.assertIn("cannot update OpenCode master credentials", err)
        self.assertEqual(store.master_path().read_text(), "not json")

    def test_held_master_lock_warns_and_keeps_exit_status(self):
        store.ensure_master_dir()
        with launch_lock(store.master_lock_path(), "/other/project"):
            code, err = self.launch(self.sandbox(exported=json.dumps([CRED]), agent_code=5))
        self.assertEqual(code, 5)
        self.assertIn("not saved", err)
        self.assertIn("project /other/project", err)
        self.assertIn(f"rm -- {shlex.quote(str(store.master_lock_path()))}", err)
        self.assertIsNone(store.read_master())

    def test_warnings_survive_a_dead_terminal(self):
        sandbox = self.sandbox(export_code=1)
        ctx = self.context()
        stderr = mock.Mock()
        stderr.write.side_effect = OSError(5, "Input/output error")
        with mock.patch("agtbox.agents.opencode.filesystem_type", return_value="nfs"), \
             mock.patch("agtbox.agents.opencode.sys.stderr", stderr), \
             self.assertRaises(SystemExit) as result:
            ctx.agent.run(sandbox, ctx)
        self.assertEqual(result.exception.code, 0)

    def test_netfs_override(self):
        cases = (("1", "ext2/ext3", True, False), ("0", "nfs", False, False),
                 ("yes", "nfs", True, True), ("yes", "xfs", False, True))
        for value, fstype, network, warned in cases:
            with self.subTest(value=value, fstype=fstype), \
                 mock.patch.dict(os.environ, {"AGTBOX_OPENCODE_NETFS": value}), \
                 mock.patch("agtbox.agents.opencode.filesystem_type", return_value=fstype) as detect, \
                 mock.patch("agtbox.agents.opencode.sys.stderr") as err:
                ctx, sandbox = self.context(), self.sandbox()
                try:
                    ctx.agent.run(sandbox, ctx)
                except SystemExit:
                    pass
                self.assertEqual(sandbox.wait.called, network)
                self.assertEqual(sandbox.run.called, not network)
                self.assertEqual(detect.called, value not in ("0", "1"))
                text = "".join(c.args[0] for c in err.write.call_args_list)
                self.assertEqual("AGTBOX_OPENCODE_NETFS" in text, warned)

    def test_setup_or_launch_failure_releases_lock_without_modifying_database(self):
        for name in ("opencode.db", "opencode.db-wal", "opencode.db-shm", "auth.json"):
            (self.data / name).write_text("existing contents")
        _, _, lock = self.files()
        for stage in ("run_setup", "wait"):
            with self.subTest(stage=stage), \
                 mock.patch("agtbox.agents.opencode.filesystem_type", return_value="nfs"), \
                 mock.patch("agtbox.agents.opencode.sys.stderr"):
                ctx, sandbox = self.context(), self.sandbox()
                getattr(sandbox, stage).side_effect = OSError("cannot launch")
                with self.assertRaisesRegex(OSError, "cannot launch"):
                    ctx.agent.run(sandbox, ctx)
                self.assertFalse(lock.exists())
        for file in self.data.iterdir():
            if file.is_file():
                self.assertEqual(file.read_text(), "existing contents")
```

Delete the old `test_lock_is_not_inside_a_sandbox_bind`, `test_nfs_holds_global_lock_through_setup_and_exit` and `test_other_project_is_blocked_before_setup` (their checks now live in `test_master_is_outside_every_sandbox_bind`, `test_network_path_uses_per_project_database_and_lock` and `test_same_project_is_blocked_with_owner_message`). `test_same_project_is_blocked_with_owner_message` reads the message from `code` because `SystemExit(message).code` is the message string.

Add an end-to-end process test class after `LaunchLock` (SIGTERM during the agent still exports and merges):

```python
class NetworkLaunch(unittest.TestCase):
    def test_sigterm_during_agent_still_saves_credentials_and_releases_lock(self):
        script = """
import sys
from pathlib import Path
from unittest import mock
from agtbox import core
home = Path(sys.argv[1])
with mock.patch.multiple(core, HOME=str(home), AGENT_TOOLS=str(home / "tools"),
                         AGENT_STATE=str(home / "state"), AGENT_CONFIG=str(home / "config"),
                         AGENT_CACHE=str(home / "cache")):
    from agtbox.agents.opencode import Opencode
    from agtbox.context import RunContext
    from agtbox.sandboxes.bwrap import Bwrap
    agent_worker = '''
import signal, sys
from pathlib import Path
Path(sys.argv[1]).touch()
signal.signal(signal.SIGTERM, lambda s, f: sys.exit(42))
print("ready", flush=True)
signal.pause()
'''
    export = ('[{"id": "cred_1", "integrationID": "openai", "label": "API key", '
              '"active": true, "value": {"type": "key", "key": "sk-test"}}]')

    class Stub(Bwrap):
        def build_run_argv(self, ctx, cmd=None, tty=None):
            if cmd is None:
                db = Path(core.AGENT_TOOLS) / "opencode" / dict(ctx.env)["OPENCODE_DB"]
                return [sys.executable, "-c", agent_worker, str(db)]
            return [sys.executable, "-c", f"print({export!r})"]

        def run_setup(self, ctx):
            pass

    agent = Opencode()
    ctx = RunContext(agent=agent, binds=agent.binds, env=[], app_dir=str(home / "project"))
    agent.run(Stub(), ctx)
"""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / "project").mkdir()
            env = {**os.environ, "AGTBOX_OPENCODE_NETFS": "1"}
            with subprocess.Popen([sys.executable, "-c", script, str(home)],
                                  cwd=Path(__file__).resolve().parents[1], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) as launcher:
                try:
                    self.assertTrue(select.select([launcher.stdout], [], [], 10)[0])
                    self.assertEqual(launcher.stdout.readline(), "ready\n")
                    launcher.terminate()
                    _, stderr = launcher.communicate(timeout=20)
                    self.assertEqual(launcher.returncode, 42, stderr)
                finally:
                    if launcher.poll() is None:
                        launcher.kill()
                        launcher.wait(timeout=5)
            master = json.loads((home / "opencode" / "credentials.json").read_text())
            self.assertEqual([c["id"] for c in master], ["cred_1"])
            self.assertEqual(list((home / "tools/opencode/projects").glob("*.lock")), [])
```

Then update the stub-launcher tests in `test/test_agtbox.py` (they run the real launcher as a subprocess against stubbed `bwrap`/`podman`, and today assert the old global lock).

In `LauncherTest._run`, keep a leftover exported override from flipping these tests — insert right after `env["PYTHONPATH"] = str(REPO)`:

```python
        env.pop("AGTBOX_OPENCODE_NETFS", None)  # tests choose the path via the stat stub
```

Replace `Tools.test_opencode_shared_storage_in_both_sandboxes` and `Tools.test_opencode_nfs_lock_blocks_another_project_before_setup` with:

```python
    @unittest.skipUnless(sys.platform.startswith("linux"), "uses GNU stat stub")
    def test_opencode_storage_in_both_sandboxes(self):
        from agtbox.opencode_store import project_id
        data = self.tools / "opencode"
        state = self.home / ".local/state/agent-box/opencode"
        projects = data / "projects"
        pid = project_id(str(self.app))
        for fstype in ("ext2/ext3", "nfs"):
            (self.stub / "stat").write_text(f"#!/bin/sh\nprintf '{fstype}\\n'\n")
            for engine in ("bwrap", "podman"):
                with self.subTest(engine=engine, fstype=fstype):
                    rc, argv, err = self.launch("-s", engine, "-a", str(self.app), "opencode")
                    self.assertEqual(rc, 0, err)
                    if engine == "bwrap":
                        self.assertArg(argv, str(data))
                        self.assertArg(argv, str(state))
                        self.assertArg(argv, "--chdir")
                    else:
                        self.assertArg(argv, f"{data}:{self.home}/.local/share/opencode")
                        self.assertArg(argv, f"{state}:{self.home}/.local/state/opencode")
                        self.assertArg(argv, "-w")
                    self.assertArg(argv, "--standalone")
                    network = fstype == "nfs"
                    self.assertEqual(any(f"projects/{pid}.db" in arg for arg in argv), network)
                    self.assertEqual(any("OPENCODE_DB" in arg for arg in argv), network)
                    self.assertEqual("OpenCode launch lock:" in err, network)
                    if network:
                        self.assertEqual((projects / f"{pid}.path").read_text(),
                                         f"{os.path.realpath(self.app)}\n")
                        self.assertEqual(list(projects.glob("*.lock")), [])
                    else:
                        self.assertFalse(projects.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "uses GNU stat stub")
    def test_opencode_nfs_lock_blocks_the_same_project_before_setup(self):
        from agtbox.opencode_store import project_id
        (self.stub / "stat").write_text("#!/bin/sh\nprintf 'nfs\\n'\n")
        lock = self.tools / "opencode/projects" / f"{project_id(str(self.app))}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text('{"host":"frontend-1","pid":123,"started":"earlier","project":"/other"}')
        for engine in ("bwrap", "podman"):
            with self.subTest(engine=engine):
                rc, argv, err = self.launch("-s", engine, "-a", str(self.app), "opencode")
                self.assertEqual(rc, 1, err)
                self.assertNoArg(argv, str(self.tools / "bin/opencode"))
                self.assertIn("frontend-1", err)
                self.assertIn("project /other", err)
                self.assertIn(str(lock), err)
                self.assertIn("rm --", err)
                self.assertTrue(lock.exists())
```

(`ext2/ext3` runs first so the `projects/` directory doesn't exist yet when the local path is checked. The stub engines never create `<id>.db`, so no credential commands run and the argv seen is setup + agent only.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test.test_opencode test.test_agtbox -v`
Expected: many FAILs/ERRORs, e.g. `KeyError: 'OPENCODE_DB'` in the stub sandbox, `test_warnings_survive_a_dead_terminal` raising `OSError: [Errno 5] Input/output error`, and `test_opencode_storage_in_both_sandboxes` failing on the `projects/<id>.db` assertion.

- [ ] **Step 3: Rewrite `agtbox/agents/opencode.py`**

Replace the file with (`lock_held_exit` is kept verbatim from Task 1):

```python
import json
import os
import shlex
import sys
from pathlib import Path

from agtbox import core
from agtbox.agents.base import Agent
from agtbox.locking import LockHeld, filesystem_type, launch_lock
from agtbox.opencode_store import (ensure_master_dir, master_lock_path,
                                   merge_credentials, parse_credentials,
                                   project_id, read_master, write_master)
from agtbox.sandboxes.base import SignalGuard

# Linux `stat -f -c %T` names, plus macOS `/sbin/mount` names.
NETWORK_FSTYPES = frozenset({
    "nfs", "nfs4", "lustre", "gpfs", "cifs", "smb2", "smb3", "smbfs", "ceph",
    "afpfs", "webdav",
})
# Host-only test hook: 1 forces the network path, 0 the local one.
NETFS_OVERRIDE = "AGTBOX_OPENCODE_NETFS"
CREDENTIAL_TIMEOUT = 60


def print_status(message):
    """Best-effort stderr output: after a terminal hangup a write can fail (EIO)."""
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass


def warn(message):
    print_status(f"Agent Box: warning: {message}")


def lock_held_exit(err):
    """The project-lock contention message; its wording is kept stable."""
    return SystemExit(
        "Agent Box: OpenCode is already locked by another launch.\n"
        f"Owner: {err.details}\nLock file: {err.path}\n"
        "If the original run has stopped or its node is down, remove the lock "
        "and retry:\n"
        f"  rm -- {shlex.quote(str(err.path))}")


def uses_network_path(data):
    """Per-project databases on network storage (or when detection fails)."""
    override = os.environ.get(NETFS_OVERRIDE)
    if override in ("0", "1"):
        return override == "1"
    if override is not None:
        warn(f"{NETFS_OVERRIDE}={override!r} is not 0 or 1; detecting the "
             "filesystem type instead.")
    fstype = filesystem_type(data)
    if fstype is None:
        print_status(f"Agent Box: cannot determine filesystem type for {data}; "
                     "using per-project OpenCode databases.")
        return True
    return fstype in NETWORK_FSTYPES


class Opencode(Agent):
    name = "opencode"
    packages = ("@opencode/cli",)
    obsolete_packages = ("opencode-ai",)
    default_args = ("--standalone",)
    setup_args = ("service", "set", "disabled", "true")

    @property
    def binds(self):
        return [
            core.Bind(f"{core.AGENT_CONFIG}/opencode", f"{core.HOME}/.config/opencode"),
            core.Bind(f"{core.AGENT_TOOLS}/opencode", f"{core.HOME}/.local/share/opencode"),
            core.Bind(f"{core.AGENT_STATE}/opencode", f"{core.HOME}/.local/state/opencode"),
            core.Bind(f"{core.AGENT_CACHE}/opencode", f"{core.HOME}/.cache/opencode"),
        ]

    def run(self, sandbox, ctx):
        # Inspect the data mount, not the project: that is where the database lives.
        data = Path(core.AGENT_TOOLS) / self.name
        if not uses_network_path(data):
            return super().run(sandbox, ctx)

        # One database and one lock per project, side by side in the data dir.
        # OpenCode does not create the database's parent directory.
        projects = data / "projects"
        projects.mkdir(parents=True, exist_ok=True)
        pid = project_id(ctx.app_dir)
        lock = projects / f"{pid}.lock"
        try:
            with launch_lock(lock, ctx.app_dir), SignalGuard() as guard:
                code = self._locked_run(sandbox, ctx, projects, pid, guard)
        except LockHeld as err:
            raise lock_held_exit(err) from None
        raise SystemExit(code)

    def _locked_run(self, sandbox, ctx, projects, pid, guard):
        db = projects / f"{pid}.db"
        (projects / f"{pid}.path").write_text(os.path.realpath(ctx.app_dir) + "\n")
        is_new = not db.exists()  # before any command runs: `auth export` creates it
        ctx.env.append(("OPENCODE_DB", f"projects/{pid}.db"))
        print_status(f"Agent Box: OpenCode database: {db}\n"
                     f"Agent Box: OpenCode launch lock: {projects / f'{pid}.lock'}")
        sandbox.run_setup(ctx)
        if is_new:
            self._seed(sandbox, ctx, db, guard)
        # exec would discard the host-side cleanup. Wait for the sandbox instead,
        # preserving its terminal and exit status.
        code = sandbox.wait(ctx, guard=guard)
        if db.exists():  # absent if OpenCode failed before creating it
            self._push_back(sandbox, ctx, guard)
        return code

    def _seed(self, sandbox, ctx, db, guard):
        """Give a brand-new project database the master credentials."""
        try:
            credentials = read_master()
        except (OSError, ValueError) as err:
            warn(f"cannot read OpenCode master credentials ({err}); starting "
                 "without them.")
            return
        if not credentials:
            return
        try:
            code, _ = sandbox.capture(ctx, [self.bin, "auth", "import", "--standalone"],
                                      stdin=json.dumps(credentials),
                                      timeout=CREDENTIAL_TIMEOUT, guard=guard)
            failure = f"exit {code}" if code != 0 else None
        except OSError as err:
            failure = str(err)
        if failure:
            warn(f"`opencode auth import` failed ({failure}); this project starts "
                 "without saved credentials. Log in here once to fix it.")
            # The agent has not run and the lock is held: these files can only
            # hold what the failed import just created. The agent then creates a
            # fresh database, so this project is not seeded again later.
            for suffix in ("", "-wal", "-shm"):
                Path(f"{db}{suffix}").unlink(missing_ok=True)

    def _push_back(self, sandbox, ctx, guard):
        """Fold this project's credentials into the master."""
        try:
            code, out = sandbox.capture(ctx, [self.bin, "auth", "export", "--standalone"],
                                        timeout=CREDENTIAL_TIMEOUT, guard=guard)
        except OSError as err:
            warn(f"`opencode auth export` failed ({err}); this session's "
                 "credentials were not saved for new projects.")
            return
        if code != 0:
            warn(f"`opencode auth export` failed (exit {code}); this session's "
                 "credentials were not saved for new projects.")
            return
        try:
            exported = parse_credentials(out)
        except ValueError as err:
            warn(f"`opencode auth export` printed unexpected output ({err}); this "
                 "session's credentials were not saved for new projects.")
            return
        if not exported:
            return
        try:
            ensure_master_dir()
            with launch_lock(master_lock_path(), ctx.app_dir):
                write_master(merge_credentials(read_master() or [], exported))
        except LockHeld as err:
            warn("OpenCode master credentials are locked by another launch; this "
                 "session's credential changes were not saved to them.\n"
                 f"Owner: {err.details}\n"
                 "If that launch has stopped or its node is down, remove the lock:\n"
                 f"  rm -- {shlex.quote(str(err.path))}")
        except (OSError, ValueError) as err:
            warn(f"cannot update OpenCode master credentials ({err}); they were "
                 "left unchanged.")
```

- [ ] **Step 4: Run the OpenCode and launcher tests**

Run: `python3 -m unittest test.test_opencode test.test_agtbox -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m unittest discover -s test`
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add agtbox/agents/opencode.py test/test_opencode.py test/test_agtbox.py
git commit -m "opencode: per-project database and lock on network filesystems

Seeds new project databases from a host-only master credential file and
folds each session's credentials back into it. AGTBOX_OPENCODE_NETFS forces
either path for testing."
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md` (the OpenCode storage section: from "OpenCode keeps its existing **shared database…" through "…may still need further work.", ~lines 67-104)
- Modify: `agtbox/agents/README.md` (~lines 38 and 93)
- Modify: `CLAUDE.md` (line 30's OpenCode-data sentence; line 23's last sentence; the paragraphs starting "**OpenCode shared storage and network-filesystem locking.**", "`Opencode.run` checks the backing filesystem…" and "`Sandbox.wait` retains terminal I/O…", ~lines 39-43)

- [ ] **Step 1: Replace the README section**

Replace the README text described above with (the outer `~~~` fence is not part of the text):

~~~markdown
OpenCode shares its **config, state, cache, and software environment** across
projects. Its data lives under `~/.local/share/agent-box/<arch>/opencode/`,
mounted as `~/.local/share/opencode` inside each sandbox. The host's
`OPENCODE_DB` is not forwarded. `-a` still sets the sandbox's working directory
and primary writable project mount, even when launched from a subdirectory.

**On local disks** nothing changes: every project uses the one shared
`opencode.db` and launches run concurrently.

**When the data directory is on a network filesystem** (NFS, Lustre, GPFS,
CIFS/SMB, Ceph), or when detection fails (with a warning), each project gets its
own database and lock, so different projects run at the same time from any host:

```
~/.local/share/agent-box/<arch>/opencode/projects/<name>-<hash>.db    # the project's database
~/.local/share/agent-box/<arch>/opencode/projects/<name>-<hash>.path  # which project it belongs to
~/.local/share/agent-box/<arch>/opencode/projects/<name>-<hash>.lock  # held while it runs
```

`<name>` is the project directory's name and `<hash>` comes from its real path on
the launching node. Moving or renaming a project starts a fresh database (the old
one stays); a project reached through different mount paths on different nodes
gets different databases. The old shared `opencode.db` is left untouched; its
history is still readable on a local-disk host or with `AGTBOX_OPENCODE_NETFS=0`.

A second launch of the **same project** is refused with the owner's host,
launcher PID, start time, project, and an exact `rm -- …/<name>-<hash>.lock`
recovery command. A killed launcher or crashed host can leave the lock behind:
remove it only after confirming the original run has stopped or its node is down.
Locks are never broken automatically. The lock is cooperative (OpenCode run
outside Agent Box ignores it) and it doesn't make SQLite's
[WAL mode network-safe](https://sqlite.org/wal.html); it only keeps two Agent Box
launches off one database.

**Logins carry over to new projects.** When a session ends, Agent Box copies its
stored credentials into `~/.local/share/agent-box/opencode/credentials.json`
(private to you, shared by every CPU architecture, invisible to agents). A
project's *first* launch starts with those credentials. Log in once in any
project and every new project after it is authenticated. Limits:

- Projects that already have a database don't pick up later logins; log in there
  too if a key changes.
- If copying the credentials into a new project fails, Agent Box warns and that
  project starts without them (it isn't retried); log in there once.
- Logging out in one project doesn't remove the credential from the master.
- An old project with an outdated key can overwrite a newer one in the master
  when it exits; log in again to fix it.
- If two projects finish at the same moment, one of them warns that its changes
  weren't saved to the master (its own database still has them).
- Sign-in (OAuth) logins are copied too, but their refresh tokens rotate, so
  copies in other projects can go stale.

Set `AGTBOX_OPENCODE_NETFS=1` to use per-project databases on a local disk (for
testing), or `0` to force the shared database.
~~~

- [ ] **Step 2: Update `CLAUDE.md`**

In line 23, replace "on a network filesystem, OpenCode holds a global launch lock around setup and `sandbox.wait()` instead." with "on a network filesystem, OpenCode uses a per-project database and lock and runs setup, credential seeding, `sandbox.wait()`, and a credential push-back under that lock instead."

Replace the three paragraphs (`**OpenCode shared storage and network-filesystem locking.**` … through the `Sandbox.wait` paragraph ending "Other agents still use exec.") with:

```markdown
**OpenCode storage and network-filesystem locking.** Config/state/cache mounts stay shared. The data dir is `${AGENT_TOOLS}/opencode` (per arch). The host's `OPENCODE_DB` is not forwarded. `-a` sets the canonical working directory and primary project mount.

`uses_network_path` checks the data dir's backing filesystem with `filesystem_type` (`agtbox/locking.py`: GNU `stat -f -c %T` on Linux; longest matching mount in macOS `/sbin/mount`; resolves symlinks). Local types → ordinary setup/exec on the shared `opencode.db`. A type in `NETWORK_FSTYPES` (NFS, Lustre, GPFS, CIFS/SMB, Ceph, AFP, WebDAV), or failed detection (warning), → per-project path. Host-only `AGTBOX_OPENCODE_NETFS=1|0` forces either path (other values warn and detect).

Per-project path: `project_id` (`agtbox/opencode_store.py`) = sanitized basename of `realpath(app_dir)` + `-` + 12 hex of its SHA-256. The sandbox gets `OPENCODE_DB=projects/<id>.db` (relative → under the data dir; the launcher `mkdir`s `projects/`, which OpenCode won't). `projects/<id>.path` records the realpath. `projects/<id>.lock` (exclusive create, NFSv3+) is held from before setup until cleanup; it is inside the sandbox-visible data bind by choice (debuggable), so an agent could delete it. `launch_lock` raises `LockHeld`; the project lock turns it into the duplicate-launch `SystemExit` (host, PID, start, project, `rm --` line); no stale-lock guessing; inode check so an old owner never removes a replacement. The old shared `opencode.db` is never touched on this path.

Credentials: master `~/.local/share/agent-box/opencode/credentials.json` (outside the arch dir and every bind; dir 0700, file 0600, `mkstemp`+`fsync`+`os.replace`), guarded by `credentials.lock` (no retry: contention warns and skips). New project (`<id>.db` absent, checked under the lock before any command) + non-empty master → `opencode auth import --standalone` with the master on stdin; failure (or spawn `OSError`) warns "log in here" and deletes the just-created `<id>.db{,-wal,-shm}` before the agent starts — the agent then creates a fresh DB, so the seed is not retried. After the agent exits (any status), if `<id>.db` exists → `opencode auth export --standalone`, merged by `merge_credentials`: by `id`, insert inactive / replace keeping `active`; per integration the export's first active credential becomes the only active one; master-only entries kept; no stale protection. Both commands run via `Sandbox.capture` (no TTY — podman `build_run_argv(..., tty=False)` drops `-t`; own session so Ctrl-C can't hit them; 60 s timeout). Warnings after the agent are best-effort (`OSError` on stderr swallowed).

Signals: `SignalGuard` (`agtbox/sandboxes/base.py`) forwards SIGTERM/SIGHUP to the attached child and, unless that child is the agent, also records them; recorded signals are delivered to the agent when it starts (so a SIGTERM during setup or the seed import still stops it) and never to credential commands; and runs a Python no-op for SIGINT (terminal Ctrl-C already signals the foreground group; not SIG_IGN, so the child keeps default handling). SIGHUP must be forwarded because a terminal hangup can signal only the launcher as session leader. `Sandbox.wait(ctx, guard=None)` uses its own guard unless given one; OpenCode's per-project path holds one guard from lock to release, so a SIGTERM (e.g. Slurm) stops the agent but the push-back and lock cleanup still run. Exit status is the agent's. Crash/SIGKILL may leave locks. Other agents still use exec.
```

Also in `CLAUDE.md`, in the `~/.local/share/agent-box/<arch>` bullet, replace "OpenCode data (including its database and v2 credentials) also lives here under `opencode/`, shared by projects using this architecture." with "OpenCode data also lives here under `opencode/`: the shared `opencode.db` on local disks, or one `projects/<id>.db` per project on network filesystems (master credentials are kept outside, in `~/.local/share/agent-box/opencode/`)."

- [ ] **Step 3: Update `agtbox/agents/README.md`**

In the `run(sandbox, ctx)` row of the attribute table (~line 38), replace "On a network filesystem, OpenCode holds its shared launch lock across setup and `sandbox.wait(ctx)`, which forwards termination signals and returns the exit status." with "On a network filesystem, OpenCode uses a per-project database and lock, held across setup, credential seeding, `sandbox.wait(ctx)` (which forwards termination signals and returns the exit status) and the credential push-back."

Near the end (~line 93), replace "data/state/config/cache and a conditional network-filesystem launch lock)" with "data/state/config/cache, and per-project databases and locks on network filesystems)".

- [ ] **Step 4: Check nothing references the removed global lock**

Run: `grep -rn "launch\.lock\|opencode-<arch>\|global launch lock\|global lock\|shared launch lock" README.md CLAUDE.md agtbox`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md agtbox/agents/README.md
git commit -m "docs: per-project OpenCode databases and master credentials"
```

---

### Task 6: Manual verification on a real OpenCode

**Files:** none (verification only).

- [ ] **Step 1: Full suite**

Run: `python3 -m unittest discover -s test`
Expected: `OK`.

- [ ] **Step 2: Two projects at once under bwrap**

Use scratch projects under `$HOME` (bwrap mounts an empty tmpfs on `/tmp`, and podman on macOS only shares `$HOME`): `mkdir -p ~/oc-test/a ~/oc-test/b ~/oc-test/c ~/oc-test/d`. In two terminals:

```bash
AGTBOX_OPENCODE_NETFS=1 ./bin/agtbox.py -a ~/oc-test/a opencode
AGTBOX_OPENCODE_NETFS=1 ./bin/agtbox.py -a ~/oc-test/b opencode
```

Expected: both start; stderr shows `…/projects/a-<hash>.db` and `…/projects/b-<hash>.db`. A third `AGTBOX_OPENCODE_NETFS=1 ./bin/agtbox.py -a ~/oc-test/a opencode` is refused with the owner message naming this host.

- [ ] **Step 3: Login carries over to a new project**

In `a`, add an API key (`/connect` inside the TUI), then quit. Check `ls -l ~/.local/share/agent-box/opencode/` shows `credentials.json` with mode `-rw-------`. Then:

```bash
AGTBOX_OPENCODE_NETFS=1 ./bin/agtbox.py -a ~/oc-test/c opencode -- auth list
```

Expected: the key is listed as `stored` in `c` (its first launch was seeded from the master).

- [ ] **Step 4: Podman from an interactive terminal**

```bash
AGTBOX_OPENCODE_NETFS=1 ./bin/agtbox.py -s podman -a ~/oc-test/d opencode -- auth list
```

Expected: the key is listed (the seed import reached OpenCode through podman's stdin without a TTY), and no `Agent Box: warning:` about `auth export` after it exits.

- [ ] **Step 5: Local path is unchanged**

Run: `./bin/agtbox.py -a ~/oc-test/a opencode -- auth list`
Expected: no `OpenCode database:` line on stderr; lists the old shared database's credentials.

- [ ] **Step 6: Clean up**

```bash
rm -rf ~/oc-test
rm -f ~/.local/share/agent-box/*/opencode/projects/[abcd]-*
```

Leave `credentials.json` in place unless you want to start over.
