# Agent Box

Run terminal AI coding agents — **Claude Code**, **opencode**, and **OpenAI Codex** — inside an unprivileged sandbox, behind one small launcher. On Linux it uses **[bubblewrap](https://github.com/containers/bubblewrap)** over your host's own tools; on macOS (or any host without bwrap) it uses **podman** over a slim Linux image. Either way the agents are walled off from the rest of your machine, and the same per-user toolchain is shared between both.

There's no application code here — the "source" is a single Python launcher, `bin/agtbox.py`.

## Why

- **Isolation.** Each agent runs in a fresh sandbox: under bwrap your real `$HOME` is replaced by an empty tmpfs and the whole filesystem is read-only except a handful of dirs; under podman the rootfs is a throwaway image and the host filesystem isn't visible at all except the same handful of dirs. Either way the agent can only persist to the project and to `~/.{config,cache,local/share,local/state}/agent-box`. A prompt-injected agent can't read your dotfiles/keys or scribble on your system.
- **Uses what you already have.** System packages (python, git, ripgrep, gcc, …) come straight from your host. Only the bits that aren't system-wide — node, `uv`, the three agent CLIs, and the GitHub/GitLab CLIs (`gh`/`glab`) — are installed into a per-user toolchain on first run.
- **One launcher, no obfuscation.** A single `agtbox.py` runs any of the three agents and passes each tool's *own* flags straight through — nothing new to learn or quote around.
- **Config and tools persist; the sandbox doesn't.** Your agent config lives in `~/.config/agent-box`, the toolchain + anything an agent installs globally lives in `~/.local/share/agent-box`, and download caches in `~/.cache/agent-box` — all bound in, so logins, history, and installed tools survive while each run starts from a clean sandbox.

## Requirements

- **An engine:** either **bubblewrap** (`bwrap`) + **unprivileged user namespaces** — the default on most modern Linux, no root/setuid (Debian/Ubuntu: `apt install bubblewrap`) — **or podman** (used automatically when bwrap is absent, e.g. on **macOS**: `brew install podman && podman machine init && podman machine start`). The launcher prefers bwrap when present; force one with `-t podman|bwrap`.
- **python3** — the launcher itself is a Python 3 script (stdlib only, no third-party packages). Under bwrap, the host also supplies the tools the agents lean on (`git`, `curl`, `tar` during setup; `ripgrep`/`jq`/compilers as projects need them); under podman those come from the image, so the host needs only podman + python3.
- Everything else — node, `uv`, and the agent CLIs — is **installed automatically on first run** into `~/.local/share/agent-box` (the same toolchain for both engines). Local-model *serving* is out of scope (run e.g. [LM Studio](https://lmstudio.ai) separately and point an agent at it; see below).

## Quick start

```bash
git clone https://github.com/cadejager/agent-box
cd agent-box

./bin/agtbox.py claude        # Claude Code in the current directory
./bin/agtbox.py opencode      # opencode
./bin/agtbox.py codex         # OpenAI Codex
```

The **first** launch installs the toolchain (node + `uv` + the three CLIs + `gh`/`glab`) into `~/.local/share/agent-box` — a one-time download, done inside the sandbox so the installers can't touch your host (under podman it also builds the image first). Later launches start instantly. Your current directory is bound into the sandbox **at the same absolute path**, so any path the agent prints is valid on your host. `bin/agtbox.py` can be run from anywhere.

## Usage

```
agtbox.py [flags] <claude|opencode|codex> [tool args…]
```

Flags come **before** the tool name; **everything after the tool name is passed to the agent verbatim** — use the tool's own flags. Run `agtbox.py <tool> --help` for a given tool's own help.

| Flag | Meaning |
|------|---------|
| `-a DIR` | Project directory, bound at the same path inside (default: current dir) |
| `-v VOL` | Extra dir, bound **read-write** at the same path (repeatable) |
| `-r VOL` | Extra dir, bound **read-only** at the same path (repeatable) |
| `-t ENG` | Engine: `podman` or `bwrap` (default: auto — bwrap on Linux, else podman) |
| `-b` | Rebuild the podman image (podman engine only) |
| `-h` | Show help |

```bash
./bin/agtbox.py -a ~/src/myproject claude          # run against a specific directory
./bin/agtbox.py claude --resume                    # resume — pick a claude session interactively
./bin/agtbox.py codex resume 0f3c1a…               # resume a specific codex session
./bin/agtbox.py claude --model opus --effort high  # the tool's own flags, straight through
./bin/agtbox.py -v ~/datasets opencode             # bind an extra directory (read-write)
./bin/agtbox.py -r ~/reference-data opencode       # bind an extra directory read-only
```

Session handling is just each tool's native syntax: claude `--continue` / `--resume [ID]` / `--fork-session`; opencode `--continue` / `--session ID` / `--fork`; codex `resume [ID]` / `fork [ID]`.

`AGTBOX_REINSTALL=1 ./bin/agtbox.py claude` reinstalls the toolchain in place (it leaves your config and opencode's data alone).

## How it works

- **One self-contained launcher, two engines.** `bin/agtbox.py` constructs a `bwrap …` (or `podman run …`) command as an argv array — no `eval` — and execs it. The tool name only selects the binary; the env and config wiring are identical for every agent and (by design) nearly identical between the two engines — everything is bound at the **same host path**, so the bind tables are shared.
- **Locked-down sandbox (bwrap).** `/usr`, `/bin`, `/lib`, `/etc`, … are bound **read-only**; `$HOME` and `/tmp` are fresh tmpfs; networking is shared (the agents reach their APIs); and only these are writable, each bound at its real path: your **project**, `~/.config/agent-box`, `~/.local/share/agent-box`, `~/.local/state/agent-box`, `~/.cache/agent-box`. Nothing else on the host is even visible.
- **Container sandbox (podman).** When bwrap isn't available, the same dirs are bind-mounted (`-v`, with `:ro` for `-r`) at the same paths into a throwaway container built from `container/Containerfile` (`debian:trixie` + git/ripgrep/openssh-client/build-essential/… — node/uv/the CLIs stay in the bind-mounted toolchain, so an image rebuild never touches them). It runs as the container's root, which rootless podman maps to your host user, so the files the agent writes stay owned by you; networking is shared. The image auto-builds on first use; `-b` rebuilds it.
- **Persistent toolchain + global installs.** `~/.local/share/agent-box` holds node, `uv`, the agent CLIs, the `gh`/`glab` git-hosting CLIs, and anything an agent installs globally (`npm -g`, `pip`, `uv tool` — outside a venv). It persists across runs and is shared between tools, so installs stick around and the CLIs can auto-update. Project dependencies still belong in the project (a `.venv`, `node_modules`, …).
- **Config, wired straight in.** Each tool's config is bound from `~/.config/agent-box/<tool>` onto the path the tool expects (`~/.claude`, `~/.codex`, opencode's XDG dirs, `~/.claude.json`, `gh`/`glab`/`git` config so logins and git identity persist, and `~/.ssh` so SSH keys/`known_hosts` are available for git push). Drop your SSH key into `~/.config/agent-box/ssh/`. One dir to back up or wipe.
- **Download caches** (npm/pip/uv) live in `~/.cache/agent-box` and persist, so re-installs are fast.

## Pointing an agent at a local model

Serve the model yourself (e.g. LM Studio), then (these env vars are forwarded into the sandbox when set):

- **Claude Code** — export `ANTHROPIC_BASE_URL` (and optionally `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_DEFAULT_SONNET_MODEL`).
- **Codex** — set the endpoint in `~/.config/agent-box/codex/config.toml` (codex ignores `OPENAI_BASE_URL`), or `./bin/agtbox.py codex --oss --local-provider lmstudio`.

## Layout

```
bin/
  agtbox.py               # the single launcher (sandbox construction + first-run install)
container/
  Containerfile           # the podman engine's image (debian:trixie + runtime/build packages)
test/
  test_agtbox.py          # unittest suite: stub-engine integration + unit tests
.github/workflows/
  lint.yml                # CI: ruff + the Python test suite
CLAUDE.md                 # terse architecture reference (for AI assistants)
```

## Notes & caveats

- **The agent can persist only to:** the project and the four `agent-box` dirs (`~/.config`, `~/.local/share`, `~/.local/state`, `~/.cache`). Everything else is read-only or an ephemeral tmpfs; global installs persist (and are shared between runs) by design.
- **What stays *readable*:** `/usr` and `/etc` are bound read-only (needed for the toolchain, DNS, and the CA trust store), so system files there — including things like `/etc/passwd` and host config — are visible to the agent, just not writable. Your home directory, SSH/cloud keys, and the rest of the filesystem are hidden entirely (`$HOME` is an empty tmpfs).
- **Concurrent runs share** the config/cache/toolchain dirs (so tools see each other's installs); simultaneous installs of the same package could race.
- **Shared network.** The agent reaches the network like the host does — including services on `127.0.0.1` and cloud metadata endpoints. That's required for it to reach its APIs, but don't bind sensitive dev services to loopback while an agent is running.
- **Recommended host hardening (TTY injection).** A sandboxed process sharing your terminal can use the legacy `TIOCSTI` ioctl to push keystrokes into your shell — commands that would run *after* the agent exits. Close it once on the host (it's the kernel default on some distros but not all): `echo 'dev.tty.legacy_tiocsti=0' | sudo tee /etc/sysctl.d/99-agtbox.conf && sudo sysctl --system`. This doesn't affect clipboard copy/paste (that's OSC 52 / `xclip`-style, unrelated to `TIOCSTI`).
- **macOS / podman.** podman runs a Linux VM that shares your `$HOME`, so the `agent-box` dirs and your project must live **under `$HOME`**.
- **Behind a corporate proxy?** If a TLS-intercepting proxy sits between you and the internet, the **podman** engine bakes your company's CA certs into the image at build time: drop them (PEM, `.crt` extension) into `~/.local/share/certs/` (or set `AGENT_CERTS_DIR`) and they're trusted inside the container. The **bwrap** engine needs nothing — it reuses your host's `/etc` trust store, so whatever your host already trusts, the agents trust too.
- **Coming from the old build?** The old image-based build (and its Charliecloud path) is gone; podman is back as the macOS engine but now uses a slim `agent-box` image built on demand. Config in `~/.config/agent-box` carries over; `podman image rm agent-box` forces a fresh image build next run.
- See [`CLAUDE.md`](CLAUDE.md) for the architecture-level reference.

## License

[GNU AGPL v3](LICENSE).
