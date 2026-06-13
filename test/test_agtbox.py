#!/usr/bin/env python3
"""Test suite for bin/agtbox.py (Agent Box launcher: bwrap + podman engines).

Two layers, stdlib `unittest` only (no third-party deps):

* Integration tests run the launcher as a subprocess with `bwrap` and `podman`
  STUBBED on PATH (each echoes the argv it WOULD exec as `ARG:<word>` lines) and a
  throwaway HOME whose toolchain is pre-seeded so the one-time install is skipped.
  This exercises real flag parsing, engine selection, argv construction, and exit
  codes -- hermetic and offline, no real sandbox/build/network.
* Unit tests import the module and check the pure helpers directly.

Run: python3 -m unittest discover -s test   (or: python3 test/test_agtbox.py)
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGTBOX = REPO / "bin" / "agtbox.py"

# A stub for both engines. podman's `image`/`build`/`run ... uname -m` sub-commands
# are answered so build/install are skipped; every other invocation (the real bwrap
# or `podman run`) echoes its argv so the test can assert the constructed command.
STUB = r"""#!/usr/bin/env bash
case "$1" in
  image) exit 0 ;;
  build) exit 0 ;;
  run)
    case " $* " in
      *" uname -m "*) echo aarch64; exit 0 ;;
    esac ;;
esac
printf 'ARG:%s\n' "$@"
exit 0
"""


class LauncherTest(unittest.TestCase):
    """Base: a throwaway HOME with stubbed engines and a pre-seeded toolchain."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.stub = self.tmp / "stub"
        self.app = self.tmp / "app"
        self.ro = self.tmp / "ro"
        self.rw = self.tmp / "rw"
        for d in (self.home, self.stub, self.app, self.ro, self.rw):
            d.mkdir(parents=True)
        for engine in ("bwrap", "podman"):
            p = self.stub / engine
            p.write_text(STUB)
            p.chmod(0o755)
        # Pre-seed the toolchain so ensure_tools() skips the networked install.
        self.tools = self.home / ".local/share/agent-box"
        (self.tools / "bin").mkdir(parents=True)
        (self.tools / "node/bin").mkdir(parents=True)
        for b in ("claude", "opencode", "codex", "uv"):
            self._exe(self.tools / "bin" / b)
        self._exe(self.tools / "node/bin/node")
        (self.tools / ".stamp").write_text("seeded\n")

    @staticmethod
    def _exe(path):
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)

    def launch(self, *args, set_home=True, drop_home=False):
        """Run agtbox.py; return (returncode, argv_list, stderr). argv_list is the
        words the stubbed engine was exec'd with (the ARG: lines)."""
        env = dict(os.environ)
        env["PATH"] = f"{self.stub}:{env['PATH']}"
        env.pop("AGTBOX_REINSTALL", None)
        if drop_home:
            env.pop("HOME", None)
        elif set_home:
            env["HOME"] = str(self.home)
        proc = subprocess.run(
            [sys.executable, str(AGTBOX), *args],
            capture_output=True, text=True, env=env,
        )
        argv = [ln[4:] for ln in proc.stdout.splitlines() if ln.startswith("ARG:")]
        return proc.returncode, argv, proc.stderr

    # convenience assertions
    def assertArg(self, argv, word):
        self.assertIn(word, argv, f"expected arg {word!r} in {argv}")

    def assertNoArg(self, argv, word):
        self.assertNotIn(word, argv, f"unexpected arg {word!r} in {argv}")


class BwrapArgv(LauncherTest):
    def test_locked_down_sandbox(self):
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app),
                                    "-r", str(self.ro), "claude", "--resume", "X")
        self.assertEqual(rc, 0, err)
        # wiped env + read-only system binds
        self.assertArg(argv, "--clearenv")
        for d in ("/usr", "/etc"):
            self.assertArg(argv, d)
        self.assertArg(argv, "--ro-bind")
        self.assertArg(argv, "--ro-bind-try")
        for d in ("/sbin", "/opt", "/cpe"):
            self.assertArg(argv, d)
        # ephemeral home + min /dev,/proc + isolation primitives
        self.assertArg(argv, "--tmpfs")
        self.assertArg(argv, str(self.home))
        for flag in ("--dev", "--proc", "--die-with-parent",
                     "--unshare-pid", "--unshare-ipc", "--unshare-uts"):
            self.assertArg(argv, flag)
        # persistent toolchain + cache (rw)
        self.assertArg(argv, "--bind")
        self.assertArg(argv, str(self.tools))
        self.assertArg(argv, str(self.home / ".cache/agent-box"))
        # env allowlist via --setenv (key and value are separate args)
        self.assertArg(argv, "--setenv")
        self.assertArg(argv, "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS")
        self.assertArg(argv, "npm_config_prefix")
        # per-tool config binds, onto the real paths
        cfg = self.home / ".config/agent-box"
        for src, dst in [
            (cfg / "claude", self.home / ".claude"),
            (cfg / "claude.json", self.home / ".claude.json"),  # file bind
            (cfg / "ssh", self.home / ".ssh"),
            (cfg / "git", self.home / ".config/git"),
            (cfg / "gh", self.home / ".config/gh"),
            (cfg / "glab", self.home / ".config/glab-cli"),
        ]:
            self.assertArg(argv, str(src))
            self.assertArg(argv, str(dst))
        # opencode's four XDG bases
        for dst in (".local/share/opencode", ".local/state/opencode",
                    ".cache/opencode", ".config/opencode"):
            self.assertArg(argv, str(self.home / dst))
        # project + agent bin + verbatim tool args + terminator
        self.assertArg(argv, str(self.app))
        self.assertArg(argv, str(self.tools / "bin/claude"))
        self.assertArg(argv, "--resume")
        self.assertArg(argv, "X")
        self.assertArg(argv, "--")
        # -r mounted read-only
        self.assertArg(argv, str(self.ro))
        # things that must NOT be there
        self.assertNoArg(argv, "--unshare-net")   # network is shared
        self.assertNoArg(argv, "--symlink")        # direct binds only
        self.assertNoArg(argv, "GIT_CONFIG_GLOBAL")


class PodmanArgv(LauncherTest):
    def test_run_argv(self):
        rc, argv, err = self.launch("-t", "podman", "-a", str(self.app),
                                    "-r", str(self.ro), "opencode", "--session", "Y")
        self.assertEqual(rc, 0, err)
        for word in ("run", "-it", "--rm", "--security-opt", "label=disable"):
            self.assertArg(argv, word)
        self.assertNoArg(argv, "--userns=keep-id")   # run as container-root -> host user
        # env as -e K=V
        self.assertArg(argv, f"HOME={self.home}")
        self.assertArg(argv, "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1")
        # same-path binds (-v src:dst), :ro for -r
        self.assertArg(argv, f"{self.tools}:{self.tools}")
        cfg = self.home / ".config/agent-box"
        self.assertArg(argv, f"{cfg}/claude:{self.home}/.claude")
        self.assertArg(argv, f"{cfg}/claude.json:{self.home}/.claude.json")
        self.assertArg(argv, f"{cfg}/ssh:{self.home}/.ssh")
        self.assertArg(argv, f"{self.app}:{self.app}")
        self.assertArg(argv, f"{self.ro}:{self.ro}:ro")
        # image, agent bin, verbatim args, terminator
        self.assertArg(argv, "agent-box")
        self.assertArg(argv, str(self.tools / "bin/opencode"))
        self.assertArg(argv, "--session")
        self.assertArg(argv, "Y")
        self.assertArg(argv, "--")
        self.assertNoArg(argv, "--network")     # network shared
        self.assertNoArg(argv, "--clearenv")    # bwrap-only flag


class Tools(LauncherTest):
    def test_codex_resume_subcommand(self):
        rc, argv, err = self.launch("-a", str(self.app), "-t", "bwrap",
                                    "codex", "resume", "Z")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, str(self.tools / "bin/codex"))
        self.assertArg(argv, "resume")
        self.assertArg(argv, "Z")

    def test_bare_tool_injects_nothing(self):
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(rc, 0, err)
        for word in ("--continue", "--resume", "--fork-session"):
            self.assertNoArg(argv, word)


class Passthrough(LauncherTest):
    def test_dash_leading_tool_arg_passes_through(self):
        rc, argv, err = self.launch("-t", "podman", "-a", str(self.app),
                                    "codex", "--weird-flag")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, "--weird-flag")

    def test_double_dash_separator_consumed(self):
        # `--` ends launcher options; the tool + its `-`-leading args follow verbatim.
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app),
                                    "--", "claude", "--resume", "X")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, str(self.tools / "bin/claude"))
        self.assertArg(argv, "--resume")
        self.assertArg(argv, "X")


class Volumes(LauncherTest):
    def test_v_and_r_distinct_paths(self):
        rc, argv, err = self.launch("-t", "podman", "-a", str(self.app),
                                    "-v", str(self.rw), "-r", str(self.ro), "claude")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, f"{self.rw}:{self.rw}")
        self.assertArg(argv, f"{self.ro}:{self.ro}:ro")

    def test_same_path_v_and_r_rw_wins_with_warning(self):
        rc, argv, err = self.launch("-t", "podman", "-a", str(self.app),
                                    "-v", str(self.ro), "-r", str(self.ro), "claude")
        self.assertEqual(rc, 0, err)
        self.assertIn("given as both -v (rw) and -r (ro)", err)
        self.assertArg(argv, f"{self.ro}:{self.ro}")        # bound rw
        self.assertNoArg(argv, f"{self.ro}:{self.ro}:ro")   # not also ro


class EngineSelect(LauncherTest):
    def test_default_prefers_bwrap(self):
        # both stubs on PATH -> bwrap chosen; --clearenv is bwrap-only
        rc, argv, err = self.launch("-a", str(self.app), "claude")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, "--clearenv")

    def test_rebuild_flag_warns_under_bwrap(self):
        rc, argv, err = self.launch("-b", "-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(rc, 0, err)
        self.assertIn("-b (rebuild) applies to the podman engine only", err)

    def test_clustered_flags(self):
        # -bt podman == -b -t podman
        rc, argv, err = self.launch("-bt", "podman", "-a", str(self.app),
                                    "opencode", "--session", "Y")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, "agent-box")          # podman engine
        self.assertArg(argv, str(self.tools / "bin/opencode"))


class ErrorCases(LauncherTest):
    def test_no_tool(self):
        rc, _, err = self.launch("-t", "bwrap")
        self.assertEqual(rc, 1)

    def test_unknown_tool(self):
        rc, _, err = self.launch("frobnicate")
        self.assertEqual(rc, 1)

    def test_unknown_flag(self):
        rc, _, err = self.launch("-Z", "claude")
        self.assertEqual(rc, 1)

    def test_bad_engine(self):
        rc, _, err = self.launch("-t", "bogus", "claude")
        self.assertEqual(rc, 1)

    def test_missing_optarg(self):
        rc, _, err = self.launch("-t", "bwrap", "-a")  # -a with no value
        self.assertEqual(rc, 1)
        self.assertIn("requires an argument", err)

    def test_help_exits_one(self):
        rc, _, _ = self.launch("-h")
        self.assertEqual(rc, 1)

    def test_missing_volume_path(self):
        rc, _, err = self.launch("-t", "bwrap", "-a", str(self.app),
                                 "-v", str(self.tmp / "does-not-exist"), "claude")
        self.assertEqual(rc, 1)
        self.assertIn("path does not exist", err)

    def test_home_unset_clean_error(self):
        rc, _, err = self.launch("-t", "bwrap", "claude", drop_home=True)
        self.assertEqual(rc, 1)
        self.assertIn("HOME is not set", err)
        self.assertNotIn("Traceback", err)   # a clean message, not a crash


# ---- unit tests of the pure helpers (import the module directly) -------------

_spec = importlib.util.spec_from_file_location("agtbox", AGTBOX)
agtbox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agtbox)


class Helpers(unittest.TestCase):
    def test_arch_pair(self):
        self.assertEqual(agtbox.arch_pair("aarch64"), ("arm64", "arm64"))
        self.assertEqual(agtbox.arch_pair("x86_64"), ("x64", "amd64"))
        with self.assertRaises(SystemExit):
            agtbox.arch_pair("riscv64")

    def test_split_pair(self):
        self.assertEqual(agtbox._split_pair("/a/b:/c/d"), ("/a/b", "/c/d"))

    def test_kv(self):
        self.assertEqual(agtbox._kv("FOO=bar=baz"), ("FOO", "bar=baz"))

    def test_fmt_env_styles(self):
        pairs = [("HOME", "/h"), ("X", "1")]
        self.assertEqual(agtbox._fmt_env("bwrap", pairs),
                         ["--setenv", "HOME", "/h", "--setenv", "X", "1"])
        self.assertEqual(agtbox._fmt_env("podman", pairs),
                         ["-e", "HOME=/h", "-e", "X=1"])

    def test_bind_args_styles(self):
        self.addCleanup(setattr, agtbox, "VOLUMES", agtbox.VOLUMES)
        self.addCleanup(setattr, agtbox, "RO_VOLUMES", agtbox.RO_VOLUMES)
        agtbox.VOLUMES = ["/vol"]
        agtbox.RO_VOLUMES = ["/rovol"]
        bwrap = agtbox.bind_args("bwrap")
        self.assertIn("--bind", bwrap)
        self.assertIn("/vol", bwrap)
        self.assertIn("--ro-bind", bwrap)
        self.assertIn("/rovol", bwrap)
        podman = agtbox.bind_args("podman")
        self.assertIn("/vol:/vol", podman)
        self.assertIn("/rovol:/rovol:ro", podman)


if __name__ == "__main__":
    unittest.main(verbosity=2)
