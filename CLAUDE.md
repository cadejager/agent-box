# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Agent Box: a single Bash launcher that runs AI coding agents (Claude Code, opencode, codex) inside an unprivileged **bubblewrap (`bwrap`)** sandbox. No container image, no engine, no root. The only "source" is `bin/agtbox.sh`; `test/argv.sh` is a stub-`bwrap` argv test. (Human-facing setup/usage live in `README.md`; this file is the terse architecture reference.)

## Common commands

```bash
./bin/agtbox.sh [-a DIR] [-v VOL] [-r VOL] [-h] claude|opencode|codex [tool args...]
```

- Flags (`-a`/`-v`/`-r`/`-h`) come **before** the tool name; everything after it is passed to the agent **verbatim** (no flag remapping) — session handling is each tool's own syntax (claude `--continue`/`--resume [ID]`/`--fork-session`; opencode `--continue`/`--session ID`/`--fork`; codex `resume [ID]`/`fork [ID]`). `-v` = read-write bind, `-r` = read-only bind, both at the same host path.
- The toolchain auto-installs into `~/.local/share/agent-box` on first run (`AGTBOX_REINSTALL=1` forces a reinstall). Argv-construction test: `./test/argv.sh`.

## Architecture

**No image — a bubblewrap sandbox over host tools + a per-user toolchain.** System packages (python, git, ripgrep, compilers, …) come from the host's `/usr`; only node, `uv`, and the three CLIs are installed into `~/.local/share/agent-box`. Requires `bwrap` + unprivileged user namespaces (validated on Debian 13 / aarch64).

**Launcher — single self-contained `bin/agtbox.sh`.** `getopts` parses the flags and stops at the tool name; the rest is the agent's own args, forwarded verbatim. The tool name only selects the binary (`AGENT_BIN="${AGENT_TOOLS}/bin/${TOOL}"`) after a `claude|opencode|codex` validation. Functions: `normalize_paths` (realpath volumes + dedup a path given as both `-v` and `-r`, rw wins), `derive_tz` (host IANA tz → `TZ`), `ensure_sources` (host-side bind sources), `ensure_tools`/`install_tools` (first-run setup), `bwrap_common` (shared sandbox args), `run_bwrap` (build argv + exec), `launch` (orchestrates).

**The sandbox (`bwrap_common` + `run_bwrap`).** Locked-down: `--ro-bind` each of `/usr /bin /sbin /lib /etc` (+ `--ro-bind-try /lib64`, absent on aarch64) read-only; `--dev /dev --proc /proc --tmpfs /tmp`; `--tmpfs "${HOME}"` (empty ephemeral home); read-write `--bind` for the three persistent dirs (`~/.local/share/agent-box`, `~/.cache/agent-box`) + the project (`APP_DIR`, same path) + the per-tool config binds; `--die-with-parent --unshare-pid/ipc/uts`. Network is **shared** (no `--unshare-net` — agents need their APIs). A standalone `--` ends bwrap's option parsing so a `-`-leading tool arg can't be misread. Built as an argv array — no `eval`.

**Three writable persistent dirs (everything else is read-only or ephemeral tmpfs).**
- `~/.local/share/agent-box` (`AGENT_TOOLS`): node + npm + `uv` + the CLIs + **all global installs** the agents make (`npm -g`, `pip`, `uv tool` — anything not in a venv). Bound rw, persists across runs, shared between tools, so installs stick and CLIs auto-update. Env routes installs here (`npm_config_prefix`, `PIP_PREFIX`/`PYTHONUSERBASE` + `PIP_BREAK_SYSTEM_PACKAGES=1`, `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`) and caches to `~/.cache/agent-box` (`npm_config_cache`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`). `PATH` = `${AGENT_TOOLS}/bin:${AGENT_TOOLS}/node/bin:/usr/bin:/bin`.
- `~/.config/agent-box` (`AGENT_CONFIG`): per-tool config, wired into the sandbox by the `CONFIG_DIRS`/`CONFIG_FILES` tables — each `subdir:homepath` is bound straight onto the tool's expected path (`~/.claude`, `~/.codex`, opencode's XDG dirs). **No symlinks anywhere.** `~/.claude.json` is a robust **file bind**; `ensure_sources` seeds missing JSON files with `{}` (opencode rejects a present-but-invalid file).
- `~/.cache/agent-box` (`AGENT_CACHE`): npm/pip/uv download caches.

**Env union.** `ENV_FORWARD` (`ANTHROPIC_*`/`OPENAI_API_KEY`/`CODEX_API_KEY`) forwarded when set; `ENV_LITERAL` (`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` + `OPENCODE_*`) always; `derive_tz` appends `TZ`. All emitted as `--setenv NAME VALUE`. Tool-independent — a tool ignores env it doesn't read.

**Auto-install on first run (`ensure_tools` → `install_tools`).** If the `${AGENT_TOOLS}/.stamp` completion marker (written only after a fully successful install) or the requested `${AGENT_BIN}` is missing (or `AGTBOX_REINSTALL=1`), install runs **inside a bwrap sandbox** (the toolchain + cache dirs rw, system ro, `$HOME` tmpfs) so the official installers can't touch the host: resolve the **current LTS** node from `nodejs.org/dist/index.json` (newest release with a truthy `lts`, via `python3`; arch from `uname -m`) and download that tarball into `${AGENT_TOOLS}/node` — no pinned version, so a reinstall tracks upstream's latest LTS — then `npm install -g --prefix ${AGENT_TOOLS}` the `NPM_PKGS`, then the `uv` installer (`UV_INSTALL_DIR=${AGENT_TOOLS}/bin`). Idempotent (re-run upgrades). Needs host `curl`/`tar`/`xz`/`python3`.

Adding an agent = add its npm package to `NPM_PKGS` + a `CONFIG_DIRS`/`CONFIG_FILES` entry for any config path it needs + allow its name in the validation `case`.

**Custom CA certs.** No baking — the sandbox binds host `/etc` read-only, inheriting the system trust store. Behind a TLS-intercepting proxy, install the CA into the **host** trust store once (`/usr/local/share/ca-certificates/*.crt` + `update-ca-certificates`).

**Pointing agents at local models.** The launcher forwards the env union when set: claude reads `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_DEFAULT_SONNET_MODEL` (e.g. LM Studio). codex ignores `OPENAI_BASE_URL`, so its local endpoint goes in `~/.config/agent-box/codex/config.toml`.

**Isolation model.** The agent can persist only to the project + the three `agent-box` dirs; real `$HOME` is an empty tmpfs and `/` is read-only, so it can't read the user's dotfiles/keys or write elsewhere. Global installs persist + are shared across runs by design.
