import importlib.util, os, shutil, tempfile, unittest
from pathlib import Path
from unittest import mock
from unittest.mock import mock_open
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


class IdentityFiles(_HomeCase):
    def test_identity_files_seeded_from_host_and_append_missing_entries(self):
        home = self._home()
        m = fresh_core(home)
        fake_grp = type("G", (), {})
        groups = {
            os.getgid(): fake_grp(),
        }
        groups[os.getgid()].gr_name = "primary"
        groups[os.getgid()].gr_gid = os.getgid()
        groups[os.getgid()].gr_mem = []
        groups[os.getgid()].gr_passwd = "x"
        extra_gid = os.getgid() + 1
        groups[extra_gid] = fake_grp()
        groups[extra_gid].gr_name = "extra"
        groups[extra_gid].gr_gid = extra_gid
        groups[extra_gid].gr_mem = ["someone"]
        groups[extra_gid].gr_passwd = "x"

        file_data = {
            "/etc/passwd": "root:x:0:0::/root:/bin/bash\n",
            "/etc/group": "root:x:0:\n",
        }
        written = {}

        def fake_open(path, mode="r", *args, **kwargs):
            if "r" in mode:
                return mock_open(read_data=file_data.get(path, ""))()
            handle = mock_open()()

            def write(data):
                written[path] = written.get(path, "") + data
                return len(data)

            handle.write.side_effect = write
            return handle

        with mock.patch.object(m, "HOME", str(home)), \
             mock.patch.object(m.os, "getuid", return_value=26158), \
             mock.patch.object(m.os, "getgid", return_value=os.getgid()), \
             mock.patch.object(m.os, "getgroups", return_value=[os.getgid(), extra_gid]), \
             mock.patch.dict(m.os.environ, {"USER": "dejager", "HOME": str(home), "SHELL": "/bin/bash"}, clear=True), \
              mock.patch.object(m, "AGENT_STATE", f"{home}/.local/state/agent-box"), \
              mock.patch.object(m.grp, "getgrgid", side_effect=lambda gid: groups[gid]), \
              mock.patch("builtins.open", side_effect=fake_open):
            m.ensure_identity_files()

        passwd_out = written[f"{home}/.local/state/agent-box/passwd"]
        group_out = written[f"{home}/.local/state/agent-box/group"]
        self.assertIn("root:x:0:0::/root:/bin/bash", passwd_out)
        self.assertIn("dejager:x:26158:", passwd_out)
        self.assertEqual(passwd_out.count("dejager:x:26158:"), 1)
        self.assertIn("primary:x:", group_out)
        self.assertIn("extra:x:", group_out)

    def test_identity_files_do_not_duplicate_existing_name_entries(self):
        home = self._home()
        m = fresh_core(home)
        fake_grp = type("G", (), {})
        group = fake_grp()
        group.gr_name = "primary"
        group.gr_gid = 26158
        group.gr_mem = []
        group.gr_passwd = "x"

        file_data = {
            "/etc/passwd": "dejager:x:26158:26158::/users/dejager:/bin/bash\n",
            "/etc/group": "primary:x:26158:\n",
        }
        written = {}

        def fake_open(path, mode="r", *args, **kwargs):
            if "r" in mode:
                return mock_open(read_data=file_data.get(path, ""))()
            handle = mock_open()()

            def write(data):
                written[path] = written.get(path, "") + data
                return len(data)

            handle.write.side_effect = write
            return handle

        with mock.patch.object(m.os, "getuid", return_value=26158), \
             mock.patch.object(m.os, "getgid", return_value=26158), \
             mock.patch.object(m.os, "getgroups", return_value=[26158]), \
             mock.patch.dict(m.os.environ, {"USER": "dejager", "HOME": str(home), "SHELL": "/bin/bash"}, clear=True), \
              mock.patch.object(m, "AGENT_STATE", f"{home}/.local/state/agent-box"), \
              mock.patch.object(m.grp, "getgrgid", return_value=group), \
              mock.patch("builtins.open", side_effect=fake_open):
            m.ensure_identity_files()

        passwd_out = written[f"{home}/.local/state/agent-box/passwd"]
        group_out = written[f"{home}/.local/state/agent-box/group"]
        self.assertEqual(passwd_out.count("dejager:x:26158:26158::/users/dejager:/bin/bash"), 1)
        self.assertEqual(group_out.count("primary:x:26158:"), 1)


if __name__ == "__main__":
    unittest.main()
