# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Bash launchers + Containerfiles + Compose files for running AI coding agents (Claude Code, Claude Desktop, opencode) inside rootless containers, alongside local LLM inference servers (Ollama, LocalAI). There is no application code, build system, or test suite — the "source" is shell scripts and container definitions.

## Common commands

Agent launchers (`bin/`, runnable from anywhere; default mount target = current dir):

```bash
./bin/ccc.sh [-a APP_DIR] [-v VOL] [-t podman|charliecloud] [-c] [-e EFFORT] [-s SESSION] [-f] [-r]
./bin/occ.sh [-a APP_DIR] [-v VOL] [-t podman|charliecloud] [-c] [-s SESSION] [-f] [-r]   # opencode
./bin/cdt.sh [-a APP_DIR] [-v VOL] [-g] [-r]                                              # Claude Desktop GUI (podman only)
```

- `-r` forces an image rebuild; `-t` overrides engine auto-detection; `-h` prints full usage.
- ccc/occ `-c` / `-s` / `-f` map to the agent's continue / resume / fork-session options; ccc `-e` sets the effort level.

Local model servers — `ollama.sh`/`localai.sh` are thin wrappers around `bin/service.sh`, which needs a Compose provider (`podman-compose` works; it also falls back to `podman compose`):

```bash
./bin/ollama.sh  up|down|attach     # ollama/  → port 11434   (= bin/service.sh ollama …)
./bin/localai.sh up|down|attach     # localai/ → port 8080    (= bin/service.sh localai …)
```

`attach` starts the stack first if it isn't already running, then execs a shell inside the container.

There is no separate build/lint/test step — agent images build lazily on first launch (or with `-r`).

## Architecture

**Two-stage image hierarchy.** `agents/Containerfile.base` builds `agent-base` (node:trixie + ansible/git/python/C toolchain/rust + host CA certs). Each agent image (`Containerfile.claude-code`, `.claude-desktop`, `.opencode`) is `FROM agent-base` and only installs its own tool. Editing the base affects all three.

**Launcher pattern (ccc.sh and occ.sh are near-identical).** Each launcher:
1. Auto-detects the engine — prefers Charliecloud (`ch-run`) if present, else podman; `-t` overrides.
2. Lazily builds `agent-base`, then the tool image, if missing (`-r` deletes and rebuilds both).
3. Mounts the target dir **at the same absolute path inside the container**, so file paths in agent output stay valid on the host; extra `-v` volumes follow the same host==container rule.
4. Bind-mounts host agent config/state so the container itself stays ephemeral (`--rm`): ccc → `~/.claude/` + `~/.claude.json`; occ → `~/.config/opencode/` + `~/.local/share/opencode/`.

Because ccc.sh and occ.sh duplicate this engine/build/mount logic, a change to one (how volumes, engines, or mounts are handled) usually needs the same change in the other.

**Charliecloud vs podman.** Podman runs the tool images directly. Charliecloud builds the same Containerfiles via `ch-image`, converts them to dir format under `agents/.charliecloud/`, and runs them unprivileged with `ch-run --write-fake --private-tmp`. Both paths produce the same mount layout.

**Custom CA certs.** Before building, launchers copy `~/.local/share/certs/*` into `agents/certs/` (gitignored); the base image `COPY`s them in and runs `update-ca-certificates` — needed behind TLS-intercepting proxies. Missing certs are silently skipped.

**Pointing agents at local models.** `ccc.sh` forwards `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_DEFAULT_SONNET_MODEL` from the host env, but only when they are set (so unset vars don't shadow the mounted `~/.claude.json`) — set these to aim Claude Code at the Ollama (`:11434`) or LocalAI (`:8080`) server (or any proxy) instead of the default API. `occ.sh` instead sets `OPENCODE_ENABLE_EXA` and `OPENCODE_EXPERIMENTAL_LSP_TOOL`.

**Claude Desktop GUI (cdt.sh, podman only).** Displays via native Wayland by bind-mounting the host Wayland socket (plus D-Bus when present). The container is the security boundary, so inner sandboxes are disabled: Electron runs `--no-sandbox` and Cowork is pinned to `COWORK_VM_BACKEND=host`. Default passes `/dev/dri` for GPU rendering; `-g` switches to software rendering. Known trade-off (documented in-file): the global hotkey doesn't work under native Wayland.

**Local inference stacks.** `ollama/` and `localai/` each hold a `compose.yaml` exposing an AMD GPU (`/dev/kfd` + `/dev/dri`) with Vulkan. Model data persists to gitignored subdirs (`ollama/ollama`, `localai/models`, `localai/backends`).
