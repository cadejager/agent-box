# Agent Box

Run terminal AI coding agents — **Claude Code**, **opencode**, and **OpenAI Codex** — inside an unprivileged **[bubblewrap](https://github.com/containers/bubblewrap)** sandbox, behind one small launcher. No container image, no daemon, no root — the agents run against your host's own tools, walled off from the rest of your machine.

There's no application code here — the "source" is a single Bash launcher, `bin/agtbox.sh`.

## Why

- **Isolation without a container image.** Each agent runs in a fresh bubblewrap sandbox: your real `$HOME` is replaced by an empty tmpfs, the whole filesystem is read-only except a handful of dirs, and the agent can only persist to the project and to `~/.{config,cache,local/share}/agent-box`. A prompt-injected agent can't read your dotfiles/keys or scribble on your system.
- **Uses what you already have.** System packages (python, git, ripgrep, gcc, …) come straight from your host. Only the bits that aren't system-wide — node, `uv`, and the three agent CLIs — are installed into a per-user toolchain on first run. No multi-GB image to build or store.
- **One launcher, no obfuscation.** A single `agtbox.sh` runs any of the three agents and passes each tool's *own* flags straight through — nothing new to learn or quote around.
- **Config and tools persist; the sandbox doesn't.** Your agent config lives in `~/.config/agent-box`, the toolchain + anything an agent installs globally lives in `~/.local/share/agent-box`, and download caches in `~/.cache/agent-box` — all bound in, so logins, history, and installed tools survive while each run starts from a clean sandbox.

## Requirements

- **bubblewrap** (`bwrap`) and **unprivileged user namespaces** (the default on most modern Linux; works without root or setuid). On Debian/Ubuntu: `apt install bubblewrap`.
- **bash ≥ 4**, plus the host tools the agents lean on (`python3`, `git`, `curl`, `tar` are used during setup; `ripgrep`/`jq`/compilers as your projects need them).
- Everything else — node, `uv`, and the agent CLIs — is **installed automatically on first run** into `~/.local/share/agent-box`. Local-model *serving* is out of scope (run e.g. [LM Studio](https://lmstudio.ai) separately and point an agent at it; see below).

## Quick start

```bash
git clone https://github.com/cadejager/agent-box
cd agent-box

./bin/agtbox.sh claude        # Claude Code in the current directory
./bin/agtbox.sh opencode      # opencode
./bin/agtbox.sh codex         # OpenAI Codex
```

The **first** launch installs the toolchain (node + `uv` + the three CLIs) into `~/.local/share/agent-box` — a one-time download, done inside a sandbox so the installers can't touch your host. Later launches start instantly. Your current directory is bound into the sandbox **at the same absolute path**, so any path the agent prints is valid on your host. `bin/agtbox.sh` can be run from anywhere.

## Usage

```
agtbox.sh [flags] <claude|opencode|codex> [tool args…]
```

Flags come **before** the tool name; **everything after the tool name is passed to the agent verbatim** — use the tool's own flags. Run `agtbox.sh <tool> --help` for a given tool's own help.

| Flag | Meaning |
|------|---------|
| `-a DIR` | Project directory, bound at the same path inside (default: current dir) |
| `-v VOL` | Extra dir, bound **read-write** at the same path (repeatable) |
| `-r VOL` | Extra dir, bound **read-only** at the same path (repeatable) |
| `-h` | Show help |

```bash
./bin/agtbox.sh -a ~/src/myproject claude          # run against a specific directory
./bin/agtbox.sh claude --resume                    # resume — pick a claude session interactively
./bin/agtbox.sh codex resume 0f3c1a…               # resume a specific codex session
./bin/agtbox.sh claude --model opus --effort high  # the tool's own flags, straight through
./bin/agtbox.sh -v ~/datasets opencode             # bind an extra directory (read-write)
./bin/agtbox.sh -r ~/reference-data opencode       # bind an extra directory read-only
```

Session handling is just each tool's native syntax: claude `--continue` / `--resume [ID]` / `--fork-session`; opencode `--continue` / `--session ID` / `--fork`; codex `resume [ID]` / `fork [ID]`.

`AGTBOX_REINSTALL=1 ./bin/agtbox.sh claude` reinstalls the toolchain (or just `rm -rf ~/.local/share/agent-box` and relaunch).

## How it works

- **One self-contained launcher.** `bin/agtbox.sh` constructs a `bwrap …` command (as an argv array — no `eval`) and execs it. The tool name only selects the binary; the sandbox, env, and config wiring are identical for every agent.
- **Locked-down sandbox.** `/usr`, `/bin`, `/lib`, `/etc`, … are bound **read-only**; `$HOME` and `/tmp` are fresh tmpfs; networking is shared (the agents reach their APIs); and only these are writable, each bound at its real path: your **project**, `~/.config/agent-box`, `~/.cache/agent-box`, `~/.local/share/agent-box`. Nothing else on the host is even visible.
- **Persistent toolchain + global installs.** `~/.local/share/agent-box` holds node, `uv`, the CLIs, and anything an agent installs globally (`npm -g`, `pip`, `uv tool` — outside a venv). It persists across runs and is shared between tools, so installs stick around and the CLIs can auto-update. Project dependencies still belong in the project (a `.venv`, `node_modules`, …).
- **Config, wired straight in.** Each tool's config is bound from `~/.config/agent-box/<tool>` onto the path the tool expects (`~/.claude`, `~/.codex`, opencode's XDG dirs, `~/.claude.json`) — direct binds, no symlinks. One dir to back up or wipe.
- **Download caches** (npm/pip/uv) live in `~/.cache/agent-box` and persist, so re-installs are fast.
- **Host-local time.** The launcher derives your host timezone and sets `TZ` so the agent's clock matches the host.

## Pointing an agent at a local model

Serve the model yourself (e.g. LM Studio), then (these env vars are forwarded into the sandbox when set):

- **Claude Code** — export `ANTHROPIC_BASE_URL` (and optionally `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_DEFAULT_SONNET_MODEL`).
- **Codex** — set the endpoint in `~/.config/agent-box/codex/config.toml` (codex ignores `OPENAI_BASE_URL`), or `./bin/agtbox.sh codex --oss --local-provider lmstudio`.

## Corporate CA certs

The sandbox binds your host's `/etc` read-only, so it inherits the **system trust store** automatically. Behind a TLS-intercepting proxy, install your CA into the host once (`/usr/local/share/ca-certificates/your-ca.crt` then `sudo update-ca-certificates`) and the agents will trust it — nothing to rebuild.

## Layout

```
bin/
  agtbox.sh               # the single launcher (sandbox construction + first-run install)
test/
  argv.sh                 # stub-bwrap argv tests for agtbox.sh
CLAUDE.md                 # terse architecture reference (for AI assistants)
```

## Notes & caveats

- **The agent can persist only to four places:** the project, `~/.config/agent-box`, `~/.cache/agent-box`, `~/.local/share/agent-box`. Everything else is read-only or an ephemeral tmpfs. Global installs persist (and are shared between runs) by design.
- **Concurrent runs share** the config/cache/toolchain dirs (so tools see each other's installs); simultaneous installs of the same package could race.
- **Coming from the old podman/Charliecloud image?** Reclaim it: `podman image rm agent-box` and `rm -rf ~/.cache/podman-ai-agents`. Config in `~/.config/agent-box` carries over.
- See [`CLAUDE.md`](CLAUDE.md) for the architecture-level reference.

## License

[GNU AGPL v3](LICENSE).
