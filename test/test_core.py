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
    def test_arch_names(self):
        from agtbox import core
        self.assertEqual(core.arch_names("aarch64"),
                         {"narch": "arm64", "goarch": "arm64", "uvarch": "aarch64"})
        self.assertEqual(core.arch_names("x86_64"),
                         {"narch": "x64", "goarch": "amd64", "uvarch": "x86_64"})

    def test_arch_names_unsupported_exits(self):
        from agtbox import core
        with self.assertRaises(SystemExit):
            core.arch_names("riscv64")

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

    def test_existing_file_bind_not_clobbered(self):
        # A file bind is seeded '{}' only when ABSENT -- an existing one (e.g. a real
        # claude.json holding the user's login) must be left untouched, or every
        # launch would wipe their session.
        home = self._home()
        c = fresh_core(home)
        cfg = home / ".config/agent-box"
        cfg.mkdir(parents=True)
        (cfg / "claude.json").write_text('{"real":"login-state"}')
        # SHARED_BINDS included so ensure_sources runs its full path (it always gets
        # the shared binds); the point is the existing claude.json survives.
        c.ensure_sources([c.Bind(f"{cfg}/claude.json", f"{home}/.claude.json", "file"),
                          *c.SHARED_BINDS])
        self.assertEqual((cfg / "claude.json").read_text(), '{"real":"login-state"}')

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


class AgentEnv(unittest.TestCase):
    def test_pip_prefix_absent_and_uv_python_dirs_present(self):
        keys = [e.partition("=")[0] for e in _canon_core.AGENT_ENV]
        self.assertNotIn("PIP_PREFIX", keys)   # it overrides venvs
        self.assertIn("PYTHONUSERBASE", keys)
        env = dict(e.partition("=")[::2] for e in _canon_core.AGENT_ENV)
        self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], f"{_canon_core.AGENT_TOOLS}/uv/python")
        self.assertEqual(env["UV_PYTHON_BIN_DIR"], f"{_canon_core.AGENT_TOOLS}/bin")


class SkillBind(_HomeCase):
    def test_skill_bind_shape_and_source_exists(self):
        b = _canon_core.skill_bind("/h/.claude/skills")
        self.assertEqual((b.dst, b.kind), ("/h/.claude/skills/agent-box", "ro"))
        self.assertEqual(b.src, _canon_core.SKILL_SRC)
        self.assertTrue(os.path.isfile(os.path.join(b.src, "SKILL.md")))

    def test_skill_frontmatter_matches_name_and_has_no_bare_colon(self):
        text = Path(_canon_core.SKILL_SRC, "SKILL.md").read_text()
        front = text.split("---")[1]
        self.assertIn(f"name: {_canon_core.SKILL_NAME}\n", front)
        desc = [l for l in front.splitlines() if l.startswith("description:")][0]
        # an unquoted ": " inside a plain YAML scalar is a parse error
        self.assertNotIn(": ", desc[len("description:"):].replace('"', ""))

    def test_ensure_sources_ro_existing_is_left_alone_missing_exits(self):
        home = self._home()
        c = fresh_core(home)
        src = home / "skill"
        src.mkdir()
        c.ensure_sources([c.Bind(str(src), "/x", "ro"), *c.SHARED_BINDS])
        self.assertEqual(list(src.iterdir()), [])          # not seeded or modified
        with self.assertRaises(SystemExit):
            c.ensure_sources([c.Bind(str(home / "nope"), "/y", "ro"), *c.SHARED_BINDS])
        self.assertFalse((home / "nope").exists())          # and not created

    def test_ensure_sources_creates_arch_cache_dir(self):
        home = self._home()
        c = fresh_core(home)
        c.ensure_sources([c.Bind(f"{c.AGENT_CACHE}/{c.ARCH}/opencode", "/x"), *c.SHARED_BINDS])
        self.assertTrue((Path(c.AGENT_CACHE) / c.ARCH / "opencode").is_dir())
