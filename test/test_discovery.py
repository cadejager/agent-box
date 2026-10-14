import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from agtbox import discovery
from agtbox.agents.base import Agent


class Discovery(unittest.TestCase):
    def test_finds_all_agents_sorted(self):
        agents = discovery.discover_agents()
        self.assertEqual(list(agents), ["bash", "claude", "codex", "opencode"])

    def test_finds_all_sandboxes_sorted(self):
        self.assertEqual(list(discovery.discover_sandboxes()), ["bwrap", "podman"])

    def test_classes_registered_from_their_own_module(self):
        # The __module__ guard means a class is registered only from the module that
        # DEFINES it -- so a future plugin importing another's class to subclass it
        # can't re-register the imported class from the wrong module. Each plugin
        # lives in agtbox.<kind>.<name>, so its class's __module__ must match.
        for name, cls in discovery.discover_agents().items():
            self.assertEqual(cls.__module__, f"agtbox.agents.{name}")
        for name, cls in discovery.discover_sandboxes().items():
            self.assertEqual(cls.__module__, f"agtbox.sandboxes.{name}")


class DiscoveryGuards(unittest.TestCase):
    """Discovery fails loudly on a duplicate or empty plugin name rather than
    silently last-wins (the failure mode the fork-and-drop-a-file model invites)."""

    def _discover_pkg(self, modules):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pkg = tmp / "fakeplugins"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        for mod, src in modules.items():
            (pkg / f"{mod}.py").write_text(src)
        sys.path.insert(0, str(tmp))
        self.addCleanup(lambda: sys.path.remove(str(tmp)))
        for m in [m for m in sys.modules if m == "fakeplugins" or m.startswith("fakeplugins.")]:
            del sys.modules[m]
        import fakeplugins
        return discovery._discover(fakeplugins, Agent)

    def test_duplicate_name_exits(self):
        src = "from agtbox.agents.base import Agent\nclass {c}(Agent):\n    name = 'dup'\n"
        with self.assertRaises(SystemExit):
            self._discover_pkg({"a": src.format(c="A"), "b": src.format(c="B")})

    def test_empty_name_exits(self):
        with self.assertRaises(SystemExit):
            self._discover_pkg({"a": "from agtbox.agents.base import Agent\n"
                                     "class A(Agent):\n    name = ''\n"})


if __name__ == "__main__":
    unittest.main()
