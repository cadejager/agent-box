import json
import os
import pty
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agtbox import core
from agtbox import opencode_store as store
from agtbox.agents.opencode import NETWORK_FSTYPES, Opencode, lock_held_exit
from agtbox.context import RunContext
from agtbox.locking import LockHeld, filesystem_type, launch_lock
from agtbox.sandboxes.base import SignalGuard


class FilesystemType(unittest.TestCase):
    def test_linux_types_and_unrecognized_type(self):
        for output, expected in (("nfs\n", "nfs"), ("nfs4\n", "nfs4"),
                                 ("ext2/ext3\n", "ext2/ext3"), ("tmpfs\n", "tmpfs"),
                                 ("", None), ("UNKNOWN (0x1234)", None)):
            with self.subTest(output=output), \
                 mock.patch("agtbox.locking.sys.platform", "linux"), \
                 mock.patch("agtbox.locking.subprocess.run", return_value=mock.Mock(stdout=output)):
                self.assertEqual(filesystem_type("/database"), expected)

    def test_linux_follows_symlink_and_passes_path_as_single_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "shared data"
            data.mkdir()
            alias = Path(tmp) / "alias"
            alias.symlink_to(data, target_is_directory=True)
            with mock.patch("agtbox.locking.sys.platform", "linux"), \
                 mock.patch("agtbox.locking.subprocess.run", return_value=mock.Mock(stdout="nfs\n")) as run:
                self.assertEqual(filesystem_type(alias), "nfs")
                self.assertEqual(run.call_args.args[0],
                                 ["stat", "-f", "-c", "%T", "--", str(data.resolve())])

    def test_detection_failures_return_unknown(self):
        for error in (FileNotFoundError("stat"), subprocess.CalledProcessError(1, "stat"),
                      subprocess.TimeoutExpired("stat", 5)):
            with self.subTest(error=error), \
                 mock.patch("agtbox.locking.sys.platform", "linux"), \
                 mock.patch("agtbox.locking.subprocess.run", side_effect=error):
                self.assertIsNone(filesystem_type("/database"))

    def test_macos_uses_deepest_mount_and_respects_path_boundaries(self):
        output = """/dev/disk1 on / (apfs, local)
server:/home on /Users/shared home (nfs, nodev)
/dev/disk2 on /Users/shared home/local (apfs, local)
"""
        for path, expected in (("/Users/shared home/db", "nfs"),
                               ("/Users/shared home", "nfs"),
                               ("/Users/shared home/local/db", "apfs"),
                               ("/Users/shared home/locality/db", "nfs"),
                               ("/Users/shared home2/db", "apfs"), ("/", "apfs")):
            with self.subTest(path=path), \
                 mock.patch("agtbox.locking.sys.platform", "darwin"), \
                 mock.patch("agtbox.locking.os.path.realpath", return_value=path), \
                 mock.patch("agtbox.locking.subprocess.run", return_value=mock.Mock(stdout=output)) as run:
                self.assertEqual(filesystem_type(path), expected)
                self.assertEqual(run.call_args.args[0], ["/sbin/mount"])

    def test_unreadable_mount_listing_is_unknown(self):
        with mock.patch("agtbox.locking.sys.platform", "darwin"), \
             mock.patch("agtbox.locking.subprocess.run", return_value=mock.Mock(stdout="invalid")):
            self.assertIsNone(filesystem_type("/database"))


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


class LaunchLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock = self.root / "launch.lock"

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

    def test_partial_owner_file_still_blocks_launch(self):
        self.lock.write_text('{"host":')
        with self.assertRaisesRegex(LockHeld, "owner information unavailable"), \
             launch_lock(self.lock, "/project"):
            self.fail("incomplete lock was ignored")
        self.assertTrue(self.lock.exists())

    def test_old_owner_cannot_remove_replacement_lock(self):
        with launch_lock(self.lock, "/project"):
            self.lock.unlink()
            self.lock.write_text("replacement")
        self.assertEqual(self.lock.read_text(), "replacement")

    def test_exception_releases_lock(self):
        with self.assertRaisesRegex(RuntimeError, "failed"), launch_lock(self.lock, "/project"):
            raise RuntimeError("failed")
        self.assertFalse(self.lock.exists())

    def test_process_lock_survives_crash_until_explicitly_removed(self):
        script = """
import sys
from pathlib import Path
from agtbox.locking import filesystem_type, launch_lock
with launch_lock(Path(sys.argv[1]), '/project'):
    print('locked', flush=True)
    sys.stdin.read()
"""
        with subprocess.Popen([sys.executable, "-c", script, str(self.lock)],
                              cwd=Path(__file__).resolve().parents[1],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as child:
            try:
                # Wait for the child to acquire ownership, without a timing race.
                self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                self.assertEqual(child.stdout.readline(), "locked\n")
                with self.assertRaises(LockHeld), launch_lock(self.lock, "/project"):
                    self.fail("another process already owns this lock")
                child.send_signal(signal.SIGKILL)
                child.wait(timeout=5)
                with self.assertRaises(LockHeld) as stale, launch_lock(self.lock, "/project"):
                    self.fail("stale lock was automatically broken")
                self.assertIn("node is down", str(lock_held_exit(stale.exception)))
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
        self.lock.unlink()
        with launch_lock(self.lock, "/project"):
            self.assertTrue(self.lock.exists())

    def test_launcher_forwards_termination_waits_and_cleans_up(self):
        script = """
import sys
from pathlib import Path
from agtbox.locking import filesystem_type, launch_lock
from agtbox.sandboxes.bwrap import Bwrap
worker = '''
import signal, sys, time
signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(42))
print('ready', flush=True)
# Not signal.pause(): a signal landing just before it would block forever.
deadline = time.time() + 5
while time.time() < deadline:
    time.sleep(0.05)
sys.exit(1)
'''
sandbox = Bwrap()
sandbox.build_run_argv = lambda ctx: [sys.executable, '-c', worker]
with launch_lock(Path(sys.argv[1]), '/project'):
    raise SystemExit(sandbox.wait(None))
"""
        with subprocess.Popen([sys.executable, "-c", script, str(self.lock)],
                              cwd=Path(__file__).resolve().parents[1],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True) as launcher:
            try:
                self.assertTrue(select.select([launcher.stdout], [], [], 5)[0])
                self.assertEqual(launcher.stdout.readline(), "ready\n")
                self.assertTrue(self.lock.exists())
                launcher.terminate()
                _, stderr = launcher.communicate(timeout=5)
                self.assertEqual(launcher.returncode, 42, stderr)
                self.assertFalse(self.lock.exists())
            finally:
                # Also clean up the worker if a regression leaves it running.
                try:
                    os.killpg(launcher.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                launcher.wait(timeout=5)


    def test_terminal_hangup_reaches_sandbox_and_releases_lock(self):
        script = """
import fcntl, sys, termios
from pathlib import Path
from agtbox.locking import launch_lock
from agtbox.sandboxes.bwrap import Bwrap
# This launcher is the session leader; acquire stdin as its controlling terminal.
fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
worker = '''
import signal, sys, time
from pathlib import Path
def hangup(signum, frame):
    # The lock must remain held until the worker has finished shutting down.
    sys.exit(42 if Path(sys.argv[1]).exists() else 43)
signal.signal(signal.SIGHUP, hangup)
print('ready', flush=True)
deadline = time.time() + 5
while time.time() < deadline:
    time.sleep(0.05)
sys.exit(1)
'''
sandbox = Bwrap()
sandbox.build_run_argv = lambda ctx: [sys.executable, '-c', worker, sys.argv[1]]
with launch_lock(Path(sys.argv[1]), '/project'):
    raise SystemExit(sandbox.wait(None))
"""
        master, slave = pty.openpty()
        try:
            with subprocess.Popen([sys.executable, "-c", script, str(self.lock)],
                                  cwd=Path(__file__).resolve().parents[1], stdin=slave,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, start_new_session=True) as launcher:
                os.close(slave)
                slave = None
                try:
                    self.assertTrue(select.select([launcher.stdout], [], [], 5)[0])
                    self.assertEqual(launcher.stdout.readline(), "ready\n")
                    self.assertTrue(self.lock.exists())
                    # Closing the terminal sends SIGHUP to the session leader,
                    # unlike killpg(), which would signal the worker directly.
                    os.close(master)
                    master = None
                    _, stderr = launcher.communicate(timeout=5)
                    self.assertEqual(launcher.returncode, 42, stderr)
                    self.assertFalse(self.lock.exists())
                finally:
                    # Also clean up the worker if a regression leaves it running.
                    try:
                        os.killpg(launcher.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    launcher.wait(timeout=5)
        finally:
            for fd in (master, slave):
                if fd is not None:
                    os.close(fd)

    def test_ctrl_c_reaches_sandbox_once(self):
        script = """
import sys
from pathlib import Path
from agtbox.locking import launch_lock
from agtbox.sandboxes.bwrap import Bwrap
worker = '''
import signal, sys, time
count = 0
def handler(signum, frame):
    global count
    count += 1
signal.signal(signal.SIGINT, handler)
print('ready', flush=True)
deadline = time.time() + 5
while not count and time.time() < deadline:
    time.sleep(0.05)
time.sleep(0.5)
sys.exit(count)
'''
sandbox = Bwrap()
sandbox.build_run_argv = lambda ctx: [sys.executable, '-c', worker]
with launch_lock(Path(sys.argv[1]), '/project'):
    raise SystemExit(sandbox.wait(None))
"""
        with subprocess.Popen([sys.executable, "-c", script, str(self.lock)],
                              cwd=Path(__file__).resolve().parents[1],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True) as launcher:
            try:
                self.assertTrue(select.select([launcher.stdout], [], [], 5)[0])
                self.assertEqual(launcher.stdout.readline(), "ready\n")
                os.killpg(launcher.pid, signal.SIGINT)  # what a terminal's Ctrl-C does
                _, stderr = launcher.communicate(timeout=5)
                self.assertEqual(launcher.returncode, 1, stderr)
                self.assertFalse(self.lock.exists())
            finally:
                # Also clean up the worker if a regression leaves it running.
                try:
                    os.killpg(launcher.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                launcher.wait(timeout=5)


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
import signal, sys, time
from pathlib import Path
Path(sys.argv[1]).touch()
signal.signal(signal.SIGTERM, lambda s, f: sys.exit(42))
print("ready", flush=True)
deadline = time.time() + 10
while time.time() < deadline:
    time.sleep(0.05)
sys.exit(1)
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
                                  text=True, start_new_session=True) as launcher:
                try:
                    self.assertTrue(select.select([launcher.stdout], [], [], 10)[0])
                    self.assertEqual(launcher.stdout.readline(), "ready\n")
                    launcher.terminate()
                    _, stderr = launcher.communicate(timeout=20)
                    self.assertEqual(launcher.returncode, 42, stderr)
                finally:
                    # Also clean up the agent if a regression leaves it running.
                    try:
                        os.killpg(launcher.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    launcher.wait(timeout=5)
            master = json.loads((home / "opencode" / "credentials.json").read_text())
            self.assertEqual([c["id"] for c in master], ["cred_1"])
            self.assertEqual(list((home / "tools/opencode/projects").glob("*.lock")), [])



if __name__ == "__main__":
    unittest.main()
