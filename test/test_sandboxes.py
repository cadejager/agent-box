import unittest
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
        self.assertTrue(Bwrap().install_full_env)


if __name__ == "__main__":
    unittest.main()
