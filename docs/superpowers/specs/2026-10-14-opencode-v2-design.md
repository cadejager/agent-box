# opencode 2.0 (standalone) — design

Branch: `opencode-v2`. Scope: the agent-box harness only. Porting the opencode
config repo (`~/.config/agent-box/opencode`) is a separate, follow-up change.

## Goal

`agtbox.py opencode` runs opencode 2.0 instead of v1, always in **standalone** mode
(a private per-invocation server), and never starts v2's shared background service.
v1 is **replaced**, not kept alongside.

## Background (opencode 2.0)

- npm package is `@opencode/cli` (v1: `opencode-ai`). Both ship `bin/opencode`, so
  they collide in the shared toolchain prefix (`${AGENT_TOOLS}`); upstream does not
  support side-by-side installs.
- By default every client discovers/starts one shared per-user server
  (`opencode serve --service`). `opencode --standalone` runs a private server for
  that invocation instead.
- `opencode service set disabled true` makes private servers the default for every
  CLI command (including `opencode run`, `opencode api`, … that the agent might
  spawn itself). It is persisted in `~/.config/opencode/service.json` — inside the
  sandbox that is the bound `~/.config/agent-box/opencode` (the config repo).
- The daemon model is unusable here: a daemon can't outlive the sandbox (which dies
  with the launcher via `--die-with-parent` / `podman run --rm`), so it would only
  leave a stale registration in `~/.local/state/opencode/service.json`.

Both mechanisms are used: `--standalone` guarantees the TUI we launch is private;
`service disabled` covers commands that don't go through our launch argv.

## Changes

### 1. Package swap — `agtbox/agents/opencode.py`

- `packages = ("@opencode/cli",)`.
- New class attr `obsolete_packages = ("opencode-ai",)`.

### 2. Removing obsolete packages — `agtbox/agents/base.py`, `agtbox/install.py`

- `Agent.obsolete_packages = ()` (tuple, like the other sequence defaults).
- `agt_env` adds `AGT_NPM_REMOVE` = space-joined `obsolete_packages`.
- Install script: before the npm install step, if `AGT_NPM_REMOVE` is non-empty, run
  `npm uninstall -g --prefix "${AGT_TOOLS}" ${AGT_NPM_REMOVE}` (a no-op if absent).
- No new reinstall trigger. A v1 toolchain already has `bin/opencode`, so a plain
  launch keeps running v1 (which reports it isn't 2.0); the user upgrades explicitly
  with `agtbox.py -u opencode`.

### 3. Default launch args — `agtbox/agents/base.py`, both sandboxes

- `Agent.default_args = ()`; opencode sets `("--standalone",)`.
- `bwrap.build_run_argv` / `podman.build_run_argv` emit
  `ctx.agent.bin, *ctx.agent.default_args, *ctx.extra_args`. User args after `--`
  stay verbatim and come after the defaults.

### 4. Disabling the service — `agtbox/agents/base.py`, `agtbox/sandboxes/base.py`, `agtbox/cli.py`

- `Agent.setup_args = ()`; opencode sets `("service", "set", "disabled", "true")`.
- Must run with the agent's config binds, which the install sandbox deliberately
  lacks (toolchain + cache only). So it runs in the **run** sandbox:
  `Sandbox.run_setup(ctx)` in `sandboxes/base.py` builds the run argv for a copy of
  `ctx` whose command is `agent.bin, *setup_args` (no `default_args`, no user args)
  and runs it with `subprocess.run(..., check=True)`. No-op when `setup_args` is
  empty.
- Runs on **every launch**: `cli.main()` calls `sandbox.run_setup(ctx)` after
  `ensure_tools()` and before `sandbox.run()`. Costs one short extra opencode start
  per run; keeps the setting enforced even if `service.json` is removed or reset.
- It writes `service.json` into the config repo; the user commits or gitignores it.

### 5. Docs

- `CLAUDE.md`, `README.md`, `agtbox/agents/README.md`: v2 package, `--standalone`
  default, `service disabled` setup step, the new `default_args` / `setup_args` /
  `obsolete_packages` agent attributes, and the shared-toolchain note below.

## Shared-toolchain caveat

`main` and `opencode-v2` share `~/.local/share/agent-box/<arch>`. Once the branch
upgrades it, `main`'s `opencode` launches v2 too (without `--standalone`), and main
can't downgrade by itself (it doesn't know to remove `@opencode/cli`). Roll back with
`npm uninstall -g --prefix ~/.local/share/agent-box/<arch> @opencode/cli` then
`agtbox.py -u opencode` on main.

## Config-repo follow-up (not in this branch)

Harness first, then the config repo on its own branch, then merge both:
- `plugins/bedrock-tools-fix.js` — v1 plugin API (`@opencode-ai/plugin` 1.14.28);
  V1 plugins don't run in V2, needs porting.
- `superpowers` plugin (`opencode.json`) — needs a v2-compatible release.
- `opencode.json`, `agent/`, `skills/` — v2 reads them as-is; converting to the
  native v2 shape is optional.
- Commit or gitignore the generated `service.json`.

## Testing

Unit (`python3 -m unittest discover -s test`):
- `test_agents.py`: opencode `packages`, `obsolete_packages`, `default_args`,
  `setup_args`; other agents' defaults are empty.
- `test_sandboxes.py`: run argv is `bin, *default_args, *extra_args` for both
  sandboxes; `run_setup` argv ends with `bin, *setup_args` and carries the config
  binds.
- `install` tests: `AGT_NPM_REMOVE` in env; script contains the uninstall step.
- `cli` tests: `run_setup` is called on every launch, before `run`; it runs nothing
  for agents with empty `setup_args`.

Manual (bwrap): on an existing v1 toolchain, `agtbox.py -u opencode` → v1 removed,
v2 installed, `service.json` written, TUI starts; `ps` inside shows no `serve --service`; then
`agtbox.py opencode -- run "hi"` also stays private. Podman: same, if available.

## Verify once implemented (expected to be fine)

- That `--standalone` is accepted as a leading global flag before subcommands
  (`opencode --standalone run …`, `opencode --standalone --continue`). If not,
  fall back to relying on `service disabled` alone for subcommand invocations.
- That `opencode service set disabled true` succeeds with no service running and
  needs no network.
