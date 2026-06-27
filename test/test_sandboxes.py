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


if __name__ == "__main__":
    unittest.main()
