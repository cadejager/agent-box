import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from agtbox import install


class InstallScript(unittest.TestCase):
    def test_is_syntactically_valid_bash(self):
        # install_script() is assembled from the releases.py table; a stray quote in
        # the template or a row would still break first-run install for every user
        # and no other test would notice.
        # `bash -n` parses without executing (no network/downloads).
        bash = shutil.which("bash") or "/bin/bash"
        r = subprocess.run([bash, "-n", "-c", install.install_script("x86_64")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_aux_tools_best_effort_and_stamp_last(self):
        s = install.install_script("x86_64")
        for tool in ("uv", "gh", "glab"):
            self.assertIn(f"install_bins {tool} optional", s)
        self.assertIn("install_tree node required", s)
        self.assertLess(s.index("install_bins glab"),
                        s.index('date > "${AGT_TOOLS}/.stamp"'))

    def test_core_block_is_gated_and_npm_always_runs(self):
        s = install.install_script("x86_64")
        self.assertIn('if [ "${AGT_DO_CORE}" = "1" ]', s)        # core gated
        self.assertIn("npm", s)
        # npm install references AGT_NPM_PKGS unconditionally (outside the core gate)
        self.assertIn("${AGT_NPM_PKGS}", s)


class AgtEnv(unittest.TestCase):
    def test_carries_packages_and_do_core(self):
        from agtbox.agents.claude import Claude
        pairs = dict(install.agt_env(Claude(), do_core=True))
        self.assertEqual(pairs["AGT_NPM_PKGS"], "@anthropic-ai/claude-code")
        self.assertEqual(pairs["AGT_DO_CORE"], "1")

    def test_arch_vars_no_longer_cross_the_boundary(self):
        # Arch is substituted in Python and baked into the script literals.
        from agtbox.agents.claude import Claude
        pairs = dict(install.agt_env(Claude(), do_core=True))
        self.assertNotIn("AGT_NARCH", pairs)
        self.assertNotIn("AGT_GOARCH", pairs)


class InstallEnvContents(unittest.TestCase):
    """The install env is sandbox-independent: AGENT_ENV routing + the generic
    proxy/locale forwards WHEN set + the AGT_* inputs -- never the agent-specific
    API/config vars (install doesn't run the agent)."""

    def test_routing_and_proxy_forward_but_no_agent_vars(self):
        import os
        from unittest import mock
        from agtbox.agents.claude import Claude
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://p"}):   # no leak into later tests
            pairs = dict(install.install_env(Claude(), True))
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
            pairs = dict(install.install_env(Claude(), True))
        self.assertNotIn("HTTPS_PROXY", pairs)                       # unset -> not forwarded


class ResolveRedirect(unittest.TestCase):
    """resolve() in redirect mode: the tag is the last path segment of where
    releases/latest lands, and a non-version landing page is a failure."""

    def _resolve(self, landing):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "curl").write_text(f"#!/bin/sh\nprintf '%s' '{landing}'\n")
        (tmp / "curl").chmod(0o755)
        env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}",
               "AGT_TOOLS": str(tmp / "tools"), "AGT_NPM_PKGS": "", "AGT_DO_CORE": "0"}
        script = install.install_script("x86_64") + '\nresolve "https://x/releases/latest" "" ""\n'
        return subprocess.run([shutil.which("bash") or "/bin/bash", "-c", script],
                              capture_output=True, text=True, env=env, check=False)

    def test_tag_is_last_segment_of_redirect(self):
        r = self._resolve("https://github.com/cli/cli/releases/tag/v2.102.0")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "v2.102.0"), r.stderr)

    def test_unversioned_tag_is_accepted(self):
        r = self._resolve("https://github.com/astral-sh/uv/releases/tag/0.13.0")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "0.13.0"), r.stderr)

    def test_non_release_landing_fails(self):
        # A repo without releases redirects to .../releases, not .../tag/<v>.
        r = self._resolve("https://github.com/cli/cli/releases")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")


class SkipPathEndToEnd(unittest.TestCase):
    """Run the real generated script with curl/tar stubbed: nodejs.org answers, the
    auxiliary hosts fail. The script must warn, skip all three, still write .stamp,
    and exit 0 -- otherwise a blocked host stops the core toolchain completing."""

    def _stub_dir(self, tmp):
        stub = tmp / "stub"
        stub.mkdir()
        # curl: serve node's index.json, fail (7) for anything else.
        (stub / "curl").write_text(
            '#!/bin/sh\n'
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            '    *nodejs.org*) echo \'[{"version":"v20.0.0","lts":"Iron"}]\'; exit 0 ;;\n'
            '  esac\n'
            'done\n'
            'exit 7\n')
        # tar: accept whatever it is handed, so node's "download" succeeds.
        # Must drain stdin: a consumer that exits early gives the upstream `echo` in
        # the stub curl a SIGPIPE, pipefail fails the pipeline, and the test is racy.
        (stub / "tar").write_text('#!/bin/sh\ncat >/dev/null\nexit 0\n')
        for f in ("curl", "tar"):
            (stub / f).chmod(0o755)
        return stub

    def test_blocked_aux_hosts_warn_and_still_stamp(self):
        r, tools = self._run(self._stub_dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        for tool in ("uv", "gh", "glab"):
            self.assertIn(f"WARNING -- {tool} install failed", r.stderr)
        self.assertNotIn("WARNING -- node", r.stderr)         # node succeeded
        self.assertIn("skipped: uv gh glab", r.stderr)
        self.assertTrue((tools / ".stamp").exists(), r.stderr)

    def test_warning_names_the_first_host_contacted(self):
        # Derived from the URLs, so it cannot drift: each tool blames the first host
        # it contacts. None goes through an API host any more -- never astral.sh.
        r, _ = self._run(self._stub_dir)
        self.assertIn("gh install failed (github.com blocked?)", r.stderr)
        self.assertIn("uv install failed (github.com blocked?)", r.stderr)
        self.assertIn("glab install failed (gitlab.com blocked?)", r.stderr)
        self.assertNotIn("astral.sh", r.stderr)

    def test_progress_lines_are_emitted(self):
        r, _ = self._run(self._stub_dir)
        for tool in ("node", "uv", "gh", "glab"):
            self.assertIn(f"installing {tool}", r.stderr)

    def test_required_failure_is_loud_and_unstamped(self):
        def stubs(tmp):
            stub = tmp / "stub"
            stub.mkdir()
            (stub / "curl").write_text('#!/bin/sh\nexit 7\n')
            (stub / "tar").write_text('#!/bin/sh\ncat >/dev/null\nexit 0\n')
            for f in ("curl", "tar"):
                (stub / f).chmod(0o755)
            return stub
        r, tools = self._run(stubs)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ERROR -- node install failed", r.stderr)
        self.assertFalse((tools / ".stamp").exists())

    def _run(self, make_stubs):
        bash = shutil.which("bash") or "/bin/bash"
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        stub = make_stubs(tmp)
        tools = tmp / "tools"
        (tools / "bin").mkdir(parents=True)
        env = {**os.environ,
               "PATH": f"{stub}:{os.environ['PATH']}",
               "AGT_TOOLS": str(tools),
               "AGT_NPM_PKGS": "",
               "AGT_DO_CORE": "1"}
        r = subprocess.run([bash, "-c", install.install_script("x86_64")],
                           capture_output=True, text=True, env=env, check=False)
        return r, tools


if __name__ == "__main__":
    unittest.main()
