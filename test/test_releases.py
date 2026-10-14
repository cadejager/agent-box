import re
import unittest
from agtbox import releases


class Table(unittest.TestCase):
    def test_four_tools_in_execution_order(self):
        self.assertEqual([t.name for t in releases.TOOLS],
                         ["node", "uv", "gh", "glab"])

    def test_node_required_and_others_optional(self):
        by_name = {t.name: t for t in releases.TOOLS}
        self.assertTrue(by_name["node"].required)
        for name in ("uv", "gh", "glab"):
            self.assertFalse(by_name[name].required, name)

    def test_uv_installs_both_uv_and_uvx(self):
        # The astral bootstrap installed uvx implicitly; the tarball path must not
        # silently drop it.
        uv = next(t for t in releases.TOOLS if t.name == "uv")
        self.assertEqual(sorted(b.split(":")[1] for b in uv.bins), ["uv", "uvx"])

    def test_uv_skips_the_github_api(self):
        # uv's asset names are version-free, so releases/latest/download/ needs no
        # api.github.com lookup (a separate allowlist entry, rate-limited 60/h).
        uv = next(t for t in releases.TOOLS if t.name == "uv")
        self.assertEqual(uv.api, "")
        self.assertIn("github.com/astral-sh/uv/releases/latest/download/", uv.url)
        self.assertNotIn("@VER", uv.url + "".join(uv.bins))

    def test_no_tool_uses_an_api_host(self):
        # gh/glab resolve via the releases/latest redirect on the web host, so a
        # locked-down network needs only github.com/gitlab.com (plus nodejs.org).
        for t in releases.TOOLS:
            self.assertNotIn("api.github.com", t.api, t.name)
            self.assertNotIn("/api/v4/", t.api, t.name)
        by_name = {t.name: t for t in releases.TOOLS}
        for name in ("gh", "glab"):
            self.assertTrue(by_name[name].api.endswith("/latest"), name)
            self.assertEqual(by_name[name].tag_field, "", name)

    def test_nothing_references_the_astral_bootstrap_host(self):
        # The whole point of the change: locked-down hosts reach GitHub, not astral.sh.
        blob = repr(releases.TOOLS)
        self.assertNotIn("astral.sh", blob)


class Render(unittest.TestCase):
    def test_substitutes_uv_arch(self):
        self.assertIn("uv-x86_64-unknown-linux-gnu.tar.gz",
                      releases.render("x86_64")[0])
        self.assertIn("uv-aarch64-unknown-linux-gnu.tar.gz",
                      releases.render("aarch64")[0])

    def test_substitutes_go_arch_for_gh(self):
        self.assertIn("gh_@VERNOV@_linux_amd64.tar.gz", releases.render("x86_64")[0])
        self.assertIn("gh_@VERNOV@_linux_arm64.tar.gz", releases.render("aarch64")[0])

    def test_substitutes_node_arch(self):
        self.assertIn("node-@VER@-linux-x64.tar.xz", releases.render("x86_64")[0])
        self.assertIn("node-@VER@-linux-arm64.tar.xz", releases.render("aarch64")[0])

    def test_no_python_placeholders_survive(self):
        # Guards against a typo like {uarch} for {uvarch}, which would otherwise only
        # surface as a 404 on a user's first cold install. Bash's own ${VAR} braces are
        # stripped first: those are MEANT to survive into the script, where bash expands
        # them. Only a leftover Python format placeholder is a defect here.
        for machine in ("x86_64", "aarch64"):
            consts, calls = releases.render(machine)
            blob = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "", consts + calls)
            self.assertNotIn("{", blob, machine)
            self.assertNotIn("}", blob, machine)

    def test_calls_use_the_right_mode_per_tool(self):
        calls = releases.render("x86_64")[1]
        self.assertIn("install_tree node required", calls)
        for tool in ("uv", "gh", "glab"):
            self.assertIn(f"install_bins {tool} optional", calls)

    def test_node_dest_is_a_bash_variable_reference(self):
        # ${AGT_TOOLS} must reach the script intact -- nothing substitutes @AGT_TOOLS@,
        # so a literal would extract node into a directory of that name and leave
        # ${AGT_TOOLS}/node/bin/npm missing.
        node = next(t for t in releases.TOOLS if t.name == "node")
        self.assertEqual(node.dest, "${AGT_TOOLS}/node")
        self.assertIn('"${AGT_TOOLS}/node"', releases.render("x86_64")[1])


if __name__ == "__main__":
    unittest.main()
