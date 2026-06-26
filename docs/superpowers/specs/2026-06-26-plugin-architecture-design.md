# Design: plugin-based sandboxes and agents

**Date:** 2026-06-26
**Status:** Approved (pending spec review)

## Goal

Refactor Agent Box from a single self-contained `bin/agtbox.py` into a small
Python package whose two pluggable axes are **drop-in modules**:

1. **Sandboxes** — the isolation mechanism (today: `bwrap`, `podman`; future:
   `docker`, …).
2. **Agents** — the AI coding CLI being run (today: `claude`, `opencode`,
   `codex`, plus `bash` as an audit shell).

Each new sandbox or agent should be addable by writing one new file in the
matching directory — no edits to the core launcher.

## Naming decisions

- **`sandbox`** names the isolation axis (replaces the informal `engine`/`-t`).
  Podman can do far more than sandboxing, but sandboxing is why this project
  uses it, so the name reflects intent.
- **`agent`** names the CLI axis (replaces the informal `tool`). This matches
  how the project already speaks (`AGENT_BIN`, "AI coding agents") and how the
  vendors describe these programs ("coding agent"). The well-known overload —
  each agent spawns its own sub-agents — is accepted: within this codebase
  `agent` already means "the CLI we launch."

## Scope of "plugin"

Level **B — drop-in, discovered, in-repo only.** The launcher discovers
built-in plugins at runtime by scanning the package's `sandboxes/` and
`agents/` directories. There is **no** external/user plugin directory and **no**
stable third-party API: extension is by forking the repo. This keeps the
discovery machinery minimal and avoids committing to a public contract.

## Architecture

### Package layout

```
bin/agtbox.py            # thin entry shim: `from agtbox.cli import main; main()`
agtbox/
  __init__.py
  cli.py                 # main(): argv split, dynamic argparse, dispatch
  core.py                # paths/constants, env allowlist, ensure_sources, identity files
  context.py             # RunContext dataclass: binds + env + app_dir + extra_args + agent
  discovery.py           # scan sandboxes/ and agents/, import, collect ABC subclasses
  sandboxes/
    __init__.py
    base.py              # class Sandbox(ABC)
    bwrap.py             # class Bwrap(Sandbox)
    podman.py            # class Podman(Sandbox)
  agents/
    __init__.py
    base.py              # class Agent(ABC)
    claude.py            # class Claude(Agent)
    opencode.py          # class Opencode(Agent)
    codex.py             # class Codex(Agent)
    bash.py              # class Bash(Agent)
container/
  Containerfile          # unchanged; referenced by the podman sandbox
test/                    # restructured to mirror the package (see Testing)
```

- **100% stdlib.** Discovery uses `pkgutil.iter_modules` + `importlib`.
- `bin/agtbox.py` remains the primary entry point so `./bin/agtbox.py …` and all
  existing docs keep working. It only imports and calls `agtbox.cli.main`.

### The `Sandbox` ABC (`agtbox/sandboxes/base.py`)

One subclass per isolation mechanism. Required surface:

- `name: str` — e.g. `"bwrap"`, `"podman"`.
- `classmethod is_available() -> bool` — host capability probe
  (`shutil.which("bwrap")`, etc.).
- `priority: int` — auto-detect order; the highest-priority available sandbox
  wins when `-s` is not given (bwrap > podman, preserving today's preference).
- `fmt_env(pairs) -> list[str]` — format `(key, value)` env pairs into this
  sandbox's argv shape (`--setenv K V` vs `-e K=V`).
- `fmt_bind(src, dst, ro: bool) -> list[str]` — format one bind
  (`--bind`/`--ro-bind` vs `-v src:dst[:ro]`).
- `prepare(ctx)` — pre-run setup. Podman: `build_image` (+ `refresh_certs`),
  `derive_tz`. Bwrap: synthetic passwd/group identity files. (Bwrap's
  resolv.conf and CA-store handling stay inside its own base-args builder.)
- `run(ctx: RunContext)` — assemble the full argv and `os.execvp`.
- `install(packages, agent, ctx)` — run the install step in isolation (a nested
  `bwrap …` invocation, or `podman run`).
- `rebuild()` — rebuild the sandbox's container image, if it has one. **Default:
  no-op.** Called when `-b` is passed. Podman overrides it to rebuild the image
  (today's `build_image` with the rebuild path); bwrap inherits the no-op (it has
  no image). This makes `-b` a generic, sandbox-independent flag: a future
  `docker` sandbox would implement its own `rebuild()`, and bwrap silently does
  nothing rather than warning.

The `style="bwrap"|"podman"` branching in today's `_fmt_env`/`bind_args`/
`env_args` is replaced by polymorphism: core builds engine-agnostic
`(key,value)` and `(src,dst,ro)` lists; the chosen sandbox formats them.

### The `Agent` ABC (`agtbox/agents/base.py`)

One subclass per CLI. Required surface (with sensible defaults so a minimal
agent is a few lines):

- `name: str` — e.g. `"claude"`.
- `bin -> str` — path to the executable. Default
  `f"{AGENT_TOOLS}/bin/{name}"`; `bash` overrides to `/usr/bin/bash`.
- `packages -> list[str]` — npm packages this agent needs (e.g.
  `["@anthropic-ai/claude-code"]`). `bash` → `[]`.
- `binds -> list[Bind]` — this agent's config dir/file/seed binds. `claude` →
  `~/.claude` dir + `~/.claude.json` file; `codex` → `~/.codex`; `opencode` →
  its four XDG dirs; `bash` → `[]`.
- `env_forward -> list[str]` / `env_literal -> list[str]` — agent-specific env.
  `claude` → forward `ANTHROPIC_*`, literal `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`;
  `codex` → forward `OPENAI_*`/`CODEX_API_KEY`; `opencode` → forward
  `OPENCODE_ENABLE_EXA`, literal `OPENCODE_EXPERIMENTAL_LSP_TOOL=true`. `bash` → none.

`bash` is a fully valid degenerate `Agent` (empty packages/binds/env, absolute
bin). Core needs **no** `if tool == "bash"` special-casing — the class declares
everything.

A small `Bind` representation (dataclass or named tuple) carries
`src`, `dst`, `kind` (`dir`/`file`/`seed`), so `ensure_sources` and the sandbox
formatter both consume one typed list instead of the parallel
`BIND_DIRS`/`BIND_FILES`/`SEED_FILES` string tables.

### Core / shared (`agtbox/core.py`)

Stays in core because it belongs to neither axis:

- The XDG path constants (`AGENT_TOOLS`/`AGENT_CONFIG`/`AGENT_STATE`/
  `AGENT_CACHE`, arch-namespaced toolchain) and `AGENT_ENV` toolchain routing.
- The **generic** env allowlist: `TERM`/`COLORTERM`/`LANG`/`LC_*`, the proxy
  vars, and `HOME`/`PATH`/npm/pip/uv routing.
- The **git/gh/glab/ssh** binds — shared support infra every agent relies on,
  not agents themselves. (Could become a third "support" plugin kind later;
  YAGNI for now.)
- `ensure_sources` / identity-file generation / path normalization, retargeted
  to consume the typed `Bind` list (core's shared binds + the selected agent's
  binds).

### Launcher flow (`agtbox/cli.py`)

1. Split argv on the first `--` (agent argv after it, verbatim — unchanged).
2. Discover sandboxes and agents; build argparse `choices` **dynamically** from
   the discovered names, so `--help`, `-s`, and the positional reflect whatever
   plugins exist.
3. Resolve the sandbox (`-s` or highest-priority `is_available()`) and the
   agent (positional).
4. Core assembles the `RunContext`: shared binds + generic env, unioned with the
   agent's binds + env and the user's `-w`/`-r` volumes and `-a` project dir.
5. If `-b`: `sandbox.rebuild()`. Then `sandbox.prepare(ctx)` →
   `ensure_tools(agent, force=update_flag)` → `sandbox.run(ctx)`.

### Install flow (per-agent lazy)

- The shared toolchain (node, uv, gh, glab) stays gated by
  `${AGENT_TOOLS}/.stamp` — installed once on first run.
- Each agent's `packages` install lazily, when that agent's `bin` is absent
  (the existing `os.path.lexists(AGENT_BIN)` presence check, now scoped to the
  launched agent).
- The install splits into two **independently gated** steps: a shared "core
  toolchain" step (node + uv/gh/glab, today's logic, gated by the `.stamp`) and
  an "npm install $AGT_NPM_PKGS" step parameterized by the launched agent's
  `packages` and gated by that agent's `bin` presence. They run independently, so
  launching a second agent (stamp present, its bin absent) runs **only** the npm
  step and never re-downloads node.
- `bash` has empty `packages`, so its npm step is a no-op — but it **still
  provisions the shared core toolchain** on a fresh machine (the `.stamp` gate is
  agent-independent). Running `agtbox bash` first installs node/uv/gh/glab and no
  agent packages, matching today's "first run sets up the toolchain" behavior.

### Updating the toolchain (`-u` / `--update`)

`-u` forces a re-run of the install regardless of the `.stamp`/bin presence
checks: it refreshes the shared toolchain (latest node LTS + uv/gh/glab) **and**
re-installs the launched agent's `packages`, then launches as usual. It **always
requires an agent** positional (there is no standalone toolchain-only update
mode); updating a different agent is a separate `-u <agent>` run, consistent with
the per-agent lazy install model.

This **replaces** the `AGTBOX_REINSTALL=1` env var entirely (no users to break,
and a discoverable flag beats a hidden env var). Internally it sets the same
"force reinstall" condition `ensure_tools` already checks — just driven by a
flag instead of `os.environ`.

## Command-line interface changes

Flags renamed to match the new vocabulary (UI change, explicitly requested):

| Old | New | Meaning |
|-----|-----|---------|
| `-t <podman\|bwrap>` | `-s <sandbox>` | force the sandbox (default: auto) |
| `-v <VOL>` | `-w <VOL>` | extra read-write bind (write), repeatable |
| `-r <VOL>` | `-r <VOL>` | extra read-only bind (read), unchanged |
| `-a <DIR>` | `-a <DIR>` | project dir, unchanged |
| `-b` | `-b` | rebuild the sandbox's image (no-op for sandboxes without one) |
| `AGTBOX_REINSTALL=1` env | `-u` / `--update` | refresh the toolchain + launched agent's packages |
| `tool` positional | `agent` positional | the agent to run (or `bash`) |

New usage:

```
agtbox.py [-a DIR] [-w VOL] [-r VOL] [-s sandbox] [-b] [-u] <agent> [-- agent args...]
```

`-s` and the `agent` positional accept dynamically-discovered choices, so
adding a plugin updates `--help` automatically. `-b` is now sandbox-independent
(backed by `Sandbox.rebuild()`, default no-op), so it stays valid for any future
image-based sandbox; under bwrap it does nothing.

## Behavior parity

A faithful refactor: net observable behavior is unchanged **except** the
deliberate, requested changes — the renamed flags (`-s`/`-w`), the now-dynamic
`--help`/choices, `-u`/`--update` replacing `AGTBOX_REINSTALL=1`, and `-b`
becoming sandbox-independent. There are no users yet, so further behavior
changes are acceptable; any additional *user-interface* change beyond the above
will be raised before implementing.

## Resolved implementation decisions

Captured from a pre-implementation review so the plan has no open questions:

- **`RunContext`** carries: the typed bind list (shared + agent + user `-w`/`-r`,
  already merged), the resolved env as `(key, value)` pairs (`ENV_FORWARD`
  resolved against `os.environ` at assembly time), `app_dir`, `extra_args`, and
  the selected `agent`. `ctx.env` is **mutable**: `Sandbox.prepare()` may append
  to it (this is how podman injects `TZ` from `derive_tz` — replacing today's
  mutation of the module-global `ENV_LITERAL`).
- **`Bind` kinds:** `dir` and `file` both emit a bind arg (file additionally
  seeds `{}` in `ensure_sources`); `seed` emits **no** bind arg at all — it is an
  `ensure_sources`-only action (e.g. git's `~/.config/git/config`, written inside
  the already-bound git dir). The sandbox formatter skips `kind=seed`.
- **Install env asymmetry preserved:** the bwrap install path carries the full
  allowlist *including the launched agent's `env_literal`* (via the bwrap base
  args); the podman install path carries `AGENT_ENV` only. This matches today's
  tested behavior.
- **Two-step install gating:** core toolchain gated by `${AGENT_TOOLS}/.stamp`
  (written last, as today); agent npm packages gated by `os.path.lexists(bin)`
  (presence only — never `isfile`/`X_OK`, which regresses a real macOS/VM-mount
  bug). `-u` forces both.
- **Discovery** yields plugins in a deterministic (sorted-by-name) order so
  argparse `choices`, `--help`, and bad-choice error text are stable. A plugin
  module that fails to import aborts with a clear error rather than being silently
  skipped.
- **Entry/paths:** `bin/agtbox.py` prepends the repo root to `sys.path` before
  `from agtbox.cli import main`. `PROJ_DIR` (the `container/` location) is
  recomputed from the podman module's own `__file__`, not assumed relative to the
  entry script.
- **Sandbox resolution errors:** `-s <name>` that is discovered but whose
  `is_available()` is false → exit 1 ("sandbox 'X' is selected but not
  installed"); no sandbox available at all → exit 1 ("no sandbox found"). Same
  exit codes/intent as today's engine checks.

## Testing

TDD throughout (write/adjust the test, watch it fail, implement):

- Restructure `test/` to mirror the package. Keep the stub-`bwrap` /
  stub-`podman` integration tests that assert the final exec argv — that argv is
  unchanged, so these remain the parity backstop (updated only for the renamed
  flags).
- Add focused unit tests: discovery (finds all built-in plugins; dynamic
  argparse choices), the `Agent` contract (each agent's bin/packages/binds/env),
  the `Sandbox` formatters (`fmt_env`/`fmt_bind` shapes), and per-agent lazy
  install (only the launched agent's packages are requested).
- The pure helpers (`arch_pair`, path normalization, `_kv`/`_split_pair`
  successors) keep their unit tests.

## Documentation

- **CLAUDE.md** — rewrite the architecture sections: "single self-contained
  `bin/agtbox.py`" becomes "a package with discovered sandbox/agent plugins";
  update the vocabulary (engine→sandbox, tool→agent), the flag table, and the
  "adding an agent" recipe (now: drop a file in `agents/`).
- **README.md** — update the usage/flags (`-s`/`-w`) and the extension story
  (fork + add a plugin file).

## Out of scope (YAGNI)

- External / user-supplied plugin directories.
- A stable public plugin API or versioning.
- A `docker` sandbox or any new agent (the architecture must *allow* them; this
  work does not add them).
- Promoting git/gh/glab/ssh into their own plugin kind.
