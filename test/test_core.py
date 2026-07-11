import importlib.util, os, shutil, tempfile, unittest
from pathlib import Path
import agtbox.core as _canon_core


def fresh_core(home):
    """Load a SEPARATE copy of agtbox.core under a throwaway module name with HOME
    pointed at `home`. Must NOT reload the canonical agtbox.core: agents/sandboxes
    import `from agtbox import core` and read its constants live, so reloading the
    real module would leave them pointing at this (soon-deleted) tmp tree. Loading a
    distinct module name leaves the canonical module untouched."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        spec = importlib.util.spec_from_file_location("agtbox_core_fresh", _canon_core.__file__)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


class _HomeCase(unittest.TestCase):
    """Shared helper: a throwaway $HOME tree, cleaned up after the test."""

    def _home(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / "home"
        home.mkdir()
        return home


class Constants(unittest.TestCase):
    def test_arch_namespaced_paths(self):
        home = Path(tempfile.mkdtemp())
        c = fresh_core(home)
        arch = os.uname().machine
        self.assertEqual(c.AGENT_TOOLS, f"{home}/.local/share/agent-box/{arch}")
        self.assertEqual(c.AGENT_CONFIG, f"{home}/.config/agent-box")
        self.assertEqual(c.AGENT_STATE, f"{home}/.local/state/agent-box")
        self.assertEqual(c.AGENT_CACHE, f"{home}/.cache/agent-box")


class BindType(unittest.TestCase):
    def test_defaults_to_dir(self):
        from agtbox.core import Bind
        b = Bind("/src", "/dst")
        self.assertEqual((b.src, b.dst, b.kind), ("/src", "/dst", "dir"))


class Helpers(unittest.TestCase):
    def test_arch_pair(self):
        from agtbox import core
        self.assertEqual(core.arch_pair("aarch64"), ("arm64", "arm64"))
        self.assertEqual(core.arch_pair("x86_64"), ("x64", "amd64"))
        with self.assertRaises(SystemExit):
            core.arch_pair("riscv64")

    def test_kv(self):
        from agtbox import core
        self.assertEqual(core._kv("FOO=bar=baz"), ("FOO", "bar=baz"))


class ResolveEnv(unittest.TestCase):
    def test_generic_plus_agent_forward_only_when_set(self):
        from agtbox import core
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["LANG"] = "en_US.UTF-8"
        pairs = dict(core.resolve_env(["ANTHROPIC_API_KEY"], ["X=1"]))
        self.assertEqual(pairs["LANG"], "en_US.UTF-8")   # generic forward, set
        self.assertNotIn("ANTHROPIC_API_KEY", pairs)      # agent forward, unset
        self.assertEqual(pairs["X"], "1")                 # agent literal
        os.environ["ANTHROPIC_API_KEY"] = "sek"
        pairs = dict(core.resolve_env(["ANTHROPIC_API_KEY"], []))
        self.assertEqual(pairs["ANTHROPIC_API_KEY"], "sek")


class EnsureSources(_HomeCase):
    def test_creates_dirs_seeds_json_and_git_and_chmods_ssh(self):
        home = self._home()
        c = fresh_core(home)
        cfg = home / ".config/agent-box"
        binds = [
            c.Bind(f"{cfg}/claude.json", f"{home}/.claude.json", "file"),
            c.Bind(f"{cfg}/git/config", f"{home}/.config/git/config", "seed"),
            *c.SHARED_BINDS,
        ]
        c.ensure_sources(binds)
        self.assertEqual((cfg / "claude.json").read_text(), "{}")
        self.assertTrue((cfg / "git/config").is_file())
        self.assertEqual((cfg / "ssh").stat().st_mode & 0o777, 0o700)

    def test_normalize_rw_wins_over_ro_with_warning(self):
        c = fresh_core(self._home())
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        app, _, ro = c.normalize_paths(str(d), [str(d)], [str(d)])
        self.assertEqual(ro, [])   # ro dropped because also rw

    def test_normalize_dedups_repeated_volumes(self):
        c = fresh_core(self._home())
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        _, vols, ro = c.normalize_paths(str(d), [str(d), str(d)], [str(d), str(d)])
        self.assertEqual(vols, [os.path.realpath(str(d))])   # repeated -w collapsed to one
        self.assertEqual(ro, [])                             # ...and ro-that-is-also-rw dropped


if __name__ == "__main__":
    unittest.main()
