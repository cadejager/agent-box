# podman-ai-agents

Run terminal AI coding agents — **Claude Code**, **opencode**, and **OpenAI Codex** — inside disposable, rootless containers, behind one consistent set of launcher commands. Works with **podman** (laptops/workstations) and **Charliecloud** (unprivileged HPC).

There's no application code here — the "source" is a handful of Bash launchers plus the Containerfiles that define each agent's image.

## Why

- **Isolation.** Each agent runs in an ephemeral (`--rm`) container — the container *is* the security boundary. Anything the agent installs or breaks is gone next run.
- **One consistent CLI.** `ccc` / `occ` / `cdx` share the same flags; the differences between the underlying tools live behind a thin wrapper plus a shared library.
- **Runs where you do.** Rootless podman locally, Charliecloud on HPC, and behind TLS-intercepting corporate proxies (your CA certs are baked into the image).
- **Config travels, container doesn't.** Your host login/session dirs are bind-mounted in, so auth and history persist while the container stays disposable.

## Requirements

- **podman** (rootless) — or **Charliecloud** (`ch-run`) on HPC. The launchers auto-detect the engine; `-t` overrides.
- Nothing else to install. Local-model *serving* is out of scope — run e.g. [LM Studio](https://lmstudio.ai) separately and point an agent at it (see below).
- Host config dirs (`~/.claude`, `~/.codex`, `~/.config/opencode`, …) are created automatically if they don't exist yet.

## Quick start

```bash
git clone https://github.com/cadejager/podman-ai-agents
cd podman-ai-agents

./bin/ccc.sh        # Claude Code in the current directory
./bin/occ.sh        # opencode
./bin/cdx.sh        # OpenAI Codex
```

The first launch builds the images (a few minutes — a Node base plus a build toolchain); later launches are instant. Your current directory is mounted into the container **at the same absolute path**, so any file path the agent prints is valid on your host. The `bin/` scripts can be run from anywhere.

## Usage

All three launchers take the same flags:

| Flag | Meaning |
|------|---------|
| `-a DIR` | App directory to mount (default: current dir), at the same path inside the container |
| `-v VOL` | Extra volume to mount at the same path (repeatable) |
| `-r VOL` | Extra volume mounted **read-only** at the same path (repeatable). podman enforces it; Charliecloud has no read-only bind, so it mounts read-write and warns |
| `-t TYPE` | Engine: `podman` or `charliecloud` (default: auto-detect) |
| `-c` | Continue the most recent session |
| `-s [ID]` | Resume session `ID`; with no ID, open the agent's interactive session picker |
| `-f` | Fork instead of resume |
| `-b` | Rebuild the images |
| `-h` | Show help (incl. a per-tool pass-through cheat-sheet) |
| `-- ARGS…` | Everything after `--` is passed straight through to the underlying agent |

The unified flags map to each tool's native form (claude `--resume`, opencode `--session`, codex `resume`/`fork` subcommands). Reach anything tool-specific via `--`.

```bash
./bin/ccc.sh -a ~/src/myproject                 # run against a specific directory
./bin/ccc.sh -s                                 # resume — pick a session interactively
./bin/cdx.sh -s 0f3c1a…                          # resume a specific codex session
./bin/ccc.sh -- --model opus --effort high      # pass raw flags through to claude
./bin/occ.sh -v ~/datasets                      # mount an extra directory
./bin/occ.sh -r ~/reference-data                # mount an extra directory read-only
./bin/cdx.sh -b                                 # rebuild this agent's images
```

## How it works

- **Two-stage images.** `agents/Containerfile.base` builds `agent-base` (Node + a build toolchain + your CA certs). Each agent image is `FROM agent-base` and installs only its one CLI. Images build lazily on first launch, or on demand with `-b`.
- **Shared launcher library.** `bin/lib/agent-run.sh` handles engine detection, lazy image builds, and constructing the run command (as an argv array — no `eval`). `ccc.sh` / `occ.sh` / `cdx.sh` are thin wrappers that declare only what differs (image, config mounts, env vars, agent binary, flag mapping).
- **Same-path mounts.** Your working dir and any `-v` volumes mount at the identical absolute path inside the container, keeping file paths valid on both sides.
- **Disposable container, persistent config.** Containers run `--rm`. Your host config/login dirs are bind-mounted so sessions persist — but anything installed *globally* inside the container (apt/pip/npm) does **not** survive. Keep project dependencies in the project (a `.venv`, `node_modules`, …); those live in the mounted dir and persist across runs (a `.venv` is also how to `pip install` on the Debian-based image, which blocks system-wide installs). The pip and npm **download caches** are shared host-side under `~/.cache/podman-ai-agents/`, so global re-installs are fast (fetched once, then served from cache); that directory is safe to delete to reclaim space.

## Pointing an agent at a local model

Serve the model yourself (e.g. LM Studio), then:

- **Claude Code** — export `ANTHROPIC_BASE_URL` (and optionally `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_DEFAULT_SONNET_MODEL`); they're forwarded into the container when set.
- **Codex** — set the endpoint in `~/.codex/config.toml` (codex ignores `OPENAI_BASE_URL`), or use `./bin/cdx.sh -- --oss --local-provider lmstudio`.

## Corporate CA certs

Put your company CA certs in `~/.local/share/certs/` (override the source with `AGENT_CERTS_DIR`). The launchers copy them into the image build and run `update-ca-certificates`, so the agents work behind TLS-intercepting proxies. Certs are baked at build time — after rotating them, rebuild with `-b`.

## Layout

```
bin/
  ccc.sh occ.sh cdx.sh    # thin per-agent launchers
  lib/agent-run.sh        # shared engine-detect / build / run logic
agents/
  Containerfile.base      # agent-base (toolchain + certs)
  Containerfile.claude-code | .opencode | .codex
CLAUDE.md                 # terse architecture reference (for AI assistants)
```

## Notes & caveats

- **Charliecloud is implemented but verify on your own HPC host** — the primary dev environment is podman-only.
- Containers run as **root** and mount your working tree read-write plus your host credentials; this is intentional (the container is the trust boundary). Don't point a launcher at code or images you don't trust.
- See [`CLAUDE.md`](CLAUDE.md) for the architecture-level reference.

## License

[GNU AGPL v3](LICENSE).
