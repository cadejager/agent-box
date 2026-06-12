# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Agent Box: a single Bash launcher + one Containerfile for running AI coding agents (Claude Code, opencode, codex) inside a rootless container. No application code or build system — the "source" is `bin/agtbox.sh`, `container/Containerfile`, and `container/config-layout.sh`. (Human-facing setup/usage live in `README.md`; this file is the terse architecture reference.)

## Common commands

```bash
./bin/agtbox.sh [-a APP_DIR] [-v VOL] [-r VOL] [-t podman|charliecloud] [-b] claude|opencode|codex [tool args...]
```

- Container flags (`-a`/`-v`/`-r`/`-t`/`-b`/`-h`) come **before** the tool name; `-b` rebuilds the image, `-t` overrides engine auto-detection, `-h` prints full usage.
- **Everything after the tool name is passed to the agent verbatim** (no flag remapping) — session handling is each tool's own syntax (claude `--continue`/`--resume [ID]`/`--fork-session`; opencode `--continue`/`--session ID`/`--fork`; codex `resume [ID]`/`fork [ID]`). `-r VOL` is the read-only volume flag (podman enforces `:ro`; Charliecloud falls back to rw + a one-time warning).
- The image builds lazily on first launch (or with `-b`). The launcher's argv construction has a stub-engine test: `./test/argv.sh`.

## Architecture

**One image — `container/Containerfile` → `agent-box`.** `FROM node:trixie` + the apt toolchain (build tools, rust, python `venv`/`dev`/`pipx`, common CLI tools) + host CA certs + all three agent CLIs in one `npm i -g` (`@anthropic-ai/claude-code`→`claude`, `opencode-ai`→`opencode`, `@openai/codex`→`codex`, all under `/usr/local/bin`). Future language-specific images can build `FROM agent-box`.

**Consolidated config + caches via `container/config-layout.sh`** (single source of truth for the whole layout). *Everything* — every tool's config **and** the pip/npm caches — lives under one host dir, `~/.config/agent-box/`, which the launcher bind-mounts to `/root/.config/agent-box/` as its **only** mount. Run with `--symlinks` inside the image — **before** the `npm` install, so the tools (and npm itself, for its cache) write *through* the links instead of creating conflicting real dirs — it symlinks `~/.claude`, `~/.codex`, opencode's four XDG dirs, the pip/npm caches (`~/.cache/pip`, `~/.npm`), and `~/.claude.json` into that one dir. Run dirs-only by the launcher (`agent::ensure_config_sources`) it pre-creates the same subdirs + seed files on the host so the symlinks resolve on a fresh run. The script has a `dirs` list (symlinked dirs) and a `files` list (seeded with `{}` if absent — opencode rejects a present-but-invalid JSON file); it never clobbers existing config. **`~/.claude.json` fragility:** it's a *file* symlink that persists only because Claude writes it **in place**; an earlier Claude used atomic temp+rename, which replaces the symlink with a throwaway file and silently stops persisting — if that regresses, stop symlinking it here and bind-mount the file in the launcher instead.

**Launcher — single self-contained `bin/agtbox.sh`.** `getopts` parses the container flags and stops at the first non-option (the tool name); everything after is the agent's own args, forwarded verbatim. Image, the single config mount, and env are **tool-independent** — the tool name only selects the binary (`AGENT_BIN=/usr/local/bin/$TOOL`, since binary == tool name) after a `claude|opencode|codex` validation. Env is the **union** of every tool's vars, always exported (a tool ignores env it doesn't read): `ANTHROPIC_*` + `OPENAI_API_KEY`/`CODEX_API_KEY` forwarded when set; `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` + `OPENCODE_*` always set. The rest are `agent::*` functions: detect engine (prefers `ch-run`, else podman; `-t` overrides), derive host `TZ`, realpath volumes (+ dedup a path given as both `-v` and `-r` — rw wins), ensure config sources, lazily build the one image, then build the run argv and exec (`podman run … -- agent-box /usr/local/bin/<tool> <args>`, or the `ch-run … -- …` equivalent). The podman path adds `--` before the image so a `-`-leading tool arg can't be misread as a podman flag.

Adding an agent = add its `npm i -g` package to the Containerfile + an entry to `config-layout.sh`'s `dirs` array (and a `files` entry if it needs a seeded JSON config) + allow its name in the launcher's validation `case`.

**Ephemeral by design.** The container runs `--rm`; config persists via the `~/.config/agent-box/` mount. Global installs (apt/pip/npm) don't survive — keep project deps in the mounted project (`.venv`/`node_modules`; Debian's PEP 668 blocks system pip, so use a venv). The pip + npm **download caches** persist too — symlinked into the same one mount (host `~/.config/agent-box/cache/{pip,npm}/` → the containers' default cache dirs), so there's no separate cache mount.

**Charliecloud vs podman.** Podman runs `agent-box` directly. Charliecloud builds the same Containerfile via `ch-image`, converts it to dir format under `container/.charliecloud/`, and runs it unprivileged with `ch-run --write-fake --private-tmp`. Two ch-run specifics (untested in the podman-only dev env — verify on HPC): forwarded env uses `--env-no-expand` so colon/`$` values aren't path-expanded; and `ch-run` has no read-only bind option, so `-r` volumes are mounted **read-write** with a warning (the read-only guarantee holds only under podman). The single config mount is a *directory* bind (ch-run auto-creates a missing dest as a dir), so the old `~/.claude.json` file-bind — and its in-image `touch` — is gone: claude.json is now just a symlink inside that one dir.

**Custom CA certs.** Before building, the launcher copies `~/.local/share/certs/*` into `container/certs/` (gitignored); the image `COPY`s them and runs `update-ca-certificates` — needed behind TLS-intercepting proxies. Missing certs are silently skipped.

**Pointing agents at local models.** The launcher forwards the env union when set: claude reads `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_DEFAULT_SONNET_MODEL` (aim it at a local OpenAI/Anthropic-compatible server, e.g. LM Studio). codex ignores `OPENAI_BASE_URL`, so its local endpoint goes in `~/.config/agent-box/codex/config.toml` (codex's `~/.codex` is symlinked there). All config persists under `~/.config/agent-box/`.

**Host timezone.** `agent::derive_tz` (in `agtbox.sh`) derives the host IANA tz (`timedatectl` → `/etc/timezone` → the `/etc/localtime` symlink → `$TZ`) and appends `TZ=…` to the env so containers report host-local time instead of UTC; `agent-box` ships tzdata. Underivable → stays UTC.
