import unittest

from agtbox import core
from agtbox.agents.bash import Bash
from agtbox.agents.claude import Claude
from agtbox.agents.codex import Codex
from agtbox.agents.omos import OMOS
from agtbox.agents.opencode import Opencode


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

    def test_codex(self):
        a = Codex()
        self.assertEqual(a.bin, f"{core.AGENT_TOOLS}/bin/codex")
        self.assertIn("OPENAI_API_KEY", a.env_forward)

    def test_skill_bound_read_only_after_its_parent_bind(self):
        # The skill lives *inside* the agent's other binds, so each one must come
        # after the bind (if any) that supplies its parent directory.
        expected = {Claude: ".claude/skills", Codex: ".agents/skills",
                    Opencode: ".agents/skills", OMOS: ".agents/skills"}
        for cls, skills in expected.items():
            binds = cls().binds
            dst = f"{core.HOME}/{skills}/agent-box"
            skill = [b for b in binds if b.dst == dst]
            self.assertEqual(len(skill), 1, cls.__name__)
            self.assertEqual((skill[0].kind, skill[0].src), ("ro", core.SKILL_SRC))
            idx = binds.index(skill[0])
            for i, b in enumerate(binds):
                if b.kind != "file" and dst.startswith(b.dst + "/"):
                    self.assertLess(i, idx, f"{cls.__name__}: {b.dst} after skill bind")

    def test_opencode_and_omos_cache_is_per_arch(self):
        for cls, name in ((Opencode, "opencode"), (OMOS, "omos")):
            srcs = {b.dst: b.src for b in cls().binds}
            self.assertEqual(srcs[f"{core.HOME}/.cache/opencode"],
                             f"{core.AGENT_CACHE}/{core.ARCH}/{name}")

    def test_bash_has_no_skill(self):
        self.assertFalse(any(b.kind == "ro" for b in Bash().binds))

    def test_bash_is_degenerate(self):
        a = Bash()
        self.assertEqual(a.bin, "/usr/bin/bash")
        self.assertEqual(a.packages, ())                  # immutable default
        self.assertEqual(a.binds, [])                     # binds property returns a fresh list
        self.assertEqual(a.env_forward + a.env_literal, ())

    def test_opencode_v2_package(self):
        a = Opencode()
        self.assertEqual(a.packages, ("@opencode/cli",))
        self.assertEqual(a.obsolete_packages, ("opencode-ai",))

    def test_obsolete_packages_default_empty(self):
        for cls in (Claude, Codex, Bash):
            self.assertEqual(cls().obsolete_packages, ())

    def test_opencode_disables_service(self):
        self.assertEqual(Opencode().setup_args, ("service", "set", "disabled", "true"))
        for cls in (Claude, Codex, Bash):
            self.assertEqual(cls().setup_args, ())

    def test_opencode_standalone_by_default(self):
        self.assertEqual(Opencode().default_args, ("--standalone",))
        for cls in (Claude, Codex, Bash):
            self.assertEqual(cls().default_args, ())


if __name__ == "__main__":
    unittest.main()
