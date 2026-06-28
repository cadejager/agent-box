import unittest
from agtbox import discovery


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


if __name__ == "__main__":
    unittest.main()
