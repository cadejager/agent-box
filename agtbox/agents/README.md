# Agent plugins

An **agent** is one AI coding CLI that Agent Box can launch inside the sandbox
(e.g. `claude`, `opencode`, `codex`, plus `bash` as an audit shell). Each is a
small plugin: one file in this directory defining a subclass of `Agent`.

Plugins are **discovered at runtime** (`agtbox/discovery.py` scans this folder),
so adding one requires **no** edits to the CLI, argparse choices, or install
tables — drop the file and it appears in `--help` and as a launch target.

## How to add an agent

1. Create `agtbox/agents/<name>.py`. **The file name must equal the agent's
   `name`** (`claude.py` → `name = "claude"`); discovery and a test enforce this.
2. Define a class subclassing `Agent` (from `agtbox.agents.base`) and set the
   fields below.
3. Add a test to `test/test_agents.py` asserting its `bin`/`packages`/`binds`/env.
4. Run `python3 -m unittest discover -s test`.

That's it — no registry to update.

## Fields and properties

Subclass `agtbox.agents.base.Agent` and set what you need. Everything except
`name` has a sensible default, so a minimal agent is a few lines.

| Member | Kind | Default | Meaning |
|--------|------|---------|---------|
| `name` | class attr `str` | `""` (**required**) | The CLI name, the argparse choice, the discovery key. Must match the file name. |
| `packages` | class attr `tuple[str]` | `()` | **npm** package names, installed with `npm install -g` into the shared toolchain the first time this agent is launched. `()` if the agent ships no npm package (e.g. `bash`). |
| `bin` | property `str` | `f"{core.AGENT_TOOLS}/bin/{name}"` | Path to the executable **inside the sandbox**. The default points at where `npm -g` installs binaries. Override only for a system binary (`bash` → `/usr/bin/bash`). |
| `binds` | property `list[core.Bind]` | `[]` | The agent's own config/state directories to persist across runs (see below). |
| `env_forward` | class attr `tuple[str]` | `()` | Env var **names** forwarded from the host **only when set** — API keys, base URLs, model overrides. An unset var is never passed (so it can't shadow mounted config). |
| `env_literal` | class attr `tuple[str]` | `()` | `"KEY=VALUE"` strings **always** set for this agent (feature flags, etc.). |

Use **tuples** (not lists) for `packages`/`env_forward`/`env_literal` so the
shared class-level default can never be mutated in place and leak between agents.

### `binds` — persisting the agent's config

Each `Bind(source, dest, kind)` mounts a host directory onto the path the agent
expects inside the sandbox. The **source** lives under one of the four
persistent Agent Box bases (namespaced so nothing collides), and the **dest** is
wherever the tool looks:

- `core.AGENT_CONFIG` — config (`~/.config/agent-box`)
- `core.AGENT_TOOLS` — data / installed files (`~/.local/share/agent-box/<arch>`)
- `core.AGENT_STATE` — state (`~/.local/state/agent-box`)
- `core.AGENT_CACHE` — disposable cache (`~/.cache/agent-box`)

`kind` (default `"dir"`):

- `"dir"` — bind the directory (created on the host if absent).
- `"file"` — bind a single file, seeded with `{}` if absent (for tools that
  demand a valid-JSON file, e.g. `~/.claude.json`).
- `"seed"` — **not mounted**; an empty file created inside an already-bound dir,
  for tools that write via temp-file-and-rename and can't have the target itself
  be a mountpoint (this is how the shared git config is handled).

You do **not** declare git/gh/glab/ssh here — those are shared infrastructure
that `core.SHARED_BINDS` provides to every agent automatically.

## Worked example

A hypothetical `gemini` agent (npm package `@google/gemini-cli`, config in
`~/.gemini`, forwards a `GEMINI_API_KEY`):

```python
# agtbox/agents/gemini.py
from agtbox import core
from agtbox.agents.base import Agent


class Gemini(Agent):
    name = "gemini"
    packages = ("@google/gemini-cli",)
    env_forward = ("GEMINI_API_KEY",)

    @property
    def binds(self):
        return [core.Bind(f"{core.AGENT_CONFIG}/gemini", f"{core.HOME}/.gemini")]
```

Launch it with `./bin/agtbox.py gemini` — its npm package installs on first run,
its config persists under `~/.config/agent-box/gemini`, and `GEMINI_API_KEY` is
forwarded when you have it exported.

See `claude.py` (dir + JSON-file binds, several forwards), `opencode.py` (all
four XDG bases), `codex.py` (minimal), and `bash.py` (degenerate: a system
binary, no packages/binds/env) for real examples.
