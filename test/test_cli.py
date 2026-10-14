import unittest
from agtbox import cli


class Parser(unittest.TestCase):
    def test_dynamic_choices_and_new_flags(self):
        p = cli.build_parser(["bwrap", "podman"], ["bash", "claude"])
        ns = p.parse_args(["-s", "bwrap", "-w", "/rw", "-r", "/ro", "-u", "claude"])
        self.assertEqual(ns.sandbox, "bwrap")
        self.assertEqual(ns.volumes, ["/rw"])
        self.assertEqual(ns.ro_volumes, ["/ro"])
        self.assertTrue(ns.update)
        self.assertEqual(ns.agent, "claude")

    def test_bad_sandbox_exits_2(self):
        p = cli.build_parser(["bwrap"], ["claude"])
        with self.assertRaises(SystemExit) as cm:
            p.parse_args(["-s", "nope", "claude"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
