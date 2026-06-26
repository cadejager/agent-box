# Plugin Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the single-file `bin/agtbox.py` launcher into a stdlib-only `agtbox/` package whose sandboxes (bwrap, podman) and agents (claude, opencode, codex, bash) are runtime-discovered drop-in plugins, each subclassing an ABC.

**Architecture:** Build the new package bottom-up *in parallel* with the untouched `bin/agtbox.py`, so the existing test suite stays green throughout. New modules get their own unit tests as they're written. A final task atomically flips `bin/agtbox.py` to a thin shim, ports the subprocess integration tests to the renamed flags, and removes the now-obsolete old-internal unit tests. Engine-specific arg shaping (`--bind`/`--setenv` vs `-v`/`-e`) becomes polymorphism on the `Sandbox` subclass instead of a `style=` string branch.

**Tech Stack:** Python 3 standard library only (`argparse`, `importlib`, `pkgutil`, `dataclasses`, `abc`, `subprocess`, `os`, `shutil`, `pathlib`, `grp`). Tests: stdlib `unittest`, run via `python3 -m unittest discover -s test`.

## Global Constraints

- **Stdlib only.** No third-party imports anywhere in `agtbox/` or `test/`.
- **Python 3** (uses f-strings, `dataclasses`, `abc`; matches current code).
- **`bin/agtbox.py` stays the entry point.** Final form: a shim that prepends the repo root to `sys.path` then calls `agtbox.cli.main`. CLI invocation `./bin/agtbox.py …` is unchanged.
- **Renamed flags (verbatim):** `-t`→`-s` (sandbox), `-v`→`-w` (read-write bind). `-r` (read-only), `-a` (project dir), `-b` (rebuild), `-h` unchanged. New `-u`/`--update`. `AGTBOX_REINSTALL=1` env var is **removed**.
- **`-b` is sandbox-independent:** backed by `Sandbox.rebuild()` (default no-op). No "podman only" warning.
- **`-u` always requires an agent positional.** No standalone toolchain-only update.
- **Argv is always a list** passed to `os.execvp`/`subprocess.run` — never a shell string (except the deliberate `bash -c <install-script>` run *inside* the sandbox).
- **Toolchain paths unchanged:** `~/.local/share/agent-box/<arch>` (arch-namespaced), `~/.config/agent-box`, `~/.local/state/agent-box`, `~/.cache/agent-box`.
- **`os.path.lexists` (presence only)** for agent-bin install gating — never `isfile`/`X_OK` (documented macOS/VM-mount regression).
- **Deterministic discovery:** plugins sorted by name so argparse choices / `--help` / error text are stable.

---

## File Structure

New package (all created during this plan):

| File | Responsibility |
|------|----------------|
| `agtbox/__init__.py` | Empty package marker. |
| `agtbox/core.py` | Path constants, `Bind` dataclass, generic env tables (`AGENT_ENV`, generic forward/literal), shared infra binds (git/gh/glab/ssh) + toolchain/cache binds, `arch_pair`, `_kv`, `resolve_env`, `normalize_paths`, `ensure_identity_files`, `ensure_sources`. |
| `agtbox/context.py` | `RunContext` dataclass (binds + mutable env + app_dir + extra_args + agent + volumes). |
| `agtbox/agents/base.py` | `Agent` ABC. |
| `agtbox/agents/{claude,opencode,codex,bash}.py` | Concrete agents. |
| `agtbox/sandboxes/base.py` | `Sandbox` ABC. |
| `agtbox/sandboxes/bwrap.py` | `Bwrap` sandbox. |
| `agtbox/sandboxes/podman.py` | `Podman` sandbox (+ image build, cert refresh, TZ). |
| `agtbox/discovery.py` | `discover_agents()`, `discover_sandboxes()`. |
| `agtbox/install.py` | `install_script()` (two-step), `ensure_tools()`. |
| `agtbox/cli.py` | `main()`: argv split, dynamic argparse, resolve + dispatch. |
| `bin/agtbox.py` | (Modified last) thin shim. |

New test files (mirror the package): `test/test_core.py`, `test/test_agents.py`, `test/test_sandboxes.py`, `test/test_discovery.py`, `test/test_install.py`. The existing `test/test_agtbox.py` keeps the subprocess integration suite (ported to new flags in the final task); its old unit-test classes migrate into the files above.

Source-of-truth line references below point at the current `bin/agtbox.py` (the version with 647 lines reviewed for this plan).

---

## Task 1: Package skeleton + core constants + `Bind`

**Files:**
- Create: `agtbox/__init__.py` (empty), `agtbox/core.py`
- Test: `test/test_core.py`

**Interfaces:**
- Produces: `core.HOME`, `core.ARCH`, `core.AGENT_TOOLS`, `core.AGENT_CONFIG`, `core.AGENT_STATE`, `core.AGENT_CACHE`, `core.IMAGE` (str constants); `core.Bind` dataclass with fields `src: str`, `dst: str`, `kind: str` (`"dir"|"file"|"seed"`) and a default `kind="dir"`.

- [ ] **Step 1: Write the failing test** — `test/test_core.py`:

```python
import importlib, os, tempfile, unittest
from pathlib import Path


def fresh_core(home):
    """Import agtbox.core with HOME pointed at a throwaway tree."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        import agtbox.core as core
        return importlib.reload(core)
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it, verify failure**

Run: `python3 -m unittest test.test_core -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agtbox'`.

- [ ] **Step 3: Create `agtbox/__init__.py`** (empty file) and `agtbox/core.py` with the constants (port lines 24–51 of `bin/agtbox.py`) and the `Bind` dataclass:

```python
"""Core constants, the Bind type, env tables, and host-side source setup."""
import grp
import os
import sys
from dataclasses import dataclass

HOME = os.environ.get("HOME")
if not HOME:
    print("Error: HOME is not set.", file=sys.stderr)
    sys.exit(1)

ARCH = os.uname().machine
AGENT_TOOLS = f"{HOME}/.local/share/agent-box/{ARCH}"
AGENT_CONFIG = f"{HOME}/.config/agent-box"
AGENT_STATE = f"{HOME}/.local/state/agent-box"
AGENT_CACHE = f"{HOME}/.cache/agent-box"
IMAGE = "agent-box"


@dataclass
class Bind:
    """A host->sandbox mount. kind: 'dir'/'file' both emit a bind arg (file is
    seeded '{}' if absent); 'seed' emits NO bind arg -- it is written inside an
    already-bound dir (see core.ensure_sources)."""
    src: str
    dst: str
    kind: str = "dir"
```

- [ ] **Step 4: Run tests, verify pass**

Run: `python3 -m unittest test.test_core -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add agtbox/__init__.py agtbox/core.py test/test_core.py
git commit -m "feat(core): package skeleton, path constants, Bind type"
```

---

## Task 2: Core helpers — `arch_pair`, `_kv`, `resolve_env`

**Files:**
- Modify: `agtbox/core.py`
- Test: `test/test_core.py`

**Interfaces:**
- Produces: `core.arch_pair(machine: str) -> tuple[str, str]`; `core._kv(entry: str) -> tuple[str, str]`; the env tables `core.AGENT_ENV: list[str]` (K=V strings), `core.ENV_FORWARD_GENERIC: list[str]`, `core.ENV_LITERAL_GENERIC: list[str]`; `core.resolve_env(extra_forward, extra_literal) -> list[tuple[str, str]]`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests** — append to `test/test_core.py`:

```python
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
```

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m unittest test.test_core -v`
Expected: FAIL — `AttributeError: module 'agtbox.core' has no attribute 'arch_pair'`.

- [ ] **Step 3: Implement in `agtbox/core.py`.** Port `arch_pair` (lines 361–370) and `_kv` (133–136) verbatim. Add the *generic* env tables (the non-agent-specific subset of lines 83–111) and `resolve_env`:

```python
AGENT_ENV = [
    f"HOME={HOME}",
    f"PATH={AGENT_TOOLS}/bin:{AGENT_TOOLS}/node/bin:/usr/bin:/bin",
    f"npm_config_prefix={AGENT_TOOLS}",
    f"npm_config_cache={AGENT_CACHE}/npm",
    f"PIP_PREFIX={AGENT_TOOLS}",
    f"PYTHONUSERBASE={AGENT_TOOLS}",
    f"PIP_CACHE_DIR={AGENT_CACHE}/pip",
    "PIP_BREAK_SYSTEM_PACKAGES=1",
    f"UV_CACHE_DIR={AGENT_CACHE}/uv",
    f"UV_TOOL_DIR={AGENT_TOOLS}/uv/tools",
    f"UV_TOOL_BIN_DIR={AGENT_TOOLS}/bin",
]
# Generic (non-agent) env. Agent-specific vars (ANTHROPIC_*, OPENAI_*, OPENCODE_*,
# CLAUDE_CODE_*) move into the agent plugins (Task 4).
ENV_FORWARD_GENERIC = [
    "TERM", "COLORTERM", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
]
ENV_LITERAL_GENERIC = []


def arch_pair(machine):
    if machine == "aarch64":
        return "arm64", "arm64"
    if machine == "x86_64":
        return "x64", "amd64"
    print(f"Error: unsupported architecture '{machine}'.", file=sys.stderr)
    sys.exit(1)


def _kv(entry):
    key, _, val = entry.partition("=")
    return key, val


def resolve_env(extra_forward, extra_literal):
    """Resolve the env allowlist to (key, value) pairs: AGENT_ENV + generic and
    agent literals always; generic + agent forwards only when actually set on the
    host (an unset var must not shadow mounted config)."""
    pairs = [_kv(e) for e in AGENT_ENV]
    for name in [*ENV_FORWARD_GENERIC, *extra_forward]:
        if os.environ.get(name):
            pairs.append((name, os.environ[name]))
    pairs += [_kv(e) for e in [*ENV_LITERAL_GENERIC, *extra_literal]]
    return pairs
```

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m unittest test.test_core -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agtbox/core.py test/test_core.py
git commit -m "feat(core): arch_pair, _kv, generic env tables, resolve_env"
```

---

## Task 3: Shared binds + `ensure_sources` + identity files + `normalize_paths`

**Files:**
- Modify: `agtbox/core.py`
- Test: `test/test_core.py`

**Interfaces:**
- Produces: `core.SHARED_BINDS: list[Bind]` (git/gh/glab/ssh config dirs + the git-config seed); `core.ensure_identity_files()`; `core.ensure_sources(binds: list[Bind])`; `core.normalize_paths(app_dir, volumes, ro_volumes) -> tuple[str, list[str], list[str]]`.
- Consumes: `Bind`, the path constants.

- [ ] **Step 1: Write the failing tests** — append to `test/test_core.py`:

```python
import shutil


class EnsureSources(unittest.TestCase):
    def _home(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        h = tmp / "home"
        h.mkdir()
        return h

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
```

(Identity-file behavior is covered by migrating the two existing `EnsureSources` identity tests — see Task 11.)

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m unittest test.test_core -v`
Expected: FAIL — `AttributeError: ... 'SHARED_BINDS'`.

- [ ] **Step 3: Implement in `agtbox/core.py`.** Add `SHARED_BINDS`; port `ensure_identity_files` (lines 230–272) verbatim; rewrite `ensure_sources` (lines 274–297) to take a `list[Bind]`; port `normalize_paths` (207–227) to take/return params instead of globals:

```python
import shutil  # add to imports

SHARED_BINDS = [
    Bind(f"{AGENT_CONFIG}/git", f"{HOME}/.config/git"),
    Bind(f"{AGENT_CONFIG}/gh", f"{HOME}/.config/gh"),
    Bind(f"{AGENT_CONFIG}/glab", f"{HOME}/.config/glab-cli"),
    Bind(f"{AGENT_CONFIG}/ssh", f"{HOME}/.ssh"),
    Bind(f"{AGENT_CONFIG}/git/config", f"{HOME}/.config/git/config", "seed"),
]


def ensure_sources(binds):
    """Create host-side bind sources so a fresh user can launch. `binds` is the
    full merged list (shared + agent). dir/file -> create the dir; file -> seed
    '{}' if absent; seed -> create an empty file inside its (already-created) dir."""
    ensure_identity_files()
    for d in (AGENT_TOOLS, f"{AGENT_CACHE}/npm", f"{AGENT_CACHE}/pip", f"{AGENT_CACHE}/uv"):
        os.makedirs(d, exist_ok=True)
    for b in binds:
        if b.kind == "dir":
            os.makedirs(b.src, exist_ok=True)
        elif b.kind == "file":
            os.makedirs(os.path.dirname(b.src), exist_ok=True)
            if not os.path.exists(b.src):
                with open(b.src, "w") as fh:
                    fh.write("{}")
        elif b.kind == "seed":
            os.makedirs(os.path.dirname(b.src), exist_ok=True)
            if not os.path.exists(b.src):
                open(b.src, "w").close()
    os.chmod(f"{AGENT_CONFIG}/ssh", 0o700)   # ssh refuses group/world-accessible ~/.ssh
```

`ensure_identity_files` is copied unchanged (it already reads `AGENT_STATE`/`HOME` module globals). `normalize_paths(app_dir, volumes, ro_volumes)` returns the realpath'd, existence-checked, dedup'd triple (same logic + messages as lines 207–227, but on parameters; raise `SystemExit` via the existing `sys.exit(1)` on a missing path).

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m unittest test.test_core -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agtbox/core.py test/test_core.py
git commit -m "feat(core): shared binds, Bind-aware ensure_sources, normalize_paths"
```

---

## Task 4: `Agent` ABC + the four agents

**Files:**
- Create: `agtbox/agents/__init__.py` (empty), `agtbox/agents/base.py`, `agtbox/agents/claude.py`, `agtbox/agents/opencode.py`, `agtbox/agents/codex.py`, `agtbox/agents/bash.py`
- Test: `test/test_agents.py`

**Interfaces:**
- Produces: `agents.base.Agent` ABC with `name: str` (class attr), property/attrs `bin -> str` (default `f"{core.AGENT_TOOLS}/bin/{name}"`), `packages -> list[str]` (default `[]`), `binds -> list[core.Bind]` (default `[]`), `env_forward -> list[str]` (default `[]`), `env_literal -> list[str]` (default `[]`). Concrete: `Claude`, `Opencode`, `Codex`, `Bash`.

- [ ] **Step 1: Write the failing tests** — `test/test_agents.py`:

```python
import unittest
from agtbox import core
from agtbox.agents.claude import Claude
from agtbox.agents.opencode import Opencode
from agtbox.agents.codex import Codex
from agtbox.agents.bash import Bash


class AgentContract(unittest.TestCase):
    def test_claude(self):
        a = Claude()
        self.assertEqual(a.name, "claude")
        self.assertEqual(a.bin, f"{core.AGENT_TOOLS}/bin/claude")
        self.assertIn("@anthropic-ai/claude-code", a.packages)
        dsts = [b.dst for b in a.binds]
        self.assertIn(f"{core.HOME}/.claude", dsts)
        self.assertIn(f"{core.HOME}/.claude.json", dsts)
        self.assertTrue(any(b.kind == "file" for b in a.binds))   # claude.json
        self.assertIn("ANTHROPIC_API_KEY", a.env_forward)
        self.assertIn("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1", a.env_literal)

    def test_opencode_four_xdg_dirs(self):
        dsts = [b.dst for b in Opencode().binds]
        for d in (".local/share/opencode", ".local/state/opencode",
                  ".cache/opencode", ".config/opencode"):
            self.assertIn(f"{core.HOME}/{d}", dsts)
        self.assertIn("OPENCODE_EXPERIMENTAL_LSP_TOOL=true", Opencode().env_literal)

    def test_codex(self):
        a = Codex()
        self.assertEqual(a.bin, f"{core.AGENT_TOOLS}/bin/codex")
        self.assertIn("OPENAI_API_KEY", a.env_forward)

    def test_bash_is_degenerate(self):
        a = Bash()
        self.assertEqual(a.bin, "/usr/bin/bash")
        self.assertEqual(a.packages, [])
        self.assertEqual(a.binds, [])
        self.assertEqual(a.env_forward + a.env_literal, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m unittest test.test_agents -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agtbox.agents.claude'`.

- [ ] **Step 3: Implement.** `agtbox/agents/base.py`:

```python
from abc import ABC
from agtbox import core


class Agent(ABC):
    """One AI coding CLI. Subclasses set `name` and override what they need."""
    name = ""
    packages = []
    env_forward = []
    env_literal = []

    @property
    def bin(self):
        return f"{core.AGENT_TOOLS}/bin/{self.name}"

    @property
    def binds(self):
        return []
```

`agtbox/agents/claude.py`:

```python
from agtbox import core
from agtbox.agents.base import Agent


class Claude(Agent):
    name = "claude"
    packages = ["@anthropic-ai/claude-code"]
    env_forward = ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                   "ANTHROPIC_API_KEY", "ANTHROPIC_DEFAULT_SONNET_MODEL"]
    env_literal = ["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"]

    @property
    def binds(self):
        return [
            core.Bind(f"{core.AGENT_CONFIG}/claude", f"{core.HOME}/.claude"),
            core.Bind(f"{core.AGENT_CONFIG}/claude.json", f"{core.HOME}/.claude.json", "file"),
        ]
```

`agtbox/agents/opencode.py` (the four XDG bases, from lines 60–63):

```python
from agtbox import core
from agtbox.agents.base import Agent


class Opencode(Agent):
    name = "opencode"
    packages = ["opencode-ai"]
    env_forward = ["OPENCODE_ENABLE_EXA"]
    env_literal = ["OPENCODE_EXPERIMENTAL_LSP_TOOL=true"]

    @property
    def binds(self):
        return [
            core.Bind(f"{core.AGENT_CONFIG}/opencode", f"{core.HOME}/.config/opencode"),
            core.Bind(f"{core.AGENT_TOOLS}/opencode", f"{core.HOME}/.local/share/opencode"),
            core.Bind(f"{core.AGENT_STATE}/opencode", f"{core.HOME}/.local/state/opencode"),
            core.Bind(f"{core.AGENT_CACHE}/opencode", f"{core.HOME}/.cache/opencode"),
        ]
```

`agtbox/agents/codex.py`:

```python
from agtbox import core
from agtbox.agents.base import Agent


class Codex(Agent):
    name = "codex"
    packages = ["@openai/codex"]
    env_forward = ["OPENAI_BASE_URL", "OPENAI_API_KEY", "CODEX_API_KEY"]

    @property
    def binds(self):
        return [core.Bind(f"{core.AGENT_CONFIG}/codex", f"{core.HOME}/.codex")]
```

`agtbox/agents/bash.py`:

```python
from agtbox.agents.base import Agent


class Bash(Agent):
    """An audit shell inside the sandbox: the system bash, no toolchain binary,
    no config, no packages."""
    name = "bash"

    @property
    def bin(self):
        return "/usr/bin/bash"
```

Create empty `agtbox/agents/__init__.py`.

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m unittest test.test_agents -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add agtbox/agents test/test_agents.py
git commit -m "feat(agents): Agent ABC + claude/opencode/codex/bash plugins"
```

---

## Task 5: `RunContext` + `Sandbox` ABC

**Files:**
- Create: `agtbox/context.py`, `agtbox/sandboxes/__init__.py` (empty), `agtbox/sandboxes/base.py`
- Test: `test/test_sandboxes.py`

**Interfaces:**
- Produces: `context.RunContext` dataclass with fields `agent`, `binds: list[core.Bind]`, `env: list[tuple[str, str]]` (mutable), `app_dir: str`, `volumes: list[str]`, `ro_volumes: list[str]`, `extra_args: list[str]`.
- Produces: `sandboxes.base.Sandbox` ABC: class attrs `name: str`, `priority: int`; `classmethod is_available() -> bool`; abstract `fmt_env(pairs) -> list[str]`, `fmt_bind(src, dst, ro) -> list[str]`, `build_run_argv(ctx) -> list[str]`, `build_install_argv(script, agent, do_core) -> list[str]`; concrete `prepare(ctx)` (default no-op), `rebuild()` (default no-op), `run(ctx)` (calls `os.execvp` on `build_run_argv`), `install(script, agent, do_core)` (calls `subprocess.run` on `build_install_argv`), and `bind_args(ctx) -> list[str]` (shared helper: maps `ctx.binds` skipping `kind=="seed"`, then `volumes` rw, then `ro_volumes` ro, via `fmt_bind`).

- [ ] **Step 1: Write the failing test** — `test/test_sandboxes.py`:

```python
import unittest
from agtbox.context import RunContext
from agtbox.sandboxes.base import Sandbox


class FakeSandbox(Sandbox):
    name = "fake"
    priority = 0
    @classmethod
    def is_available(cls):
        return True
    def fmt_env(self, pairs):
        return [f"E:{k}={v}" for k, v in pairs]
    def fmt_bind(self, src, dst, ro):
        return [f"B:{src}>{dst}{':ro' if ro else ''}"]
    def build_run_argv(self, ctx):
        return ["fake", *self.bind_args(ctx)]
    def build_install_argv(self, script, agent, do_core):
        return ["fake-install"]


class BindArgs(unittest.TestCase):
    def test_seed_binds_skipped_volumes_emitted(self):
        from agtbox.core import Bind
        ctx = RunContext(agent=None,
                         binds=[Bind("/a", "/A"), Bind("/s", "/S", "seed")],
                         env=[], app_dir="/app", volumes=["/rw"],
                         ro_volumes=["/ro"], extra_args=[])
        out = FakeSandbox().bind_args(ctx)
        self.assertIn("B:/a>/A", out)
        self.assertNotIn("B:/s>/S", out)          # seed never bound
        self.assertIn("B:/rw>/rw", out)
        self.assertIn("B:/ro>/ro:ro", out)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m unittest test.test_sandboxes -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agtbox.context'`.

- [ ] **Step 3: Implement.** `agtbox/context.py`:

```python
from dataclasses import dataclass, field


@dataclass
class RunContext:
    agent: object
    binds: list
    env: list                      # list[tuple[str, str]], mutable (prepare may append)
    app_dir: str
    volumes: list = field(default_factory=list)
    ro_volumes: list = field(default_factory=list)
    extra_args: list = field(default_factory=list)
```

`agtbox/sandboxes/base.py`:

```python
import os
import shutil
import subprocess
from abc import ABC, abstractmethod


class Sandbox(ABC):
    name = ""
    priority = 0

    @classmethod
    def is_available(cls):
        return bool(shutil.which(cls.name))

    @abstractmethod
    def fmt_env(self, pairs): ...
    @abstractmethod
    def fmt_bind(self, src, dst, ro): ...
    @abstractmethod
    def build_run_argv(self, ctx): ...
    @abstractmethod
    def build_install_argv(self, script, agent, do_core): ...

    def prepare(self, ctx):
        pass

    def rebuild(self):
        pass

    def bind_args(self, ctx):
        args = []
        for b in ctx.binds:
            if b.kind == "seed":
                continue
            args += self.fmt_bind(b.src, b.dst, False)
        for m in ctx.volumes:
            args += self.fmt_bind(m, m, False)
        for m in ctx.ro_volumes:
            args += self.fmt_bind(m, m, True)
        return args

    def env_args(self, ctx):
        out = []
        for pair in ctx.env:
            out += self.fmt_env([pair])
        return out

    def run(self, ctx):
        argv = self.build_run_argv(ctx)
        os.execvp(argv[0], argv)

    def install(self, script, agent, do_core):
        argv = self.build_install_argv(script, agent, do_core)
        subprocess.run(argv, check=True)
```

Create empty `agtbox/sandboxes/__init__.py`.

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m unittest test.test_sandboxes -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agtbox/context.py agtbox/sandboxes/__init__.py agtbox/sandboxes/base.py test/test_sandboxes.py
git commit -m "feat(sandboxes): RunContext + Sandbox ABC with shared bind/env args"
```

---

## Task 6: `Bwrap` sandbox

**Files:**
- Create: `agtbox/sandboxes/bwrap.py`
- Test: `test/test_sandboxes.py`

**Interfaces:**
- Produces: `sandboxes.bwrap.Bwrap(Sandbox)` — `name="bwrap"`, `priority=20`; `fmt_env` → `["--setenv", k, v]`; `fmt_bind` → `["--ro-bind"|"--bind", src, dst]`; `prepare(ctx)` writes identity files (`core.ensure_identity_files()`); `base_args(ctx)` (the locked-down system binds + tmpfs home + toolchain/cache + env, ported from `bwrap_common`); `build_run_argv(ctx)`; `build_install_argv(script, agent, do_core)`.

- [ ] **Step 1: Write the failing test** — append to `test/test_sandboxes.py`:

```python
class BwrapArgv(unittest.TestCase):
    def _ctx(self):
        from agtbox.context import RunContext
        from agtbox.core import Bind
        from agtbox.agents.claude import Claude
        return RunContext(agent=Claude(),
                          binds=[Bind("/cfg/claude", "/h/.claude")],
                          env=[("HOME", "/h"), ("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "1")],
                          app_dir="/app", volumes=[], ro_volumes=["/ro"],
                          extra_args=["--resume"])

    def test_run_argv_locked_down(self):
        from agtbox.sandboxes.bwrap import Bwrap
        argv = Bwrap().build_run_argv(self._ctx())
        for w in ("bwrap", "--clearenv", "/usr", "/etc", "--dev", "--proc",
                  "--die-with-parent", "--unshare-pid"):
            self.assertIn(w, argv)
        self.assertNotIn("--unshare-net", argv)       # network shared
        self.assertIn("/app", argv)
        self.assertIn("--chdir", argv)
        self.assertIn("/usr/bin/bash" if False else "--resume", argv)
        self.assertEqual(argv[-1], "--resume")        # extra args last
        self.assertIn("--setenv", argv)
```

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m unittest test.test_sandboxes.BwrapArgv -v`
Expected: FAIL — `ModuleNotFoundError: ... bwrap`.

- [ ] **Step 3: Implement `agtbox/sandboxes/bwrap.py`.** Port `bwrap_common` (lines 373–413) into `base_args(ctx)` — identical body, but: drop the `env_args("bwrap")` call and instead append `self.env_args(ctx)`; the passwd/group binds stay. Port `run_bwrap` (481–489) into `build_run_argv`, and `install_via_bwrap` (416–424) into `build_install_argv`:

```python
import os
from agtbox import core
from agtbox.sandboxes.base import Sandbox


class Bwrap(Sandbox):
    name = "bwrap"
    priority = 20            # preferred over podman when available

    def fmt_env(self, pairs):
        out = []
        for k, v in pairs:
            out += ["--setenv", k, v]
        return out

    def fmt_bind(self, src, dst, ro):
        return ["--ro-bind" if ro else "--bind", src, dst]

    def prepare(self, ctx):
        core.ensure_identity_files()

    def base_args(self, ctx):
        resolv = os.path.realpath("/etc/resolv.conf")
        bw = [
            "--clearenv",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
            "--ro-bind-try", resolv, resolv,
            "--ro-bind", f"{core.AGENT_STATE}/passwd", "/etc/passwd",
            "--ro-bind", f"{core.AGENT_STATE}/group", "/etc/group",
            "--ro-bind-try", "/var/lib/ca-certificates", "/var/lib/ca-certificates",
            "--ro-bind-try", "/bin", "/bin", "--ro-bind-try", "/sbin", "/sbin",
            "--ro-bind-try", "/lib", "/lib", "--ro-bind-try", "/lib64", "/lib64",
            "--ro-bind-try", "/opt", "/opt", "--ro-bind-try", "/cpe", "/cpe",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--tmpfs", core.HOME,
            "--bind", core.AGENT_TOOLS, core.AGENT_TOOLS,
            "--bind", core.AGENT_CACHE, core.AGENT_CACHE,
            "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        ]
        bw += self.env_args(ctx)
        return bw

    def build_run_argv(self, ctx):
        bw = self.base_args(ctx)
        bw += ["--bind", ctx.app_dir, ctx.app_dir, "--chdir", ctx.app_dir]
        bw += self.bind_args(ctx)
        return ["bwrap", *bw, "--", ctx.agent.bin, *ctx.extra_args]

    def build_install_argv(self, script, agent, do_core):
        from agtbox.install import agt_env
        bw = self.base_args_for_install()
        bw += self.fmt_env(agt_env(agent, do_core))
        return ["bwrap", *bw, "--", "/usr/bin/bash", "-c", script]

    def base_args_for_install(self):
        # The bwrap install runs with the SAME locked-down base as a real run but
        # over a minimal context (no project/config binds). Reuse base_args with an
        # install-only ctx whose env is AGENT_ENV + the launched agent's literals.
        from agtbox.context import RunContext
        return self.base_args(RunContext(agent=None, binds=[], env=self._install_env,
                                         app_dir=core.HOME))
```

Note: `build_install_argv`/`base_args_for_install` need the install env (AGENT_ENV + the agent's `env_literal`, per the preserved asymmetry). Resolve that in `install.py` (Task 9) and pass it down; here, set `self._install_env` from a parameter. To keep this task self-contained and tested, implement `build_install_argv(self, script, agent, do_core)` to build the install env inline:

```python
    def build_install_argv(self, script, agent, do_core):
        from agtbox.install import agt_env
        pairs = [core._kv(e) for e in core.AGENT_ENV]
        pairs += [core._kv(e) for e in agent.env_literal]      # bwrap: full-ish allowlist
        pairs += agt_env(agent, do_core)
        from agtbox.context import RunContext
        bw = self.base_args(RunContext(agent=None, binds=[], env=pairs, app_dir=core.HOME))
        return ["bwrap", *bw, "--", "/usr/bin/bash", "-c", script]
```

(Delete the `base_args_for_install`/`_install_env` scaffolding above; the inline version is the implementation. `agt_env` is defined in Task 9 — for this task, temporarily define a local `agt_env` stub returning `[("AGT_NPM_PKGS", " ".join(agent.packages))]` and replace the import in Task 9.)

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m unittest test.test_sandboxes.BwrapArgv -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agtbox/sandboxes/bwrap.py test/test_sandboxes.py
git commit -m "feat(sandboxes): Bwrap sandbox (run + install argv)"
```

---

## Task 7: `Podman` sandbox (+ image build, cert refresh, TZ→ctx.env)

**Files:**
- Create: `agtbox/sandboxes/podman.py`
- Test: `test/test_sandboxes.py`

**Interfaces:**
- Produces: `sandboxes.podman.Podman(Sandbox)` — `name="podman"`, `priority=10`; `fmt_env` → `["-e", f"{k}={v}"]`; `fmt_bind` → `["-v", f"{src}:{dst}[:ro]"]`; `PROJ_DIR` (module-level, recomputed from `__file__`); `refresh_certs()`, `build_image()`, `rebuild()`; `prepare(ctx)` → `build_image()` then `derive_tz(ctx)` (appends `("TZ", zone)` to `ctx.env`); `build_run_argv`, `build_install_argv`.

- [ ] **Step 1: Write the failing tests** — append to `test/test_sandboxes.py` (migrate `RefreshCerts` + `BuildImage` from `test_agtbox.py`, retargeted to `Podman`, plus a TZ test):

```python
class PodmanArgv(unittest.TestCase):
    def _ctx(self):
        from agtbox.context import RunContext
        from agtbox.core import Bind
        from agtbox.agents.opencode import Opencode
        return RunContext(agent=Opencode(), binds=[Bind("/cfg/oc", "/h/.config/opencode")],
                          env=[("HOME", "/h")], app_dir="/app", volumes=[],
                          ro_volumes=["/ro"], extra_args=["--session", "Y"])

    def test_run_argv(self):
        from agtbox.sandboxes.podman import Podman
        argv = Podman().build_run_argv(self._ctx())
        for w in ("podman", "run", "-it", "--rm", "--security-opt", "label=disable"):
            self.assertIn(w, argv)
        self.assertIn("-e", argv)
        self.assertIn("HOME=/h", argv)
        self.assertIn("/cfg/oc:/h/.config/opencode", argv)
        self.assertIn("/ro:/ro:ro", argv)
        self.assertIn("agent-box", argv)
        self.assertEqual(argv[-2:], ["--session", "Y"])
        self.assertNotIn("--clearenv", argv)

    def test_derive_tz_appends_to_ctx_env(self):
        from unittest import mock
        from agtbox.sandboxes.podman import Podman
        ctx = self._ctx()
        with mock.patch("agtbox.sandboxes.podman.os.readlink",
                        return_value="../usr/share/zoneinfo/America/New_York"):
            Podman().derive_tz(ctx)
        self.assertIn(("TZ", "America/New_York"), ctx.env)
```

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m unittest test.test_sandboxes.PodmanArgv -v`
Expected: FAIL — `ModuleNotFoundError: ... podman`.

- [ ] **Step 3: Implement `agtbox/sandboxes/podman.py`.** Port `run_podman` (557–577), `build_image` (521–536), `refresh_certs` (492–518), `derive_tz` (539–554), `install_via_podman` (427–452). Key changes: `PROJ_DIR` recomputed from this file; `derive_tz` appends to `ctx.env` instead of the global `ENV_LITERAL`; `rebuild()` does the `podman image rm` (the `REBUILD` branch of `build_image`), and `build_image()` no longer reads a global `REBUILD`:

```python
import os
import shutil
import subprocess
from pathlib import Path
from agtbox import core
from agtbox.sandboxes.base import Sandbox

PROJ_DIR = str(Path(__file__).resolve().parents[2])   # .../agtbox/sandboxes/podman.py -> repo root


class Podman(Sandbox):
    name = "podman"
    priority = 10

    def fmt_env(self, pairs):
        out = []
        for k, v in pairs:
            out += ["-e", f"{k}={v}"]
        return out

    def fmt_bind(self, src, dst, ro):
        return ["-v", f"{src}:{dst}:ro" if ro else f"{src}:{dst}"]

    def prepare(self, ctx):
        self.build_image()
        self.derive_tz(ctx)

    def derive_tz(self, ctx):
        try:
            link = os.readlink("/etc/localtime")
        except OSError:
            return
        idx = link.rfind("/zoneinfo/")
        if idx != -1 and link[idx + len("/zoneinfo/"):]:
            ctx.env.append(("TZ", link[idx + len("/zoneinfo/"):]))

    def rebuild(self):
        subprocess.run(["podman", "image", "rm", core.IMAGE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def build_image(self):
        if subprocess.run(["podman", "image", "exists", core.IMAGE]).returncode == 0:
            return
        print(f"Agent Box: building the {core.IMAGE} image (one-time)...", file=__import__("sys").stderr)
        self.refresh_certs()
        subprocess.run(["podman", "build", "-t", core.IMAGE, "-f",
                        f"{PROJ_DIR}/container/Containerfile", f"{PROJ_DIR}/container"], check=True)

    def refresh_certs(self):
        ...  # body copied verbatim from current refresh_certs(), with PROJ_DIR = module PROJ_DIR

    def build_run_argv(self, ctx):
        pd = ["run", "-it", "--rm", "--security-opt", "label=disable"]
        pd += self.env_args(ctx)
        pd += ["-v", f"{core.AGENT_TOOLS}:{core.AGENT_TOOLS}",
               "-v", f"{core.AGENT_CACHE}:{core.AGENT_CACHE}"]
        pd += ["-v", f"{ctx.app_dir}:{ctx.app_dir}", "-w", ctx.app_dir]
        pd += self.bind_args(ctx)
        return ["podman", *pd, "--", core.IMAGE, ctx.agent.bin, *ctx.extra_args]

    def build_install_argv(self, script, agent, do_core):
        from agtbox.install import agt_env
        arch = subprocess.run(["podman", "run", "--rm", core.IMAGE, "uname", "-m"],
                              check=True, capture_output=True, text=True).stdout.strip()
        pairs = [core._kv(e) for e in core.AGENT_ENV]          # podman install: AGENT_ENV ONLY
        pairs += agt_env(agent, do_core, machine=arch)
        pd = ["run", "--rm", "--security-opt", "label=disable",
              "-v", f"{core.AGENT_TOOLS}:{core.AGENT_TOOLS}",
              "-v", f"{core.AGENT_CACHE}:{core.AGENT_CACHE}"]
        pd += self.fmt_env(pairs)
        return ["podman", *pd, "--", core.IMAGE, "/usr/bin/bash", "-c", script]
```

(Fill `refresh_certs` with the verbatim body of lines 499–518, using the module `PROJ_DIR`. `agt_env` signature finalized in Task 9 — for now define a local stub matching `agt_env(agent, do_core, machine=None)`.)

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m unittest test.test_sandboxes -v`
Expected: PASS (all sandbox tests).

- [ ] **Step 5: Commit**

```bash
git add agtbox/sandboxes/podman.py test/test_sandboxes.py
git commit -m "feat(sandboxes): Podman sandbox (run/install argv, image, certs, TZ via ctx.env)"
```

---

## Task 8: Discovery

**Files:**
- Create: `agtbox/discovery.py`
- Test: `test/test_discovery.py`

**Interfaces:**
- Produces: `discovery.discover_agents() -> dict[str, type]` and `discovery.discover_sandboxes() -> dict[str, type]`, each mapping `name -> class`, ordered by sorted name. Imports every module under `agtbox/agents/` and `agtbox/sandboxes/` (except `base`/`__init__`) and collects `Agent`/`Sandbox` subclasses.

- [ ] **Step 1: Write the failing test** — `test/test_discovery.py`:

```python
import unittest
from agtbox import discovery


class Discovery(unittest.TestCase):
    def test_finds_all_agents_sorted(self):
        agents = discovery.discover_agents()
        self.assertEqual(list(agents), ["bash", "claude", "codex", "opencode"])

    def test_finds_all_sandboxes_sorted(self):
        self.assertEqual(list(discovery.discover_sandboxes()), ["bwrap", "podman"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m unittest test.test_discovery -v`
Expected: FAIL — `ModuleNotFoundError: ... discovery`.

- [ ] **Step 3: Implement `agtbox/discovery.py`:**

```python
"""Runtime discovery of the in-repo sandbox/agent plugins."""
import importlib
import pkgutil
from agtbox import agents as agents_pkg
from agtbox import sandboxes as sandboxes_pkg
from agtbox.agents.base import Agent
from agtbox.sandboxes.base import Sandbox


def _discover(package, base):
    found = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name == "base":
            continue
        mod = importlib.import_module(f"{package.__name__}.{info.name}")
        for obj in vars(mod).values():
            if isinstance(obj, type) and issubclass(obj, base) and obj is not base:
                found[obj.name] = obj
    return dict(sorted(found.items()))


def discover_agents():
    return _discover(agents_pkg, Agent)


def discover_sandboxes():
    return _discover(sandboxes_pkg, Sandbox)
```

A module that fails to import raises here (loud fail) — intended.

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m unittest test.test_discovery -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agtbox/discovery.py test/test_discovery.py
git commit -m "feat(discovery): sorted runtime discovery of agent/sandbox plugins"
```

---

## Task 9: Install orchestration (two-step script + `ensure_tools`)

**Files:**
- Create: `agtbox/install.py`
- Test: `test/test_install.py`

**Interfaces:**
- Produces: `install.install_script() -> str` (two-step: a `${AGT_DO_CORE}`-gated core block + an always-run npm block, `.stamp` written last); `install.agt_env(agent, do_core, machine=None) -> list[tuple[str,str]]` (the `AGT_*` inputs: `AGT_TOOLS`, `AGT_NARCH`, `AGT_GOARCH`, `AGT_NPM_PKGS=" ".join(agent.packages)`, `AGT_DO_CORE`); `install.ensure_tools(agent, sandbox, force) -> None`.
- Consumes: `core.arch_pair`, `core.AGENT_TOOLS`; `Sandbox.install`.

- [ ] **Step 1: Write the failing tests** — `test/test_install.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m unittest test.test_install -v`
Expected: FAIL — `ModuleNotFoundError: ... install`.

- [ ] **Step 3: Implement `agtbox/install.py`.** Port `install_script` (300–358) but wrap the node/uv/gh/glab block in `if [ "${AGT_DO_CORE}" = "1" ]; then … fi`, keep the npm-install line *outside* that gate, and keep `date > "${AGT_TOOLS}/.stamp"` last. Port `_agt_env` (178–185), adding `AGT_DO_CORE`. Port `ensure_tools` (463–478) to the new signature/gating:

```python
import os
import sys
from agtbox import core


def install_script():
    return r'''set -euo pipefail
mkdir -p "${AGT_TOOLS}/node" "${AGT_TOOLS}/bin"

if [ "${AGT_DO_CORE}" = "1" ]; then
  echo 'Agent Box: installing node (latest LTS)...' >&2
  nv=$(curl -fsSL https://nodejs.org/dist/index.json \
    | python3 -c 'import json,sys; print(next(r["version"] for r in json.load(sys.stdin) if r["lts"]))')
  curl -fsSL "https://nodejs.org/dist/${nv}/node-${nv}-linux-${AGT_NARCH}.tar.xz" \
    | tar -xJ --strip-components=1 -C "${AGT_TOOLS}/node"

  skipped=
  echo 'Agent Box: installing uv...' >&2
  { curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR="${AGT_TOOLS}/bin" UV_NO_MODIFY_PATH=1 sh; } \
    || { echo 'Agent Box: WARNING -- uv install failed (astral.sh blocked?); skipping.' >&2; skipped="${skipped} uv"; }
  echo 'Agent Box: installing gh (GitHub CLI)...' >&2
  { gv=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest \
         | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])') \
    && curl -fsSL "https://github.com/cli/cli/releases/download/${gv}/gh_${gv#v}_linux_${AGT_GOARCH}.tar.gz" \
         | tar -xz -C /tmp \
    && install -m755 "/tmp/gh_${gv#v}_linux_${AGT_GOARCH}/bin/gh" "${AGT_TOOLS}/bin/gh"; } \
    || { echo 'Agent Box: WARNING -- gh install failed (github.com blocked?); skipping.' >&2; skipped="${skipped} gh"; }
  echo 'Agent Box: installing glab (GitLab CLI)...' >&2
  { lv=$(curl -fsSL https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases \
         | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["tag_name"])') \
    && curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/${lv}/downloads/glab_${lv#v}_linux_${AGT_GOARCH}.tar.gz" \
         | tar -xz -C /tmp \
    && install -m755 /tmp/bin/glab "${AGT_TOOLS}/bin/glab"; } \
    || { echo 'Agent Box: WARNING -- glab install failed (gitlab.com blocked?); skipping.' >&2; skipped="${skipped} glab"; }
  if [ -n "${skipped}" ]; then
    echo "Agent Box: core toolchain ready; skipped:${skipped} (retry later with -u)." >&2
  fi
fi

if [ -n "${AGT_NPM_PKGS}" ]; then
  echo 'Agent Box: installing the agent CLI...' >&2
  "${AGT_TOOLS}/node/bin/npm" install -g --prefix "${AGT_TOOLS}" --no-fund --no-audit ${AGT_NPM_PKGS}
fi

date > "${AGT_TOOLS}/.stamp"
'''


def agt_env(agent, do_core, machine=None):
    narch, goarch = core.arch_pair(machine or os.uname().machine)
    return [
        ("AGT_TOOLS", core.AGENT_TOOLS),
        ("AGT_NARCH", narch),
        ("AGT_GOARCH", goarch),
        ("AGT_NPM_PKGS", " ".join(agent.packages)),
        ("AGT_DO_CORE", "1" if do_core else "0"),
    ]


def ensure_tools(agent, sandbox, force):
    """Install on first use (or -u). Core toolchain gated by .stamp; the launched
    agent's packages gated by its bin presence (lexists -- presence only)."""
    stamp = os.path.exists(f"{core.AGENT_TOOLS}/.stamp")
    bin_present = os.path.lexists(agent.bin)
    if not (force or not stamp or not bin_present):
        return
    print(f"Agent Box: setting up the toolchain in {core.AGENT_TOOLS} (one-time)...", file=sys.stderr)
    do_core = force or not stamp
    sandbox.install(install_script(), agent, do_core)
```

Now replace the temporary `agt_env` stubs in `bwrap.py` and `podman.py` with `from agtbox.install import agt_env` (already imported there).

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m unittest test.test_install test.test_sandboxes -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agtbox/install.py agtbox/sandboxes/bwrap.py agtbox/sandboxes/podman.py test/test_install.py
git commit -m "feat(install): two-step install script + per-agent ensure_tools"
```

---

## Task 10: `cli.main` — dynamic argparse, new flags, dispatch

**Files:**
- Create: `agtbox/cli.py`
- Test: covered end-to-end by Task 11 (the ported subprocess suite). Add one direct unit test of resolution here.
- Test: `test/test_cli.py`

**Interfaces:**
- Produces: `cli.main(argv=None)`; `cli.resolve_sandbox(name, sandboxes) -> Sandbox instance` (errors per spec); `cli.build_parser(sandbox_names, agent_names) -> argparse.ArgumentParser`.

- [ ] **Step 1: Write the failing test** — `test/test_cli.py`:

```python
import unittest
from agtbox import cli


class Parser(unittest.TestCase):
    def test_dynamic_choices_and_new_flags(self):
        p = cli.build_parser(["bwrap", "podman"], ["bash", "claude"])
        ns = p.parse_args(["-s", "bwrap", "-w", "/rw", "-r", "/ro", "-u", "claude"])
        self.assertEqual(ns.sandbox, "bwrap")
        self.assertEqual(ns.volumes, ["/rw"])
        self.assertEqual(ns.ro_volumes, ["/ro"])
        self.assertTrue(ns.update)
        self.assertEqual(ns.agent, "claude")

    def test_bad_sandbox_exits_2(self):
        p = cli.build_parser(["bwrap"], ["claude"])
        with self.assertRaises(SystemExit) as cm:
            p.parse_args(["-s", "nope", "claude"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m unittest test.test_cli -v`
Expected: FAIL — `ModuleNotFoundError: ... cli`.

- [ ] **Step 3: Implement `agtbox/cli.py`.** Port the argv split + argparse from `main` (598–643), renaming flags and sourcing `choices` from discovery; port `detect_engine`'s selection/validation (188–204) into `resolve_sandbox`; assemble the `RunContext` and dispatch:

```python
import argparse
import os
import sys
from agtbox import core
from agtbox.context import RunContext
from agtbox.discovery import discover_agents, discover_sandboxes
from agtbox.install import ensure_tools


def build_parser(sandbox_names, agent_names):
    p = argparse.ArgumentParser(
        prog="agtbox.py", allow_abbrev=False,
        description="Run an AI coding agent in an unprivileged sandbox.",
        epilog="Pass agent args after `--`, e.g. `%(prog)s claude -- --resume`.")
    p.add_argument("-a", dest="app_dir", default=os.getcwd(), metavar="DIR",
                   help="project directory, bound at the same path inside (default: cwd)")
    p.add_argument("-w", dest="volumes", action="append", default=[], metavar="VOL",
                   help="extra dir, bound read-write at the same path (repeatable)")
    p.add_argument("-r", dest="ro_volumes", action="append", default=[], metavar="VOL",
                   help="extra dir, bound read-only at the same path (repeatable)")
    p.add_argument("-s", dest="sandbox", choices=tuple(sandbox_names),
                   help="sandbox (default: auto -- highest priority available)")
    p.add_argument("-b", dest="rebuild", action="store_true",
                   help="rebuild the sandbox image (no-op for sandboxes without one)")
    p.add_argument("-u", "--update", dest="update", action="store_true",
                   help="refresh the toolchain + the agent's packages, then run")
    p.add_argument("agent", choices=tuple(agent_names),
                   help="the agent to run (or `bash` for an audit shell)")
    return p


def resolve_sandbox(name, sandboxes):
    if name:
        cls = sandboxes[name]
        if not cls.is_available():
            print(f"Error: sandbox '{name}' is selected but not installed.", file=sys.stderr)
            sys.exit(1)
        return cls()
    for n, cls in sorted(sandboxes.items(), key=lambda kv: -kv[1].priority):
        if cls.is_available():
            return cls()
    print("Error: no sandbox found (need bwrap or podman).", file=sys.stderr)
    sys.exit(1)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        sep = argv.index("--")
        left, extra = argv[:sep], argv[sep + 1:]
    else:
        left, extra = argv, []

    sandboxes = discover_sandboxes()
    agents = discover_agents()
    ns = build_parser(list(sandboxes), list(agents)).parse_args(left)

    sandbox = resolve_sandbox(ns.sandbox, sandboxes)
    agent = agents[ns.agent]()

    app_dir, volumes, ro_volumes = core.normalize_paths(ns.app_dir, ns.volumes, ns.ro_volumes)
    binds = [*core.SHARED_BINDS, *agent.binds]
    core.ensure_sources(binds)
    env = core.resolve_env(agent.env_forward, agent.env_literal)
    ctx = RunContext(agent=agent, binds=binds, env=env, app_dir=app_dir,
                     volumes=volumes, ro_volumes=ro_volumes, extra_args=extra)

    if ns.rebuild:
        sandbox.rebuild()
    sandbox.prepare(ctx)
    ensure_tools(agent, sandbox, force=ns.update)
    sandbox.run(ctx)
```

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m unittest test.test_cli -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agtbox/cli.py test/test_cli.py
git commit -m "feat(cli): dynamic argparse, sandbox resolution, RunContext dispatch"
```

---

## Task 11: Flip the entry point + port the integration suite green

**Files:**
- Modify: `bin/agtbox.py` (replace with shim)
- Modify: `test/test_agtbox.py` (port integration flags; migrate identity tests to `test_core.py`; delete obsolete unit-test classes)
- Modify: `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: everything built above. Produces: the working CLI at `bin/agtbox.py`.

- [ ] **Step 1: Replace `bin/agtbox.py` with the shim:**

```python
#!/usr/bin/env python3
"""Entry point: run an AI coding agent inside an unprivileged sandbox.
The implementation lives in the agtbox/ package."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from agtbox.cli import main

if __name__ == "__main__":
    main()
```

Keep it executable (`chmod +x bin/agtbox.py` is preserved by git).

- [ ] **Step 2: Port `test/test_agtbox.py` integration classes to the new flags.** Mechanical edits across the file:
  - `-t bwrap` → `-s bwrap`, `-t podman` → `-s podman` (every occurrence).
  - `-v <path>` → `-w <path>` (in `Volumes`, `test_v_and_r_distinct_paths`).
  - `-bt podman` → `-bs podman` (`test_clustered_flags`).
  - `test_same_path_v_and_r_rw_wins_with_warning`: assertion string `"given as both -v (rw) and -r (ro)"` → `"given as both -w (rw) and -r (ro)"`; update `normalize_paths`' message in `core.py` to match.
  - `_run`: remove the `env.pop("AGTBOX_REINSTALL", None)` line (the var is gone).
  - `NoEngine.test_no_engine_on_path`: assertion `"no sandbox engine found"` → `"no sandbox found"`.

- [ ] **Step 3: Rewrite the two behavior-changed tests.**
  - Replace `EngineSelect.test_rebuild_flag_warns_under_bwrap` with:

```python
    def test_rebuild_noop_under_bwrap(self):
        rc, argv, err = self.launch("-b", "-s", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("rebuild", err.lower())             # no warning
        self.assertArg(argv, str(self.tools / "bin/claude"))  # still runs
```

  - Replace `InstallTrigger.test_reinstall_env_forces_install` with:

```python
    def test_update_flag_forces_install(self):
        r = self.launch_capture("-u", "-s", "bwrap", "-a", str(self.app), "claude")
        self.assertEqual(r.rc, 0, r.err)
        self.assertTrue(r.inst, "-u must reinstall despite the stamp")
```

- [ ] **Step 4: Migrate the surviving unit tests, then delete the obsolete block.**
  - Move the two identity tests (`test_identity_files_seeded_from_host_and_append_missing_entries`, `test_identity_files_do_not_duplicate_existing_name_entries`) from `test_agtbox.py` into `test/test_core.py`, changing `m = load_agtbox(home)` to `m = fresh_core(home)` and `m.ensure_identity_files` / `m.AGENT_STATE` references to the `core` module (the function bodies and mock targets are otherwise identical).
  - Delete from `test_agtbox.py` (lines ~512–808): the `importlib` module-load block, `load_agtbox`, and classes `Helpers`, `EnsureSources`, `DeriveTz`, `RefreshCerts`, `BuildImage` — all replaced by `test_core.py` / `test_sandboxes.py` / `test_install.py`. `test_agtbox.py` keeps only the subprocess integration classes.

- [ ] **Step 5: Run the FULL suite, verify green**

Run: `python3 -m unittest discover -s test -v`
Expected: PASS — all integration tests (now `-s`/`-w`/`-u`) plus `test_core`, `test_agents`, `test_sandboxes`, `test_discovery`, `test_install`, `test_cli`.

- [ ] **Step 6: Smoke-test the real CLI surface (no network)**

Run: `./bin/agtbox.py -h` → Expected: exit 0, usage lists `-s`, `-w`, `-u`, and agents `{bash,claude,codex,opencode}`.
Run: `./bin/agtbox.py frob` → Expected: exit 2 (bad choice).

- [ ] **Step 7: Update docs.** In `CLAUDE.md`, rewrite the architecture sections: replace "single self-contained `bin/agtbox.py`" with the package + discovered-plugins model; change `engine`→`sandbox` and `tool`→`agent` vocabulary; update the flag list (`-s`/`-w`/`-u`, generic `-b`); replace "Adding an agent = add to NPM_PKGS + BIND tables + argparse choices" with "drop a file in `agtbox/agents/` (subclass `Agent`)"; note `AGTBOX_REINSTALL` is gone (use `-u`). In `README.md`, update usage/flags and the "fork to extend" story.

- [ ] **Step 8: Commit**

```bash
git add bin/agtbox.py test/test_agtbox.py test/test_core.py CLAUDE.md README.md
git commit -m "feat: flip entry point to agtbox package; port tests + docs to new flags"
```

---

## Self-Review

**Spec coverage** (each spec section → task):
- Package layout / shim → Tasks 1, 11. Discovery → Task 8. `Sandbox` ABC (fmt/prepare/run/install/rebuild) → Tasks 5–7. `Agent` ABC (bin/packages/binds/env) → Task 4. Core/shared (paths, generic env, git/gh/glab/ssh, ensure_sources, identity, normalize) → Tasks 1–3. Launcher flow (dynamic argparse, resolve, ctx, dispatch) → Task 10. Per-agent lazy install + two-step script + bash provisions core → Task 9 (`AGT_DO_CORE` gate; npm block always carries `AGT_NPM_PKGS` so `test_missing_stamp_triggers_install` stays green). `-u` replaces env var, requires agent → Tasks 9, 10, 11. `-b` generic `rebuild()` → Tasks 5, 7, 10, 11. CLI flag renames → Tasks 10, 11. Resolved decisions (RunContext mutable env, Bind seed not bound, install env asymmetry, lexists, deterministic discovery, sys.path/PROJ_DIR, sandbox resolution errors) → Tasks 2–10. Testing/docs → Task 11.
- **Gap check:** the macOS `lexists` test (`test_present_nonexecutable_bin_skips_install`) lives in `InstallTrigger` and is preserved by Task 11's flag port — `ensure_tools` uses `os.path.lexists` (Task 9), so it stays green. ✓

**Placeholder scan:** Tasks 6 and 7 use a deliberately-named *temporary* `agt_env` stub, explicitly replaced in Task 9 — flagged, not a silent TODO. `refresh_certs` body in Task 7 says "copied verbatim from current `refresh_certs()`" with the exact source lines (492–518) — a precise port instruction, not a vague placeholder.

**Type consistency:** `agt_env(agent, do_core, machine=None)` — signature matches across Tasks 6, 7, 9. `Sandbox.build_install_argv(script, agent, do_core)` and `Sandbox.install(script, agent, do_core)` and `ensure_tools(agent, sandbox, force)` are consistent across Tasks 5, 6, 7, 9, 10. `RunContext` field names (`agent`, `binds`, `env`, `app_dir`, `volumes`, `ro_volumes`, `extra_args`) match across Tasks 5, 6, 7, 10. `Bind(src, dst, kind)` consistent across Tasks 1, 3, 4, 5.
