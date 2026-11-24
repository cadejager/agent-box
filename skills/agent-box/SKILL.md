---
name: agent-box
description: Use when running inside an Agent Box sandbox (env AGENT=1, empty tmpfs $HOME, read-only system) and you need a tool, language runtime, CLI or library that isn't installed — or need to know what is available (node/npm/npx, uv/uvx, managed Python versions, gh, glab), where installs persist, or how to work with git/GitHub/GitLab here.
---

# Agent Box sandbox: what you have

You are in an unprivileged sandbox. The system (`/usr`, `/etc`, …) is **read-only** and `$HOME` is an **empty tmpfs** that disappears when the session ends. You can still install almost anything you need **without root** using the per-user toolchain below. Do not tell the user a tool is "unavailable" or ask them to install it until you have tried these.

## Toolchain (already on PATH)

| Tool | Use |
|------|-----|
| `node`, `npm`, `npx` | JavaScript runtime and packages |
| `uv`, `uvx` | Python packages, venvs, and **Python versions** |
| `gh` | GitHub CLI |
| `glab` | GitLab CLI |
| `git`, `python3`, `curl`, `tar`, compilers | From the host. Host `python3` may be **old** (e.g. 3.6) — use uv for a modern one |

Network access is available (HTTP(S) via the host's proxy settings if any).

## Getting more software

**One-off CLI tools — no install step**
- Python: `uvx ruff check .`, `uvx --from httpie http GET example.com`, `uvx black --version`
- Node: `npx --yes prettier --check .`, `npx --yes cowsay@1 hi`

**Persistent global tools** (survive across sessions, shared by all projects)
- `npm install -g <pkg>` → binary lands on PATH
- `uv tool install <pkg>` → binary lands on PATH (e.g. `uv tool install httpie`)
- `pip install <pkg>` also goes to the persistent prefix (no `--user`/`--break-system-packages` needed)

**Project dependencies**
- Python: `uv venv && uv pip install -r requirements.txt`, or `uv sync` / `uv run script.py` in a uv project, or a single script with inline deps: `uv run --with requests script.py`
- Node: `npm install` in the project as usual (`node_modules` lives in the project dir)

**Other Python versions**
- `uv python install 3.12` then `uv run --python 3.12 …`, `uv venv --python 3.12`, or `uvx --python 3.12 …`. uv downloads a standalone build; no root or system packages needed.
- `uv python list` shows what's available/installed.

**Persistence notes**
- Persisted: global npm/uv/pip installs and download caches. Not persisted: anything else under `$HOME`, `/tmp`, and system paths. Do not try `apt`/`sudo`; they won't work.
- Files in the project directory (and any extra directories the user mounted) persist normally.
- If a venv in the project stops working in a new session, recreate it (`uv venv --clear && uv sync`).

## Git, GitHub, GitLab
- `gh` and `glab` use the user's saved config; git over SSH uses the user's mounted `~/.ssh`. Check with `gh auth status` / `glab auth status`.
- If a token is reported invalid or expired, you cannot fix it — tell the user to re-run `gh auth login` / `glab auth login` (inside the sandbox, or on the host config).
- Prefer `gh`/`glab` (e.g. `gh pr create`, `gh api`, `glab mr create`, `glab api`) over scraping web pages.

## Gotchas
- Install failures with "could not resolve host"/TLS errors usually mean the network or proxy blocks that host; report the host rather than retrying repeatedly.
- Don't install into system locations or edit files outside the project; those are read-only by design.
- A tool you install globally is visible to other projects and sessions — prefer `uvx`/`npx` or a project-local venv for throwaway or version-sensitive needs.
