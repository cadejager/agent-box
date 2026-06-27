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


if __name__ == "__main__":
    unittest.main()
