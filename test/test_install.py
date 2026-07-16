import shutil
import subprocess
import unittest
from agtbox import install


class InstallScript(unittest.TestCase):
    def test_is_syntactically_valid_bash(self):
        # install_script() is a ~40-line hand-maintained shell string; a stray quote
        # would break first-run install for every user and no other test would notice.
        # `bash -n` parses without executing (no network/downloads).
        bash = shutil.which("bash") or "/bin/bash"
        r = subprocess.run([bash, "-n", "-c", install.install_script()],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

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


class InstallEnvContents(unittest.TestCase):
    """The install env is sandbox-independent: AGENT_ENV routing + the generic
    proxy/locale forwards WHEN set + the AGT_* inputs -- never the agent-specific
    API/config vars (install doesn't run the agent)."""

    def test_routing_and_proxy_forward_but_no_agent_vars(self):
        import os
        from unittest import mock
        from agtbox.agents.claude import Claude
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://p"}):   # no leak into later tests
            pairs = dict(install.install_env(Claude(), True, "aarch64"))
        self.assertIn("HOME", pairs)                                 # AGENT_ENV routing
        self.assertEqual(pairs["HTTPS_PROXY"], "http://p")           # generic forward, when set
        self.assertEqual(pairs["AGT_NPM_PKGS"], "@anthropic-ai/claude-code")
        self.assertNotIn("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", pairs)  # no agent vars
        self.assertNotIn("ANTHROPIC_API_KEY", pairs)

    def test_unset_proxy_is_omitted(self):
        import os
        from unittest import mock
        from agtbox.agents.claude import Claude
        env = {k: v for k, v in os.environ.items() if k not in ("HTTPS_PROXY", "HTTP_PROXY")}
        with mock.patch.dict(os.environ, env, clear=True):
            pairs = dict(install.install_env(Claude(), True, "aarch64"))
        self.assertNotIn("HTTPS_PROXY", pairs)                       # unset -> not forwarded


if __name__ == "__main__":
    unittest.main()
