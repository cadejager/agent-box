import json
import os
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
from agtbox.agents.opencode import Opencode
from agtbox.context import RunContext
from agtbox.locking import filesystem_type, launch_lock


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


class SharedStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patch = mock.patch.multiple(
            core, HOME=str(self.home), AGENT_STATE=str(self.home / "state"),
            AGENT_TOOLS=str(self.home / "tools"), AGENT_CONFIG=str(self.home / "config"),
            AGENT_CACHE=str(self.home / "cache"))
        patch.start()
        self.addCleanup(patch.stop)
        self.data = self.home / "tools/opencode"
        self.lock = self.home / "state/opencode/launch.lock"
        for bind in Opencode().binds:
            Path(bind.src).mkdir(parents=True)

    def context(self, path):
        agent = Opencode()
        return RunContext(agent=agent, binds=agent.binds,
                          env=core.resolve_env(agent.env_forward, agent.env_literal),
                          app_dir=str(path))

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

    def test_host_database_override_is_not_forwarded(self):
        with mock.patch.dict(os.environ, {"OPENCODE_DB": "/different.db"}):
            ctx = self.context(self.home)
        self.assertNotIn("OPENCODE_DB", dict(ctx.env))

    def test_local_storage_uses_exec_without_lock(self):
        ctx = self.context("/nfs/project")
        sandbox = mock.Mock()
        # A lock left on storage that is now local is not needed for exclusion.
        self.lock.write_text("old lock")
        with mock.patch("agtbox.agents.opencode.filesystem_type", return_value="ext2/ext3") as detect:
            ctx.agent.run(sandbox, ctx)
        detect.assert_called_once_with(self.data)
        self.assertEqual(sandbox.method_calls, [mock.call.run_setup(ctx), mock.call.run(ctx)])
        self.assertEqual(self.lock.read_text(), "old lock")

    def test_nfs_holds_global_lock_through_setup_and_exit(self):
        ctx = self.context("/local/project")

        def while_locked(*args):
            self.assertTrue(self.lock.exists())
            with self.assertRaisesRegex(SystemExit, "already locked"), \
                 launch_lock(self.lock, "/other/project"):
                self.fail("second project entered")
            return 7

        for fstype in ("nfs", "nfs4", None):
            sandbox = mock.Mock()
            sandbox.run_setup.side_effect = while_locked
            sandbox.wait.side_effect = while_locked
            with self.subTest(fstype=fstype), \
                 mock.patch("agtbox.agents.opencode.filesystem_type", return_value=fstype), \
                 mock.patch("agtbox.agents.opencode.sys.stderr") as err, \
                 self.assertRaises(SystemExit) as result:
                ctx.agent.run(sandbox, ctx)
            self.assertEqual(result.exception.code, 7)
            self.assertFalse(self.lock.exists())
            sandbox.run.assert_not_called()  # exec would discard cleanup
            message = "".join(call.args[0] for call in err.write.call_args_list)
            self.assertIn(str(self.lock), message)
            self.assertEqual("cannot determine filesystem type" in message, fstype is None)

    def test_other_project_is_blocked_before_setup(self):
        ctx = self.context("/other/project")
        sandbox = mock.Mock()
        with launch_lock(self.lock, "/first/project"), \
             mock.patch("agtbox.agents.opencode.filesystem_type", return_value="nfs"), \
             self.assertRaisesRegex(SystemExit, "project /first/project"):
            ctx.agent.run(sandbox, ctx)
        self.assertEqual(sandbox.method_calls, [])

    def test_setup_or_launch_failure_releases_lock_without_modifying_database(self):
        ctx = self.context(self.home)
        for name in ("opencode.db", "opencode.db-wal", "opencode.db-shm", "auth.json"):
            (self.data / name).write_text("existing contents")
        for stage in ("run_setup", "wait"):
            with self.subTest(stage=stage), \
                 mock.patch("agtbox.agents.opencode.filesystem_type", return_value="nfs"), \
                 mock.patch("agtbox.agents.opencode.sys.stderr"):
                sandbox = mock.Mock()
                getattr(sandbox, stage).side_effect = OSError("cannot launch")
                with self.assertRaisesRegex(OSError, "cannot launch"):
                    ctx.agent.run(sandbox, ctx)
                self.assertFalse(self.lock.exists())
        for file in self.data.iterdir():
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
            with self.assertRaises(SystemExit) as failure, launch_lock(self.lock, "/other-project"):
                self.fail("second owner entered")
            message = str(failure.exception)
            self.assertIn("frontend-1", message)
            self.assertIn("project /project", message)
            self.assertIn(str(os.getpid()), message)
            self.assertIn(f"rm -- {shlex.quote(str(self.lock))}", message)
            self.assertEqual(json.loads(self.lock.read_text()), owner)
        self.assertFalse(self.lock.exists())

    def test_partial_owner_file_still_blocks_launch(self):
        self.lock.write_text('{"host":')
        with self.assertRaisesRegex(SystemExit, "owner information unavailable"), \
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
                with self.assertRaisesRegex(SystemExit, "already locked"), \
                     launch_lock(self.lock, "/project"):
                    self.fail("another process already owns this lock")
                child.send_signal(signal.SIGKILL)
                child.wait(timeout=5)
                with self.assertRaisesRegex(SystemExit, "node is down"), \
                     launch_lock(self.lock, "/project"):
                    self.fail("stale lock was automatically broken")
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
import signal, sys
signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(42))
print('ready', flush=True)
signal.pause()
'''
sandbox = Bwrap()
sandbox.build_run_argv = lambda ctx: [sys.executable, '-c', worker]
with launch_lock(Path(sys.argv[1]), '/project'):
    raise SystemExit(sandbox.wait(None))
"""
        with subprocess.Popen([sys.executable, "-c", script, str(self.lock)],
                              cwd=Path(__file__).resolve().parents[1],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) as launcher:
            try:
                self.assertTrue(select.select([launcher.stdout], [], [], 5)[0])
                self.assertEqual(launcher.stdout.readline(), "ready\n")
                self.assertTrue(self.lock.exists())
                launcher.terminate()
                _, stderr = launcher.communicate(timeout=5)
                self.assertEqual(launcher.returncode, 42, stderr)
                self.assertFalse(self.lock.exists())
            finally:
                if launcher.poll() is None:
                    launcher.kill()
                    launcher.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
