# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Bash launchers + Containerfiles for running AI coding agents (Claude Code, opencode, codex) inside rootless containers. There is no application code, build system, or test suite — the "source" is shell scripts and container definitions. (Human-facing setup and usage live in `README.md`; this file is the terse, architecture-level reference.)

## Common commands

Agent launchers (`bin/`, runnable from anywhere; default mount target = current dir):

```bash
./bin/ccc.sh [-a APP_DIR] [-v VOL] [-t podman|charliecloud] [-c] [-r [ID]] [-f] [-b]
./bin/occ.sh [-a APP_DIR] [-v VOL] [-t podman|charliecloud] [-c] [-r [ID]] [-f] [-b]   # opencode
./bin/cdx.sh [-a APP_DIR] [-v VOL] [-t podman|charliecloud] [-c] [-r [ID]] [-f] [-b]   # codex (OpenAI Codex CLI)
```

- `-b` forces an image rebuild; `-n` shares the host network (`--network=host`, podman only); `-t` overrides engine auto-detection; `-h` prints full usage.
- All three launchers share the same flags `-c` / `-r` / `-f` (continue / resume / fork; bare `-r` opens the session picker, `-r ID` resumes by id), mapped to each agent's native form (for codex these become the `resume`/`fork` subcommands). Everything after `--` is passed through to the agent — that's how you reach per-tool options like `--model`, claude's `--effort`, or codex's `-c model_reasoning_effort=…` (each launcher's `-h` lists the common ones).

There is no separate build/lint/test step — agent images build lazily on first launch (or with `-b`).

## Architecture

**Two-stage image hierarchy.** `agents/Containerfile.base` builds `agent-base` (node:trixie + ansible/git/python/C toolchain/rust + host CA certs). Each agent image (`Containerfile.claude-code`, `.opencode`, `.codex`) is `FROM agent-base` and only installs its own tool. Editing the base affects all of them.

**Launcher pattern — `bin/lib/agent-run.sh` + thin wrappers.** `ccc.sh`, `occ.sh`, and `cdx.sh` source the shared lib, declare only what differs (image, Containerfile, config mounts, env vars, agent binary, flag→arg mapping), then call `agent::launch`. The lib:
1. Auto-detects the engine — prefers Charliecloud (`ch-run`) if present, else podman; `-t` overrides.
2. Lazily builds `agent-base`, then the tool image, if missing (`-b` deletes and rebuilds both).
3. Mounts the target dir **at the same absolute path inside the container**, so file paths in agent output stay valid on the host; extra `-v` volumes follow the same host==container rule.
4. Bind-mounts host agent config/state — auto-creating each source dir/file if missing (`agent::ensure_config_sources`), so a fresh user can launch — while keeping the container itself ephemeral (`--rm`): ccc → `~/.claude/` + `~/.claude.json`; occ → `~/.config/opencode/` + `~/.local/share/opencode/`; cdx → `~/.codex/`.

Adding an agent means a new wrapper + Containerfile; changing engine/build/run behavior means editing `bin/lib/agent-run.sh` once.

**Ephemeral by design.** Containers run `--rm`, so anything installed *globally* inside (apt/pip/npm) is discarded next run; persist work in the mounted dir instead — a project `.venv`/`node_modules` survive because `APP_DIR` is bind-mounted at the same path.

**Charliecloud vs podman.** Podman runs the tool images directly. Charliecloud builds the same Containerfiles via `ch-image`, converts them to dir format under `agents/.charliecloud/`, and runs them unprivileged with `ch-run --write-fake --private-tmp`. Both paths produce the same mount layout. Two ch-run specifics (untested in the podman-only dev env — verify on HPC): a missing `-b` destination auto-creates as a *directory*, so `Containerfile.claude-code` pre-`touch`es `/root/.claude.json` for that file-bind; and forwarded env values use `--env-no-expand` so colon/`$` values aren't path-expanded.

**Custom CA certs.** Before building, launchers copy `~/.local/share/certs/*` into `agents/certs/` (gitignored); the base image `COPY`s them in and runs `update-ca-certificates` — needed behind TLS-intercepting proxies. Missing certs are silently skipped.

**Pointing agents at local models.** `ccc.sh` forwards `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_DEFAULT_SONNET_MODEL` from the host env, but only when set and non-empty (so unset/empty vars don't shadow the mounted `~/.claude.json`) — set these to aim Claude Code at a local OpenAI/Anthropic-compatible server (e.g. LM Studio) or any proxy, instead of the default API. `occ.sh` instead sets `OPENCODE_ENABLE_EXA` and `OPENCODE_EXPERIMENTAL_LSP_TOOL`. `cdx.sh` forwards `OPENAI_API_KEY`/`CODEX_API_KEY` when set and mounts `~/.codex/`; codex ignores `OPENAI_BASE_URL`, so aiming it at a local server must be done in `~/.codex/config.toml`, not env. Auth/sessions persist to the writable `~/.codex/` mount (like `~/.claude/`); if a `codex login` doesn't stick, set `cli_auth_credentials_store = "file"` there. A server bound to the host's `localhost` is unreachable from the podman container (its `localhost` is its own net namespace); `-n` adds `--network=host` so the container's `localhost` is the host's — needed to reach e.g. LM Studio on `localhost:<port>`. Charliecloud (`ch-run`) already shares the host net, so `-n` is a no-op there.
