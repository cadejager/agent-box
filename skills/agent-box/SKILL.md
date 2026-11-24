---
name: agent-box
description: Read this before saying a tool, CLI, library or language version is missing, and whenever you hit "command not found", "No module named", pip "externally-managed" or read-only errors, or need a newer Python/Node, a linter/formatter/build tool (cmake, ruff, prettier, tsc, shellcheck…), or GitHub/GitLab access. You are in an Agent Box sandbox (PATH starts with ~/.local/share/agent-box): no root, but uv/uvx/npm/npx can install almost anything without asking the user.
---

# Agent Box sandbox

You are in an unprivileged sandbox. The system (`/usr`, `/etc`, …) is **read-only** and `$HOME` and `/tmp` are **private tmpfs** that vanish when the session ends. There is no root, `sudo` or `apt`. You can still get nearly any tool with the per-user toolchain below, so don't tell the user something is unavailable until you've tried it.

## Before saying a tool is missing, try in this order
1. `command -v X`
2. Run it once, no install: `uvx X` / `uvx --from PKG X`, or `npx -y X` / `npx -y -p PKG X`
3. Install it only if you'll reuse it: `uv tool install PKG` / `npm i -g PKG`
4. Only then report failure — with the exact error and the host that failed.

Examples that work: `uvx ruff check .`, `uvx cmake --version`, `uvx --from shellcheck-py shellcheck f.sh`, `npx -y prettier@3 --check .`, `npx -y -p typescript tsc --noEmit`, `npx -y node@20 script.js` (a different Node version).

## What's on PATH
| Tool | Notes |
|------|-------|
| `node`, `npm`, `npx` | Current LTS Node. Need another version? `npx -y node@20 …` |
| `uv`, `uvx` | Python packages, venvs and **Python versions** |
| `gh`, `glab` | GitHub / GitLab CLIs |
| `git`, `python3`, `curl`, compilers, `jq`, … | Come from the host (bwrap) or a Debian image (podman), so they **vary** — `command -v X` first. Host `python3` can be old (3.6 on some hosts) and may lack pip/venv; use uv. If `rg` is missing, use `git grep` / `grep -rn` |

Network is available. Proxy settings are forwarded only if the user's host had them set.

## Python: prefer uv
- Project: `uv sync` (uv project), or `uv venv && uv pip install -r requirements.txt`
- One script: `uv run --with requests script.py`, or store deps in the script: `uv add --script script.py requests && uv run script.py`
- Other Python versions: `uv python install 3.12`, then `uv run --python 3.12 …`, `uv venv --python 3.12`, or just `python3.12` (it lands on PATH). `uv python list` shows what's available.
- `python -m venv` + `pip` works where the host's Python has pip/venv, but it may not (esp. podman) — uv always works. A bare `pip install` is a persistent user install and only affects the Python that pip belongs to (`pip --version`).
- Node projects: plain `npm install` (`node_modules` stays in the project).

## What persists
- **Persists:** the project directory (and any extra dirs the user mounted), plus global installs — `npm i -g`, `uv tool install`, `uv python install` — and download caches.
- **Does not persist:** everything else — `$HOME`, `/tmp`, system paths. The user can't see files you put there. Put anything they should see in the project dir.
- Background processes (dev servers, watchers) die when the session ends.
- If a `.venv` breaks (`bad interpreter`, `Exec format error`): run `uv sync` in a uv project, otherwise `uv venv --clear` and reinstall. (Typical cause: it was built on the host or another CPU arch.)
- Global installs are **shared by every project and agent** that uses this toolchain. Pin versions (`uvx ruff@0.6.9`, `prettier@3`), prefer `uvx`/`npx` or a project venv for anything version-sensitive, and never upgrade or uninstall `npm`, `node`, `uv` or the agent CLIs themselves. `npx -y` runs whatever it downloads, so stick to well-known packages.

## Git, GitHub, GitLab
- `gh` / `glab` use config saved by Agent Box (not the user's host config; host `GH_TOKEN`/`GITLAB_TOKEN` are not forwarded). Git over SSH uses the mounted `~/.ssh`. Check with `gh auth status` / `glab auth status`.
- If a token is invalid or expired you can't fix it. Ask the user to run `gh auth login` / `glab auth login` in an Agent Box shell (`agtbox.py bash`).
- Prefer `gh` / `glab` (`gh pr create`, `gh api`, `glab mr create`, `glab api`) over scraping web pages.

## Gotchas
- "could not resolve host" or TLS errors usually mean the network or proxy blocks that host — report the host rather than retrying repeatedly.
- Behind a TLS-intercepting proxy `curl`/git may work while tools with their own CA bundles fail: uv → `UV_SYSTEM_CERTS=1` (older uv: `UV_NATIVE_TLS=1`); node/npm → `NODE_OPTIONS=--use-openssl-ca`.
- Servers you start on `127.0.0.1:PORT` are reachable from the host under bwrap, but not under podman (no published ports).
