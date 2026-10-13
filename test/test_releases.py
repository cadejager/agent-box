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

    def test_uv_resolves_from_the_github_api(self):
        uv = next(t for t in releases.TOOLS if t.name == "uv")
        self.assertEqual(uv.api,
                         "https://api.github.com/repos/astral-sh/uv/releases/latest")

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
        # A typo like {uarch} for {uvarch} would otherwise only surface as a 404 on
        # a user's first cold install. @VER@/@VERNOV@ use no braces, so a brace scan
        # is exact.
        for machine in ("x86_64", "aarch64"):
            consts, calls = releases.render(machine)
            self.assertNotIn("{", consts + calls, machine)
            self.assertNotIn("}", consts + calls, machine)

    def test_calls_use_the_right_mode_per_tool(self):
        calls = releases.render("x86_64")[1]
        self.assertIn("install_tree node required", calls)
        for tool in ("uv", "gh", "glab"):
            self.assertIn(f"install_bins {tool} optional", calls)


if __name__ == "__main__":
    unittest.main()
