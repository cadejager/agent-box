# Agent Box

Run terminal AI coding agents — **Claude Code**, **opencode**, and **OpenAI Codex** — inside one disposable, rootless container, behind one small launcher. Works with **podman** (laptops/workstations) and **Charliecloud** (unprivileged HPC).

There's no application code here — the "source" is one Bash launcher (`agtbox.sh`) plus a single Containerfile.

> The GitHub repo is still named `podman-ai-agents` for now (it'll be renamed to `agent-box` later); the clone command below reflects that.

## Why

- **Isolation.** The agent runs in an ephemeral (`--rm`) container — the container *is* the security boundary. Anything it installs or breaks is gone next run.
- **One launcher, one image, no obfuscation.** A single `agtbox.sh` runs any of the three agents from one `agent-box` image, and you pass each tool's *own* flags straight through — nothing new to learn or quote around.
- **Runs where you do.** Rootless podman locally, Charliecloud on HPC, and behind TLS-intercepting corporate proxies (your CA certs are baked into the image).
- **Config travels, container doesn't.** All your agent config lives in one host dir (`~/.config/agent-box/`), bind-mounted in, so auth and history persist while the container stays disposable.

## Requirements

- **podman** (rootless) — or **Charliecloud** (`ch-run`) on HPC. The launcher auto-detects the engine; `-t` overrides.
- **bash ≥ 4** for the launcher itself. Nothing else to install — local-model *serving* is out of scope (run e.g. [LM Studio](https://lmstudio.ai) separately and point an agent at it; see below).
- The host config dir `~/.config/agent-box/` is created automatically on first run.

## Quick start

```bash
git clone https://github.com/cadejager/podman-ai-agents
cd podman-ai-agents

./bin/agtbox.sh claude        # Claude Code in the current directory
./bin/agtbox.sh opencode      # opencode
./bin/agtbox.sh codex         # OpenAI Codex
```

The first launch builds the one image (a few minutes — a Node base, a build toolchain, and all three CLIs); later launches are instant. Your current directory is mounted into the container **at the same absolute path**, so any file path the agent prints is valid on your host. `bin/agtbox.sh` can be run from anywhere.

## Usage

```
agtbox.sh [container flags] <claude|opencode|codex> [tool args…]
```

Container flags come **before** the tool name; **everything after the tool name is passed to the agent verbatim** — use the tool's own flags. Run `agtbox.sh <tool> --help` for a given tool's own help.

| Container flag | Meaning |
|------|---------|
| `-a DIR` | App directory to mount (default: current dir), at the same path inside the container |
| `-v VOL` | Extra volume to mount at the same path (repeatable) |
| `-r VOL` | Extra volume mounted **read-only** at the same path (repeatable). podman enforces it; Charliecloud has no read-only bind, so it mounts read-write and warns |
| `-t TYPE` | Engine: `podman` or `charliecloud` (default: auto-detect) |
| `-b` | Rebuild the image |
| `-h` | Show help (incl. a per-tool session cheat-sheet) |

```bash
./bin/agtbox.sh -a ~/src/myproject claude          # run against a specific directory
./bin/agtbox.sh claude --resume                    # resume — pick a claude session interactively
./bin/agtbox.sh codex resume 0f3c1a…               # resume a specific codex session
./bin/agtbox.sh claude --model opus --effort high  # the tool's own flags, straight through
./bin/agtbox.sh -v ~/datasets opencode             # mount an extra directory (read-write)
./bin/agtbox.sh -r ~/reference-data opencode       # mount an extra directory read-only
./bin/agtbox.sh -b codex                           # rebuild the image
```

Session handling is just each tool's native syntax: claude `--continue` / `--resume [ID]` / `--fork-session`; opencode `--continue` / `--session ID` / `--fork`; codex `resume [ID]` / `fork [ID]`.

## How it works

- **One image.** `container/Containerfile` builds `agent-box` (Node + a build toolchain + common CLI tools + your CA certs + all three agent CLIs). Built lazily on first launch, or on demand with `-b`.
- **One self-contained launcher.** `bin/agtbox.sh` handles engine detection, the lazy image build, and constructing the run command (as an argv array — no `eval`). The tool name just selects the binary (`/usr/local/bin/<tool>`); image, env, and config mounts are the same for every agent.
- **Same-path mounts.** Your working dir and any `-v` / `-r` volumes mount at the identical absolute path inside the container, keeping file paths valid on both sides.
- **Consolidated config.** Every tool's config lives under one host dir, `~/.config/agent-box/`, bind-mounted in; inside the image, symlinks point each tool's expected path (`~/.claude`, `~/.codex`, opencode's XDG dirs) into it (see `container/config-layout.sh`, the single source of truth for that layout). One dir to back up or wipe.
- **Disposable container, persistent config.** The container runs `--rm`; your config persists via that mount. Anything installed *globally* inside (apt/pip/npm) does **not** survive — keep project deps in the project (a `.venv`, `node_modules`, …; a `.venv` is also how to `pip install` on the Debian image, which blocks system-wide installs). The pip/npm **download caches** are shared host-side under `~/.cache/agent-box/`, so global re-installs are fast (and that dir is safe to delete).
- **Host-local time.** The launcher derives your host timezone and forwards it (`TZ`), so the agent's clock matches the host instead of defaulting to UTC.

## Pointing an agent at a local model

Serve the model yourself (e.g. LM Studio), then (these env vars are forwarded into the container when set):

- **Claude Code** — export `ANTHROPIC_BASE_URL` (and optionally `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_DEFAULT_SONNET_MODEL`).
- **Codex** — set the endpoint in `~/.config/agent-box/codex/config.toml` (codex ignores `OPENAI_BASE_URL`), or `./bin/agtbox.sh codex --oss --local-provider lmstudio`.

## Corporate CA certs

Put your company CA certs in `~/.local/share/certs/` (override the source with `AGENT_CERTS_DIR`). The launcher copies them into the image build and runs `update-ca-certificates`, so the agents work behind TLS-intercepting proxies. Certs are baked at build time — after rotating them, rebuild with `-b`.

## Layout

```
bin/
  agtbox.sh               # the single launcher (engine-detect / build / run)
container/
  Containerfile           # the one agent-box image (toolchain + 3 CLIs + config symlinks)
  config-layout.sh        # consolidated-config layout, shared by the image build + launcher
test/
  argv.sh                 # stub-engine argv tests for agtbox.sh
CLAUDE.md                 # terse architecture reference (for AI assistants)
```

## Notes & caveats

- **Charliecloud is implemented but verify on your own HPC host** — the primary dev environment is podman-only.
- The container runs as **root**, mounts your working tree read-write, and mounts your whole `~/.config/agent-box/` (all three tools' config, incl. credentials) on every run — intentional (the container is the trust boundary). Don't point the launcher at code or images you don't trust.
- **Upgrading from the old per-tool launchers?** Reclaim the now-unused images/caches: `podman image rm agent-base claude-code opencode codex` and `rm -rf ~/.cache/podman-ai-agents`. Agent config now lives in `~/.config/agent-box/` (first run starts fresh / re-login).
- See [`CLAUDE.md`](CLAUDE.md) for the architecture-level reference.

## License

[GNU AGPL v3](LICENSE).
