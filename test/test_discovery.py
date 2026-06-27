import unittest
from agtbox import discovery


class Discovery(unittest.TestCase):
    def test_finds_all_agents_sorted(self):
        agents = discovery.discover_agents()
        self.assertEqual(list(agents), ["bash", "claude", "codex", "opencode"])

    def test_finds_all_sandboxes_sorted(self):
        self.assertEqual(list(discovery.discover_sandboxes()), ["bwrap", "podman"])


if __name__ == "__main__":
    unittest.main()
