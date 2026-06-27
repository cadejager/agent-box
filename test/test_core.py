import importlib.util, os, tempfile, unittest
from pathlib import Path
import agtbox.core as _canon_core


def fresh_core(home):
    """Load a SEPARATE copy of agtbox.core under a throwaway module name with HOME
    pointed at `home`. Must NOT reload the canonical agtbox.core: agents/sandboxes
    import `from agtbox import core` and read its constants live, so reloading the
    real module would leave them pointing at this (soon-deleted) tmp tree. Loading a
    distinct module name leaves the canonical module untouched."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        spec = importlib.util.spec_from_file_location("agtbox_core_fresh", _canon_core.__file__)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


class Constants(unittest.TestCase):
    def test_arch_namespaced_paths(self):
        home = Path(tempfile.mkdtemp())
        c = fresh_core(home)
        arch = os.uname().machine
        self.assertEqual(c.AGENT_TOOLS, f"{home}/.local/share/agent-box/{arch}")
        self.assertEqual(c.AGENT_CONFIG, f"{home}/.config/agent-box")
        self.assertEqual(c.AGENT_STATE, f"{home}/.local/state/agent-box")
        self.assertEqual(c.AGENT_CACHE, f"{home}/.cache/agent-box")


class BindType(unittest.TestCase):
    def test_defaults_to_dir(self):
        from agtbox.core import Bind
        b = Bind("/src", "/dst")
        self.assertEqual((b.src, b.dst, b.kind), ("/src", "/dst", "dir"))


class Helpers(unittest.TestCase):
    def test_arch_pair(self):
        from agtbox import core
        self.assertEqual(core.arch_pair("aarch64"), ("arm64", "arm64"))
        self.assertEqual(core.arch_pair("x86_64"), ("x64", "amd64"))
        with self.assertRaises(SystemExit):
            core.arch_pair("riscv64")

    def test_kv(self):
        from agtbox import core
        self.assertEqual(core._kv("FOO=bar=baz"), ("FOO", "bar=baz"))


class ResolveEnv(unittest.TestCase):
    def test_generic_plus_agent_forward_only_when_set(self):
        from agtbox import core
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["LANG"] = "en_US.UTF-8"
        pairs = dict(core.resolve_env(["ANTHROPIC_API_KEY"], ["X=1"]))
        self.assertEqual(pairs["LANG"], "en_US.UTF-8")   # generic forward, set
        self.assertNotIn("ANTHROPIC_API_KEY", pairs)      # agent forward, unset
        self.assertEqual(pairs["X"], "1")                 # agent literal
        os.environ["ANTHROPIC_API_KEY"] = "sek"
        pairs = dict(core.resolve_env(["ANTHROPIC_API_KEY"], []))
        self.assertEqual(pairs["ANTHROPIC_API_KEY"], "sek")


if __name__ == "__main__":
    unittest.main()
