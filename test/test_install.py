import unittest
from agtbox import install


class InstallScript(unittest.TestCase):
    def test_aux_tools_best_effort_and_stamp_last(self):
        s = install.install_script()
        for tool in ("uv", "gh", "glab"):
            self.assertIn(f"WARNING -- {tool} install failed", s)
        self.assertNotIn("WARNING -- node", s)
        self.assertLess(s.index("WARNING -- glab"), s.index('date > "${AGT_TOOLS}/.stamp"'))

    def test_core_block_is_gated_and_npm_always_runs(self):
        s = install.install_script()
        self.assertIn('if [ "${AGT_DO_CORE}" = "1" ]', s)        # core gated
        self.assertIn("npm", s)
        # npm install references AGT_NPM_PKGS unconditionally (outside the core gate)
        self.assertIn("${AGT_NPM_PKGS}", s)


class AgtEnv(unittest.TestCase):
    def test_carries_packages_and_do_core(self):
        from agtbox.agents.claude import Claude
        pairs = dict(install.agt_env(Claude(), do_core=True, machine="aarch64"))
        self.assertEqual(pairs["AGT_NPM_PKGS"], "@anthropic-ai/claude-code")
        self.assertEqual(pairs["AGT_DO_CORE"], "1")
        self.assertEqual(pairs["AGT_NARCH"], "arm64")


class InstallEnvAsymmetry(unittest.TestCase):
    """bwrap install gets the full allowlist (incl. proxy/locale forwards + agent
    literal); podman install gets AGENT_ENV only."""

    def _sandbox(self, full):
        return type("S", (), {"install_full_env": full})()

    def test_bwrap_full_includes_forward_and_literal(self):
        import os
        from unittest import mock
        from agtbox.agents.claude import Claude
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://p"}):   # no leak into later tests
            pairs = dict(install.install_env(Claude(), self._sandbox(True), True, "aarch64"))
        self.assertEqual(pairs["HTTPS_PROXY"], "http://p")            # forward, regression guard
        self.assertEqual(pairs["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")  # agent literal
        self.assertEqual(pairs["AGT_NPM_PKGS"], "@anthropic-ai/claude-code")

    def test_podman_agent_env_only(self):
        from agtbox.agents.claude import Claude
        pairs = dict(install.install_env(Claude(), self._sandbox(False), True, "aarch64"))
        self.assertIn("HOME", pairs)                                  # AGENT_ENV present
        self.assertNotIn("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", pairs)  # no literal/forward


if __name__ == "__main__":
    unittest.main()
