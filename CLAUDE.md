# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Bash launcher + Containerfiles for running AI coding agents (Claude Code, opencode, codex) inside rootless containers. There is no application code or build system — the "source" is one shell script and the container definitions. (Human-facing setup and usage live in `README.md`; this file is the terse, architecture-level reference.)

## Common commands

Single launcher (`bin/agtbox.sh`, runnable from anywhere; default mount target = current dir):

```bash
./bin/agtbox.sh [-a APP_DIR] [-v VOL] [-r VOL] [-t podman|charliecloud] [-b] claude|opencode|codex [tool args...]
```

- Container flags (`-a`/`-v`/`-r`/`-t`/`-b`/`-h`) come **before** the tool name; `-b` forces an image rebuild, `-t` overrides engine auto-detection, `-h` prints full usage.
- **Everything after the tool name is passed to the agent verbatim** (no flag remapping) — session handling is each tool's own syntax: claude `--continue` / `--resume [ID]` / `--fork-session`; opencode `--continue` / `--session ID` / `--fork`; codex `resume [ID]` / `fork [ID]`. `-r VOL` is the **read-only volume** flag (repeatable; bound at the same host==container path) — under podman it is enforced with `:ro`, under Charliecloud it falls back to a read-write `-b` bind plus a one-time warning (see below).

Agent images build lazily on first launch (or with `-b`). The launcher's argv construction has a stub-engine test: `./test/argv.sh` (pure bash, no framework).

## Architecture

**Two-stage image hierarchy.** `agents/Containerfile.base` builds `agent-base` (node:trixie + ansible, git, bubblewrap, a Python — `venv`/`pip`/`pipx` — + C/C++ + Rust build toolchain, common CLI tools — ripgrep, jq, less, sqlite3, tree, fd-find, rsync, zip, shellcheck — and host CA certs). Each agent image (`Containerfile.claude-code`, `.opencode`, `.codex`) is `FROM agent-base` and only installs its own tool. Editing the base affects all of them.

**Launcher — single self-contained `bin/agtbox.sh`.** Invoked `agtbox.sh <container flags> <tool> <tool args…>`: `getopts` parses the container flags and stops at the first non-option (the tool name), so everything after the tool name is the agent's own args, forwarded verbatim. A `case "$TOOL"` sets the only per-agent differences — image, Containerfile, in-container binary, config mounts, and env (`ENV_FORWARD`/`ENV_LITERAL`). The rest are `agent::*` functions in the same file:
1. Auto-detects the engine — prefers Charliecloud (`ch-run`) if present, else podman; `-t` overrides.
2. Lazily builds `agent-base`, then the tool image, if missing (`-b` deletes and rebuilds both).
3. Mounts the target dir **at the same absolute path inside the container**, so file paths in agent output stay valid on the host; `-v` (read-write) and `-r` (read-only) volumes follow the same host==container rule.
4. Bind-mounts host agent config/state — auto-creating each source dir/file if missing (`agent::ensure_config_sources`) — while keeping the container itself ephemeral (`--rm`): claude → `~/.claude/` + `~/.claude.json`; opencode → `~/.config/opencode/` + `~/.local/share/opencode/` + `~/.cache/opencode/` + `~/.local/state/opencode/`; codex → `~/.codex/`.

Adding an agent means a new `case` arm in `agtbox.sh` + a Containerfile; changing engine/build/run behavior means editing the shared `agent::*` functions once.

**Ephemeral by design.** Containers run `--rm`, so anything installed *globally* inside (apt/pip/npm) is discarded next run; persist work in the mounted dir instead — a project `.venv`/`node_modules` survive because `APP_DIR` is bind-mounted at the same path. The pip + npm **download caches** persist across runs via `SHARED_MOUNTS` in `agtbox.sh` (host `~/.cache/podman-ai-agents/{pip,npm}/` → the containers' default cache dirs), so global re-installs are fast even though the packages themselves stay ephemeral. Debian's PEP 668 blocks system/`--user` pip installs, so Python deps go in a project venv.

**Charliecloud vs podman.** Podman runs the tool images directly. Charliecloud builds the same Containerfiles via `ch-image`, converts them to dir format under `agents/.charliecloud/`, and runs them unprivileged with `ch-run --write-fake --private-tmp`. Both paths produce the same mount layout. Three ch-run specifics (untested in the podman-only dev env — verify on HPC): a missing `-b` destination auto-creates as a *directory*, so `Containerfile.claude-code` pre-`touch`es `/root/.claude.json` for that file-bind; forwarded env values use `--env-no-expand` so colon/`$` values aren't path-expanded; and `ch-run` has no read-only bind option, so `-r VOL` read-only volumes are mounted **read-write** (with a one-time stderr warning) — the read-only guarantee holds only under podman. (The podman path adds a `--` before the image as well, so a `-`-leading passed-through tool arg can never be misread as a podman flag.)

**Custom CA certs.** Before building, the launcher copies `~/.local/share/certs/*` into `agents/certs/` (gitignored); the base image `COPY`s them in and runs `update-ca-certificates` — needed behind TLS-intercepting proxies. Missing certs are silently skipped.

**Pointing agents at local models.** `agtbox.sh` sets per-tool env in its `case` block: **claude** forwards `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_DEFAULT_SONNET_MODEL` from the host env, but only when set and non-empty (so empty values don't shadow the mounted `~/.claude.json`) — set these to aim Claude Code at a local OpenAI/Anthropic-compatible server (e.g. LM Studio) or any proxy. **opencode** sets `OPENCODE_ENABLE_EXA` and `OPENCODE_EXPERIMENTAL_LSP_TOOL`. **codex** forwards `OPENAI_API_KEY`/`CODEX_API_KEY` when set and mounts `~/.codex/`; codex ignores `OPENAI_BASE_URL`, so aiming it at a local server must be done in `~/.codex/config.toml`, not env. Auth/sessions persist to the writable `~/.codex/` mount (like `~/.claude/`); if a `codex login` doesn't stick, set `cli_auth_credentials_store = "file"` there.

**Host timezone.** `agent::derive_tz` (in `agtbox.sh`) derives the host's IANA timezone (`timedatectl` → `/etc/timezone` → the `/etc/localtime` symlink → `$TZ`) and appends it to `ENV_LITERAL` as `TZ=...`, so both engines forward it and containers report host-local time instead of UTC. `agent-base` ships tzdata, so the name resolves with no Containerfile change. If the zone can't be derived, `TZ` is left unset and the container stays UTC (the prior behaviour).
