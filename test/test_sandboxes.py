import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import mock_open
import agtbox.core
from agtbox.context import RunContext
from agtbox.sandboxes.base import Sandbox


class FakeSandbox(Sandbox):
    name = "fake"
    priority = 0
    @classmethod
    def is_available(cls):
        return True
    def fmt_env(self, pairs):
        return [f"E:{k}={v}" for k, v in pairs]
    def fmt_bind(self, src, dst, ro):
        return [f"B:{src}>{dst}{':ro' if ro else ''}"]
    def build_run_argv(self, ctx):
        return ["fake", *self.bind_args(ctx)]
    def install_machine(self):
        return "x86_64"
    def install(self, script, pairs):
        pass


class BindArgs(unittest.TestCase):
    def test_seed_binds_skipped_volumes_emitted(self):
        from agtbox.core import Bind
        ctx = RunContext(agent=None,
                         binds=[Bind("/a", "/A"), Bind("/s", "/S", "seed")],
                         env=[], app_dir="/app", volumes=["/rw"],
                         ro_volumes=["/ro"], extra_args=[])
        out = FakeSandbox().bind_args(ctx)
        self.assertIn("B:/a>/A", out)
        self.assertNotIn("B:/s>/S", out)          # seed never bound
        self.assertIn("B:/rw>/rw", out)
        self.assertIn("B:/ro>/ro:ro", out)


class BwrapArgv(unittest.TestCase):
    def _ctx(self):
        from agtbox.context import RunContext
        from agtbox.core import Bind
        from agtbox.agents.claude import Claude
        return RunContext(agent=Claude(),
                          binds=[Bind("/cfg/claude", "/h/.claude")],
                          env=[("HOME", "/h"), ("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "1")],
                          app_dir="/app", volumes=[], ro_volumes=["/ro"],
                          extra_args=["--resume"])

    def test_run_argv_locked_down(self):
        from agtbox.sandboxes.bwrap import Bwrap
        argv = Bwrap().build_run_argv(self._ctx())
        for w in ("bwrap", "--clearenv", "/usr", "/etc", "--dev", "--proc",
                  "--die-with-parent", "--unshare-pid"):
            self.assertIn(w, argv)
        self.assertNotIn("--unshare-net", argv)       # network shared
        self.assertIn("/app", argv)
        self.assertIn("--chdir", argv)
        self.assertEqual(argv[-1], "--resume")        # extra args last
        self.assertIn("--setenv", argv)

    def test_install_runs_bwrap_with_given_pairs(self):
        from unittest import mock
        from agtbox.sandboxes.bwrap import Bwrap
        captured = {}
        with mock.patch("agtbox.sandboxes.bwrap.subprocess.run",
                        side_effect=lambda argv, **kw: captured.update(argv=argv)):
            Bwrap().install("SCRIPT", [("HTTPS_PROXY", "http://p"), ("AGT_NPM_PKGS", "x")])
        argv = captured["argv"]
        self.assertEqual(argv[0], "bwrap")
        self.assertIn("HTTPS_PROXY", argv)            # forwarded proxy reaches install
        self.assertEqual(argv[-3:], ["/usr/bin/bash", "-c", "SCRIPT"])


class PodmanArgv(unittest.TestCase):
    def _ctx(self):
        from agtbox.context import RunContext
        from agtbox.core import Bind
        from agtbox.agents.opencode import Opencode
        return RunContext(agent=Opencode(), binds=[Bind("/cfg/oc", "/h/.config/opencode")],
                          env=[("HOME", "/h")], app_dir="/app", volumes=[],
                          ro_volumes=["/ro"], extra_args=["--session", "Y"])

    def test_run_argv(self):
        from unittest import mock
        from agtbox.sandboxes.podman import Podman
        with mock.patch("agtbox.sandboxes.podman.sys.stdin.isatty", return_value=False), \
             mock.patch("agtbox.sandboxes.podman.sys.stdout.isatty", return_value=False):
            argv = Podman().build_run_argv(self._ctx())
        for w in ("podman", "run", "-i", "--rm", "--security-opt", "label=disable"):
            self.assertIn(w, argv)
        self.assertIn("-e", argv)
        self.assertIn("HOME=/h", argv)
        self.assertIn("/cfg/oc:/h/.config/opencode", argv)
        self.assertIn("/ro:/ro:ro", argv)
        self.assertIn("agent-box", argv)
        self.assertEqual(argv[-2:], ["--session", "Y"])
        self.assertNotIn("--clearenv", argv)

    def test_tty_allocated_only_when_attached(self):
        from unittest import mock
        from agtbox.sandboxes.podman import Podman
        # attached to a terminal -> allocate a TTY (interactive TUIs)
        with mock.patch("agtbox.sandboxes.podman.sys.stdin.isatty", return_value=True), \
             mock.patch("agtbox.sandboxes.podman.sys.stdout.isatty", return_value=True):
            argv = Podman().build_run_argv(self._ctx())
        self.assertIn("-t", argv)
        self.assertIn("-i", argv)
        # not a terminal (pipe/CI) -> no -t, so `bash -- -c ...` works
        with mock.patch("agtbox.sandboxes.podman.sys.stdin.isatty", return_value=False), \
             mock.patch("agtbox.sandboxes.podman.sys.stdout.isatty", return_value=True):
            argv = Podman().build_run_argv(self._ctx())
        self.assertNotIn("-t", argv)
        self.assertIn("-i", argv)

    def test_derive_tz_appends_to_ctx_env(self):
        from unittest import mock
        from agtbox.sandboxes.podman import Podman
        ctx = self._ctx()
        with mock.patch("agtbox.sandboxes.podman.os.readlink",
                        return_value="../usr/share/zoneinfo/America/New_York"):
            Podman().derive_tz(ctx)
        self.assertIn(("TZ", "America/New_York"), ctx.env)


# Migrated from test_agtbox.py, retargeted to Podman. Requires these imports at the
# top of test/test_sandboxes.py: `import os, shutil, tempfile, types`,
# `from pathlib import Path`, `from unittest import mock`.
class RefreshCerts(unittest.TestCase):
    def setUp(self):
        from agtbox.sandboxes import podman
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(setattr, podman, "PROJ_DIR", podman.PROJ_DIR)  # never touch the real repo
        podman.PROJ_DIR = str(self.tmp / "proj")
        self.dst = self.tmp / "proj/container/certs"
        self.addCleanup(os.environ.pop, "AGENT_CERTS_DIR", None)

    def test_copies_crt_skips_dotfiles_and_dirs(self):
        from agtbox.sandboxes.podman import Podman
        src = self.tmp / "src"
        (src / "sub").mkdir(parents=True)
        (src / "company.crt").write_text("x")
        (src / ".hidden.crt").write_text("x")
        (src / "sub/nested.crt").write_text("x")
        os.environ["AGENT_CERTS_DIR"] = str(src)
        Podman().refresh_certs()
        self.assertEqual(sorted(p.name for p in self.dst.iterdir()), ["company.crt"])

    def test_missing_source_is_fine(self):
        from agtbox.sandboxes.podman import Podman
        os.environ["AGENT_CERTS_DIR"] = str(self.tmp / "nope")
        Podman().refresh_certs()   # must not raise
        self.assertTrue(self.dst.is_dir())
        self.assertEqual(list(self.dst.iterdir()), [])


class BuildImage(unittest.TestCase):
    def setUp(self):
        from agtbox.sandboxes import podman
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(setattr, podman, "PROJ_DIR", podman.PROJ_DIR)
        podman.PROJ_DIR = str(self.tmp)                 # refresh_certs writes here, not the repo
        self.addCleanup(os.environ.pop, "AGENT_CERTS_DIR", None)
        os.environ["AGENT_CERTS_DIR"] = str(self.tmp / "nocerts")

    def _calls(self, fn, image_present):
        from agtbox.sandboxes import podman
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            rc = 1 if (argv[:3] == ["podman", "image", "exists"] and not image_present) else 0
            return types.SimpleNamespace(returncode=rc)

        with mock.patch.object(podman.subprocess, "run", fake_run):
            fn()
        return [" ".join(c) for c in calls]

    def test_builds_when_image_absent(self):
        from agtbox.sandboxes.podman import Podman
        cmds = self._calls(Podman().build_image, image_present=False)
        self.assertTrue(any(c.startswith("podman build") for c in cmds))

    def test_skips_build_when_present(self):
        from agtbox.sandboxes.podman import Podman
        cmds = self._calls(Podman().build_image, image_present=True)
        self.assertFalse(any(c.startswith("podman build") for c in cmds))

    def test_rebuild_removes_image_first(self):
        # rebuild logic now lives in Podman.rebuild(), not build_image().
        from agtbox.sandboxes.podman import Podman
        cmds = self._calls(Podman().rebuild, image_present=True)
        self.assertTrue(any("image rm" in c for c in cmds))


class IdentityFiles(unittest.TestCase):
    """bwrap.ensure_identity_files synthesizes passwd/group for LDAP-backed users.
    Reads core.HOME/core.AGENT_STATE and the bwrap module's os/grp -- patch those."""

    def _home(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / "home"
        home.mkdir()
        return home

    def test_seeded_from_host_and_append_missing_entries(self):
        from agtbox.sandboxes import bwrap
        home = self._home()
        fake_grp = type("G", (), {})
        groups = {os.getgid(): fake_grp()}
        groups[os.getgid()].gr_name = "primary"
        groups[os.getgid()].gr_gid = os.getgid()
        groups[os.getgid()].gr_mem = []
        extra_gid = os.getgid() + 1
        groups[extra_gid] = fake_grp()
        groups[extra_gid].gr_name = "extra"
        groups[extra_gid].gr_gid = extra_gid
        groups[extra_gid].gr_mem = ["someone"]

        file_data = {"/etc/passwd": "root:x:0:0::/root:/bin/bash\n", "/etc/group": "root:x:0:\n"}
        written = {}

        def fake_open(path, mode="r", *args, **kwargs):
            if "r" in mode:
                return mock_open(read_data=file_data.get(path, ""))()
            handle = mock_open()()
            handle.write.side_effect = lambda data: written.__setitem__(
                path, written.get(path, "") + data) or len(data)
            return handle

        with mock.patch.object(agtbox.core, "HOME", str(home)), \
             mock.patch.object(agtbox.core, "AGENT_STATE", f"{home}/.local/state/agent-box"), \
             mock.patch.object(bwrap.os, "getuid", return_value=26158), \
             mock.patch.object(bwrap.os, "getgid", return_value=os.getgid()), \
             mock.patch.object(bwrap.os, "getgroups", return_value=[os.getgid(), extra_gid]), \
             mock.patch.dict(bwrap.os.environ, {"USER": "dejager", "HOME": str(home), "SHELL": "/bin/bash"}, clear=True), \
             mock.patch.object(bwrap.grp, "getgrgid", side_effect=lambda gid: groups[gid]), \
             mock.patch("builtins.open", side_effect=fake_open):
            bwrap.ensure_identity_files()

        passwd_out = written[f"{home}/.local/state/agent-box/passwd"]
        group_out = written[f"{home}/.local/state/agent-box/group"]
        self.assertIn("root:x:0:0::/root:/bin/bash", passwd_out)
        self.assertIn("dejager:x:26158:", passwd_out)
        self.assertEqual(passwd_out.count("dejager:x:26158:"), 1)
        self.assertIn("primary:x:", group_out)
        self.assertIn("extra:x:", group_out)

    def test_do_not_duplicate_existing_name_entries(self):
        from agtbox.sandboxes import bwrap
        home = self._home()
        group = type("G", (), {})()
        group.gr_name = "primary"
        group.gr_gid = 26158
        group.gr_mem = []

        file_data = {
            "/etc/passwd": "dejager:x:26158:26158::/users/dejager:/bin/bash\n",
            "/etc/group": "primary:x:26158:\n",
        }
        written = {}

        def fake_open(path, mode="r", *args, **kwargs):
            if "r" in mode:
                return mock_open(read_data=file_data.get(path, ""))()
            handle = mock_open()()
            handle.write.side_effect = lambda data: written.__setitem__(
                path, written.get(path, "") + data) or len(data)
            return handle

        with mock.patch.object(agtbox.core, "HOME", str(home)), \
             mock.patch.object(agtbox.core, "AGENT_STATE", f"{home}/.local/state/agent-box"), \
             mock.patch.object(bwrap.os, "getuid", return_value=26158), \
             mock.patch.object(bwrap.os, "getgid", return_value=26158), \
             mock.patch.object(bwrap.os, "getgroups", return_value=[26158]), \
             mock.patch.dict(bwrap.os.environ, {"USER": "dejager", "HOME": str(home), "SHELL": "/bin/bash"}, clear=True), \
             mock.patch.object(bwrap.grp, "getgrgid", return_value=group), \
             mock.patch("builtins.open", side_effect=fake_open):
            bwrap.ensure_identity_files()

        passwd_out = written[f"{home}/.local/state/agent-box/passwd"]
        group_out = written[f"{home}/.local/state/agent-box/group"]
        self.assertEqual(passwd_out.count("dejager:x:26158:26158::/users/dejager:/bin/bash"), 1)
        self.assertEqual(group_out.count("primary:x:26158:"), 1)


if __name__ == "__main__":
    unittest.main()
