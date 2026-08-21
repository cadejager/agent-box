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
import types
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import mock_open

REPO = Path(__file__).resolve().parent.parent
AGTBOX = REPO / "bin" / "agtbox.py"

# A stub for both engines that tags what it was invoked for so a test can tell the
# phases apart: an install (carries AGT_NPM_PKGS) -> INST:, an image build/rm ->
# BUILD:, anything else (the real bwrap or `podman run`) -> ARG:. `podman image
# exists` reports present (skip build); `run ... uname -m` answers the arch probe.
STUB = r"""#!/usr/bin/env bash
prefix=ARG
case " $* " in *AGT_NPM_PKGS*) prefix=INST ;; esac
case "$1" in
  image) exit 0 ;;
  build) printf 'BUILD:%s\n' "$@"; exit 0 ;;
  run)
    case " $* " in
      *" uname -m "*) echo aarch64; exit 0 ;;
    esac ;;
esac
printf "${prefix}:%s\n" "$@"
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
        # Pre-seed the toolchain so ensure_tools() skips the networked install. The
        # toolchain is namespaced by CPU arch (so one shared FS can serve nodes of
        # different arches); the launcher keys it on os.uname().machine -- seed there.
        self.tools = self.home / ".local/share/agent-box" / os.uname().machine
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

    def _run(self, args, set_home=True, drop_home=False, path=None, env_add=None):
        env = dict(os.environ)
        env["PATH"] = path if path is not None else f"{self.stub}:{env['PATH']}"
        env.pop("AGTBOX_REINSTALL", None)
        if drop_home:
            env.pop("HOME", None)
        elif set_home:
            env["HOME"] = str(self.home)
        for k, v in (env_add or {}).items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return subprocess.run([sys.executable, str(AGTBOX), *args],
                              capture_output=True, text=True, env=env, check=False)

    def launch(self, *args, **kw):
        """Run agtbox.py; return (returncode, argv_list, stderr). argv_list is the
        ARG: words the stubbed engine was exec'd with for the agent run."""
        p = self._run(list(args), **kw)
        argv = [ln[4:] for ln in p.stdout.splitlines() if ln.startswith("ARG:")]
        return p.returncode, argv, p.stderr

    def launch_capture(self, *args, **kw):
        """Like launch() but split the stub output by phase: .argv (agent run),
        .inst (toolchain install), .build (image build/rm)."""
        p = self._run(list(args), **kw)

        def pick(pfx):
            return [ln[len(pfx):] for ln in p.stdout.splitlines() if ln.startswith(pfx)]

        return types.SimpleNamespace(rc=p.returncode, argv=pick("ARG:"),
                                     inst=pick("INST:"), build=pick("BUILD:"), err=p.stderr)

    # convenience assertions
    def assertArg(self, argv, word):
        self.assertIn(word, argv, f"expected arg {word!r} in {argv}")

    def assertNoArg(self, argv, word):
        self.assertNotIn(word, argv, f"unexpected arg {word!r} in {argv}")


class BwrapArgv(LauncherTest):
    def test_locked_down_sandbox(self):
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app),
                                    "-r", str(self.ro), "claude", "--", "--resume", "X")
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

    def test_resolv_conf_bound_for_dns(self):
        # /etc/resolv.conf is often a symlink into /run (systemd-resolved, SLES
        # netconfig), which dangles in the sandbox and breaks DNS. The launcher binds
        # the RESOLVED file at its own real path so the symlink in /etc resolves --
        # realpath(/etc/resolv.conf) is the literal file where it's a plain file, or
        # the /run target where it's a symlink.
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, os.path.realpath("/etc/resolv.conf"))

    def test_ca_store_bound_for_tls(self):
        # On SLES/openSUSE the CA bundle lives in /var/lib/ca-certificates and the
        # /etc/ssl symlinks point into it; /var isn't otherwise in the sandbox, so
        # without this bind HTTPS fails "unable to get local issuer certificate".
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, "/var/lib/ca-certificates")

    def test_synthetic_identity_bound(self):
        # To support LDAP-backed users, we generate synthetic passwd/group files
        # in AGENT_STATE and bind them over /etc/passwd and /etc/group.
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(rc, 0, err)
        # must be bound AFTER the general /etc bind to overlay
        self.assertArg(argv, f"{self.home}/.local/state/agent-box/passwd")
        self.assertArg(argv, "/etc/passwd")
        self.assertArg(argv, f"{self.home}/.local/state/agent-box/group")
        self.assertArg(argv, "/etc/group")


class PodmanArgv(LauncherTest):
    def test_run_argv(self):
        rc, argv, err = self.launch("-t", "podman", "-a", str(self.app),
                                    "-r", str(self.ro), "opencode", "--", "--session", "Y")
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
                                    "codex", "--", "resume", "Z")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, str(self.tools / "bin/codex"))
        self.assertArg(argv, "resume")
        self.assertArg(argv, "Z")

    def test_bare_tool_injects_nothing(self):
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(rc, 0, err)
        for word in ("--continue", "--resume", "--fork-session"):
            self.assertNoArg(argv, word)


class Bash(LauncherTest):
    """`bash` is a launchable "tool" that drops an audit shell INTO the sandbox: the
    system /usr/bin/bash, not a toolchain binary (so AGENT_BIN points at it directly)."""

    def test_bwrap_shell(self):
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app),
                                    "-r", str(self.ro), "bash", "--", "-c", "echo hi")
        self.assertEqual(rc, 0, err)
        # the locked-down sandbox is the same as for the agents
        self.assertArg(argv, "--clearenv")
        for d in ("/usr", "/etc"):
            self.assertArg(argv, d)
        self.assertArg(argv, str(self.tools))                 # toolchain still bound
        self.assertArg(argv, str(self.ro))                    # -r still mounted
        # the program is the system shell, NOT ${AGENT_TOOLS}/bin/bash
        self.assertArg(argv, "/usr/bin/bash")
        self.assertNoArg(argv, str(self.tools / "bin/bash"))
        # and -- -c "echo hi" passes through verbatim after the bin
        bin_i = argv.index("/usr/bin/bash")
        self.assertEqual(argv[bin_i + 1:], ["-c", "echo hi"])

    def test_podman_shell(self):
        rc, argv, err = self.launch("-t", "podman", "-a", str(self.app), "bash")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, "agent-box")        # the image
        self.assertArg(argv, "/usr/bin/bash")


class Passthrough(LauncherTest):
    def test_dash_leading_agent_arg_passes_through(self):
        # everything after `--` reaches the agent verbatim, incl. `-`-leading args
        rc, argv, err = self.launch("-t", "podman", "-a", str(self.app),
                                    "codex", "--", "--weird-flag")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, "--weird-flag")

    def test_double_dash_separates_agent_args(self):
        # `agtbox <tool> -- <agent args>`: the args after `--` go to the agent.
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app),
                                    "claude", "--", "--resume", "X")
        self.assertEqual(rc, 0, err)
        bin_i = argv.index(str(self.tools / "bin/claude"))
        # the user's `--` is consumed by the split; only --resume X follow the bin
        # (the `--` present in argv is bwrap's own arg terminator, before the bin)
        self.assertEqual(argv[bin_i + 1:], ["--resume", "X"])


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
        rc, _argv, err = self.launch("-b", "-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(rc, 0, err)
        self.assertIn("-b (rebuild) applies to the podman engine only", err)

    def test_clustered_flags(self):
        # -bt podman == -b -t podman
        rc, argv, err = self.launch("-bt", "podman", "-a", str(self.app),
                                    "opencode", "--", "--session", "Y")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, "agent-box")          # podman engine
        self.assertArg(argv, str(self.tools / "bin/opencode"))


class ErrorCases(LauncherTest):
    # argparse owns flag/tool/engine validation: usage errors -> stderr, exit 2.
    def test_no_tool(self):
        rc, _, _err = self.launch("-t", "bwrap")
        self.assertEqual(rc, 2)

    def test_unknown_tool(self):
        rc, _, _err = self.launch("frobnicate")
        self.assertEqual(rc, 2)

    def test_unknown_flag(self):
        rc, _, _err = self.launch("-Z", "claude")
        self.assertEqual(rc, 2)

    def test_bad_engine(self):
        rc, _, _err = self.launch("-t", "bogus", "claude")
        self.assertEqual(rc, 2)

    def test_missing_optarg(self):
        rc, _, err = self.launch("-t", "bwrap", "-a")  # -a with no value
        self.assertEqual(rc, 2)
        self.assertIn("expected one argument", err)

    def test_help_exits_zero(self):
        # argparse: -h prints to stdout and exits 0 (the standard convention).
        p = self._run(["-h"])
        self.assertEqual(p.returncode, 0)
        self.assertIn("usage:", p.stdout.lower())

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


class InstallTrigger(LauncherTest):
    """ensure_tools(): when does first-run install fire? (toolchain is pre-seeded
    in setUp; remove pieces of it to force the trigger)."""

    def test_missing_stamp_triggers_install(self):
        (self.tools / ".stamp").unlink()
        r = self.launch_capture("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(r.rc, 0, r.err)
        self.assertTrue(r.inst, "a missing .stamp must trigger install")
        self.assertIn("AGT_NPM_PKGS", r.inst)
        self.assertTrue(r.argv, "the agent should still run after install")

    def test_seeded_toolchain_skips_install(self):
        r = self.launch_capture("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(r.rc, 0, r.err)
        self.assertFalse(r.inst, "a complete toolchain must skip install")

    def test_reinstall_env_forces_install(self):
        r = self.launch_capture("-t", "bwrap", "-a", str(self.app), "claude",
                                env_add={"AGTBOX_REINSTALL": "1"})
        self.assertEqual(r.rc, 0, r.err)
        self.assertTrue(r.inst, "AGTBOX_REINSTALL=1 must reinstall despite the stamp")

    def test_missing_bin_retriggers_install(self):
        (self.tools / "bin/claude").unlink()
        r = self.launch_capture("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertTrue(r.inst, "a missing CLI bin must re-trigger install")
        r2 = self.launch_capture("-t", "bwrap", "-a", str(self.app), "codex")
        self.assertFalse(r2.inst, "a present CLI must not trigger install")

    def test_present_nonexecutable_bin_skips_install(self):
        # The macOS/podman case: the toolchain persists but the host sees the CLI as
        # non-executable across the VM mount. Presence is enough -- executability is
        # the sandbox's concern, not the host's -- so this must NOT reinstall.
        os.chmod(self.tools / "bin/claude", 0o644)   # present, but no +x
        r = self.launch_capture("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(r.rc, 0, r.err)
        self.assertFalse(r.inst, "a present CLI must not reinstall just for lacking +x")


class InstallEnv(LauncherTest):
    """The deliberate install-env asymmetry: bwrap install gets the full env
    allowlist (via bwrap_common); podman install gets AGENT_ENV only."""

    def setUp(self):
        super().setUp()
        (self.tools / ".stamp").unlink()   # force the install to run

    def test_podman_install_is_agent_env_only(self):
        r = self.launch_capture("-t", "podman", "-a", str(self.app), "claude")
        self.assertEqual(r.rc, 0, r.err)
        self.assertIn(f"HOME={self.home}", r.inst)            # AGENT_ENV present
        self.assertIn(f"AGT_TOOLS={self.tools}", r.inst)      # AGT_* inputs present
        # ENV_LITERAL / ENV_FORWARD are NOT passed to the podman install:
        self.assertNotIn("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1", r.inst)

    def test_bwrap_install_has_full_allowlist(self):
        r = self.launch_capture("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(r.rc, 0, r.err)
        self.assertIn("AGT_NPM_PKGS", r.inst)                 # AGT_* inputs
        # bwrap install goes through bwrap_common -> the full env allowlist:
        self.assertIn("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", r.inst)


class EnvForward(LauncherTest):
    """ENV_FORWARD is forwarded only for vars actually set on the host."""

    def test_set_var_forwarded_bwrap(self):
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app), "claude",
                                    env_add={"ANTHROPIC_API_KEY": "sek"})
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, "ANTHROPIC_API_KEY")   # --setenv key
        self.assertArg(argv, "sek")

    def test_set_var_forwarded_podman(self):
        rc, argv, err = self.launch("-t", "podman", "-a", str(self.app), "claude",
                                    env_add={"ANTHROPIC_API_KEY": "sek"})
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, "ANTHROPIC_API_KEY=sek")

    def test_unset_var_not_forwarded(self):
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app), "claude",
                                    env_add={"ANTHROPIC_API_KEY": None})  # ensure unset
        self.assertEqual(rc, 0, err)
        self.assertNoArg(argv, "ANTHROPIC_API_KEY")

    def test_opencode_search_off_by_default_on_when_exported(self):
        # opencode's Exa web search is forwarded, not always-on: off unless you set it.
        _, off, _ = self.launch("-t", "bwrap", "-a", str(self.app), "opencode",
                                env_add={"OPENCODE_ENABLE_EXA": None})
        self.assertNoArg(off, "OPENCODE_ENABLE_EXA")
        _, on, _ = self.launch("-t", "bwrap", "-a", str(self.app), "opencode",
                               env_add={"OPENCODE_ENABLE_EXA": "1"})
        self.assertArg(on, "OPENCODE_ENABLE_EXA")

    def test_tz_in_podman_argv_not_bwrap(self):
        # bwrap never sets TZ (it inherits host time via the /etc bind).
        _, bw, _ = self.launch("-t", "bwrap", "-a", str(self.app), "claude")
        self.assertFalse([a for a in bw if a.startswith("TZ=")])

    @unittest.skipUnless(
        os.path.islink("/etc/localtime") and "zoneinfo" in os.readlink("/etc/localtime"),
        "needs /etc/localtime -> .../zoneinfo/<zone>")
    def test_tz_derived_under_podman(self):
        _, pd, _err = self.launch("-t", "podman", "-a", str(self.app), "claude")
        self.assertTrue([a for a in pd if a.startswith("TZ=")], "podman run should carry TZ")


class NoEngine(LauncherTest):
    def test_no_engine_on_path(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        rc, _, err = self.launch("claude", path=str(empty))   # neither bwrap nor podman
        self.assertEqual(rc, 1)
        self.assertIn("no sandbox engine found", err)


class CollidingFlag(LauncherTest):
    def test_agent_arg_colliding_with_launcher_flag_passes_through(self):
        # `-a /x` after `--` goes to the agent verbatim, NOT to the launcher.
        rc, argv, err = self.launch("-t", "bwrap", "-a", str(self.app),
                                    "claude", "--", "-a", "/x")
        self.assertEqual(rc, 0, err)
        self.assertArg(argv, "--chdir")
        self.assertArg(argv, str(self.app))         # project is still the pre-tool -a value
        bin_i = argv.index(str(self.tools / "bin/claude"))
        self.assertEqual(argv[bin_i + 1:], ["-a", "/x"])   # -a /x sits after the agent bin


# ---- unit tests of the pure helpers (import the module directly) -------------

_spec = importlib.util.spec_from_file_location("agtbox", AGTBOX)
agtbox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agtbox)


def load_agtbox(home):
    """Load a FRESH agtbox module with HOME pointed at `home`, so its module-level
    AGENT_*/BIND_* constants resolve under a throwaway tree."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        spec = importlib.util.spec_from_file_location("agtbox_fresh", AGTBOX)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved
    return mod


class Helpers(unittest.TestCase):
    def test_split_pair(self):
        self.assertEqual(agtbox._split_pair("/a/b:/c/d"), ("/a/b", "/c/d"))

    def test_install_script_aux_tools_best_effort(self):
        # uv/gh/glab are auxiliary: on a locked-down network their download hosts may
        # be blocked, so a failure must warn and skip -- not abort the whole install
        # (which would also skip the .stamp and re-download node every run). node + the
        # agent CLIs stay required (no warning wrapper). Pin both, and that .stamp is
        # still written after the best-effort block.
        s = agtbox.install_script()
        for tool in ("uv", "gh", "glab"):
            self.assertIn(f"WARNING -- {tool} install failed", s)
        self.assertNotIn("WARNING -- node", s)
        self.assertNotIn("WARNING -- the agent CLIs", s)
        self.assertLess(s.index("WARNING -- glab"), s.index('date > "${AGT_TOOLS}/.stamp"'))

    def test_toolchain_is_arch_namespaced(self):
        # One shared filesystem across nodes of different arches must not share the
        # toolchain (it's arch-specific native binaries); config/state/cache stay
        # shared (arch-independent), so logins/history/caches follow you across arches.
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        m = load_agtbox(home)
        arch = os.uname().machine
        self.assertEqual(m.AGENT_TOOLS, f"{home}/.local/share/agent-box/{arch}")
        self.assertEqual(m.AGENT_CONFIG, f"{home}/.config/agent-box")
        self.assertEqual(m.AGENT_STATE, f"{home}/.local/state/agent-box")
        self.assertEqual(m.AGENT_CACHE, f"{home}/.cache/agent-box")

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


class ArchPair(unittest.TestCase):
    def test_x86_64_maps_all_arch_names(self):
        self.assertEqual(agtbox.arch_pair("x86_64"), ("x64", "amd64", "x86_64"))

    def test_aarch64_maps_all_arch_names(self):
        self.assertEqual(agtbox.arch_pair("aarch64"), ("arm64", "arm64", "aarch64"))

    def test_unsupported_arch_exits(self):
        with self.assertRaises(SystemExit):
            agtbox.arch_pair("sparc64")


class InstallScript(unittest.TestCase):
    def test_uv_install_uses_github_release_api(self):
        script = agtbox.install_script()
        self.assertIn("https://api.github.com/repos/astral-sh/uv/releases/latest", script)

    def test_uv_install_does_not_use_astral_script(self):
        script = agtbox.install_script()
        self.assertNotIn("https://astral.sh/uv/install.sh", script)

    def test_uv_install_uses_linux_gnu_tarball_pattern(self):
        script = agtbox.install_script()
        self.assertIn("uv-${AGT_UVARCH}-unknown-linux-gnu.tar.gz", script)

    def test_uvx_is_installed_with_uv(self):
        script = agtbox.install_script()
        self.assertIn('install -m755 "/tmp/uv-${AGT_UVARCH}-unknown-linux-gnu/uvx" "${AGT_TOOLS}/bin/uvx"', script)

    def test_uv_warning_stays_best_effort(self):
        script = agtbox.install_script()
        self.assertIn("WARNING -- uv install failed", script)


class WorkflowDocs(unittest.TestCase):
    def test_lint_workflow_sets_up_python_3_11(self):
        workflow = (REPO / ".github/workflows/lint.yml").read_text()
        self.assertIn("actions/setup-python", workflow)
        self.assertIn("python-version: '3.11'", workflow)

    def test_test_suite_file_is_not_marked_executable(self):
        mode = (REPO / "test/test_agtbox.py").stat().st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_test_suite_does_not_have_a_shebang(self):
        first_line = (REPO / "test/test_agtbox.py").read_text().splitlines()[0]
        self.assertNotEqual(first_line, "#!/usr/bin/env python3")


class EnsureSources(unittest.TestCase):
    def _home(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / "home"
        home.mkdir()
        return home

    def test_creates_sources_and_seeds(self):
        home = self._home()
        m = load_agtbox(home)
        m.ensure_sources()
        cfg = home / ".config/agent-box"
        for entry in m.BIND_DIRS:               # every bind source exists
            src = entry.split(":", 1)[0]
            self.assertTrue(os.path.isdir(src), f"missing bind source {src}")
        self.assertEqual((cfg / "claude.json").read_text(), "{}")   # valid-JSON seed
        self.assertTrue((cfg / "git/config").is_file())
        self.assertEqual((cfg / "git/config").read_text(), "")
        self.assertEqual(os.stat(cfg / "ssh").st_mode & 0o777, 0o700)  # ssh needs 0700

    def test_existing_claude_json_left_untouched(self):
        home = self._home()
        cfg = home / ".config/agent-box"
        cfg.mkdir(parents=True)
        (cfg / "claude.json").write_text('{"theme":"dark"}')
        load_agtbox(home).ensure_sources()
        self.assertEqual((cfg / "claude.json").read_text(), '{"theme":"dark"}')

    def test_identity_files_seeded_from_host_and_append_missing_entries(self):
        home = self._home()
        m = load_agtbox(home)
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
        m = load_agtbox(home)
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


class DeriveTz(unittest.TestCase):
    def setUp(self):
        saved = list(agtbox.ENV_LITERAL)   # derive_tz appends to this global; restore it
        self.addCleanup(lambda: (agtbox.ENV_LITERAL.clear(), agtbox.ENV_LITERAL.extend(saved)))

    def test_parses_zone_after_zoneinfo(self):
        with mock.patch.object(agtbox.os, "readlink",
                               return_value="../usr/share/zoneinfo/America/New_York"):
            agtbox.derive_tz()
        self.assertIn("TZ=America/New_York", agtbox.ENV_LITERAL)

    def test_no_zoneinfo_marker_adds_nothing(self):
        with mock.patch.object(agtbox.os, "readlink", return_value="/somewhere/else"):
            agtbox.derive_tz()
        self.assertFalse([x for x in agtbox.ENV_LITERAL if x.startswith("TZ=")])

    def test_readlink_error_adds_nothing(self):
        with mock.patch.object(agtbox.os, "readlink", side_effect=OSError):
            agtbox.derive_tz()
        self.assertFalse([x for x in agtbox.ENV_LITERAL if x.startswith("TZ=")])


class RefreshCerts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(setattr, agtbox, "PROJ_DIR", agtbox.PROJ_DIR)  # never touch the real repo
        agtbox.PROJ_DIR = str(self.tmp / "proj")
        self.dst = self.tmp / "proj/container/certs"
        self.addCleanup(os.environ.pop, "AGENT_CERTS_DIR", None)

    def test_copies_crt_skips_dotfiles_and_dirs(self):
        src = self.tmp / "src"
        (src / "sub").mkdir(parents=True)
        (src / "company.crt").write_text("x")
        (src / ".hidden.crt").write_text("x")
        (src / "sub/nested.crt").write_text("x")
        os.environ["AGENT_CERTS_DIR"] = str(src)
        agtbox.refresh_certs()
        self.assertEqual(sorted(p.name for p in self.dst.iterdir()), ["company.crt"])

    def test_missing_source_is_fine(self):
        os.environ["AGENT_CERTS_DIR"] = str(self.tmp / "nope")
        agtbox.refresh_certs()   # must not raise
        self.assertTrue(self.dst.is_dir())
        self.assertEqual(list(self.dst.iterdir()), [])


class BuildImage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(setattr, agtbox, "PROJ_DIR", agtbox.PROJ_DIR)
        self.addCleanup(setattr, agtbox, "REBUILD", agtbox.REBUILD)
        agtbox.PROJ_DIR = str(self.tmp)                 # refresh_certs writes here, not the repo
        self.addCleanup(os.environ.pop, "AGENT_CERTS_DIR", None)
        os.environ["AGENT_CERTS_DIR"] = str(self.tmp / "nocerts")

    def _build(self, image_present):
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            rc = 0
            if argv[:3] == ["podman", "image", "exists"] and not image_present:
                rc = 1
            return types.SimpleNamespace(returncode=rc)

        with mock.patch.object(agtbox.subprocess, "run", fake_run):
            agtbox.build_image()
        return [" ".join(c) for c in calls]

    def test_builds_when_image_absent(self):
        agtbox.REBUILD = False
        cmds = self._build(image_present=False)
        self.assertTrue(any(c.startswith("podman build") for c in cmds))

    def test_skips_build_when_present(self):
        agtbox.REBUILD = False
        cmds = self._build(image_present=True)
        self.assertFalse(any(c.startswith("podman build") for c in cmds))

    def test_rebuild_removes_image_first(self):
        agtbox.REBUILD = True
        cmds = self._build(image_present=False)
        self.assertTrue(any("image rm" in c for c in cmds))


if __name__ == "__main__":
    unittest.main(verbosity=2)
