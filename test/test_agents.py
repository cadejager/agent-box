import unittest
from agtbox import core
from agtbox.agents.claude import Claude
from agtbox.agents.opencode import Opencode
from agtbox.agents.codex import Codex
from agtbox.agents.bash import Bash


class AgentContract(unittest.TestCase):
    def test_claude(self):
        a = Claude()
        self.assertEqual(a.name, "claude")
        self.assertEqual(a.bin, f"{core.AGENT_TOOLS}/bin/claude")
        self.assertIn("@anthropic-ai/claude-code", a.packages)
        dsts = [b.dst for b in a.binds]
        self.assertIn(f"{core.HOME}/.claude", dsts)
        self.assertIn(f"{core.HOME}/.claude.json", dsts)
        self.assertTrue(any(b.kind == "file" for b in a.binds))   # claude.json
        self.assertIn("ANTHROPIC_API_KEY", a.env_forward)
        self.assertIn("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1", a.env_literal)

    def test_opencode_four_xdg_dirs(self):
        dsts = [b.dst for b in Opencode().binds]
        for d in (".local/share/opencode", ".local/state/opencode",
                  ".cache/opencode", ".config/opencode"):
            self.assertIn(f"{core.HOME}/{d}", dsts)
        self.assertIn("OPENCODE_EXPERIMENTAL_LSP_TOOL=true", Opencode().env_literal)

    def test_codex(self):
        a = Codex()
        self.assertEqual(a.bin, f"{core.AGENT_TOOLS}/bin/codex")
        self.assertIn("OPENAI_API_KEY", a.env_forward)

    def test_bash_is_degenerate(self):
        a = Bash()
        self.assertEqual(a.bin, "/usr/bin/bash")
        self.assertEqual(a.packages, ())                  # immutable default
        self.assertEqual(a.binds, [])                     # binds property returns a fresh list
        self.assertEqual(a.env_forward + a.env_literal, ())


if __name__ == "__main__":
    unittest.main()
