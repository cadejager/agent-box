# OpenCode: one database per project on network filesystems — design

Scope: the agent-box harness (`agtbox/agents/opencode.py`, `agtbox/locking.py`,
`agtbox/sandboxes/base.py`, tests, docs). Behavior on local filesystems is unchanged.

## Goal

On hosts whose OpenCode data directory is on a network filesystem (NFS homes on HPC
systems, where tmpfs is the only local storage), let OpenCode run in several
projects at once — from one host or many — instead of serializing every launch
behind one global lock. Each project gets its own SQLite database and its own lock.
Logging in once (API keys) should carry over to every project and every CPU
architecture without re-authenticating.

## Background (verified against `@opencode/cli` 2.4.1)

- `OPENCODE_DB` selects the database. A relative value resolves under the data dir
  (`~/.local/share/opencode`): `OPENCODE_DB=projects/x.db` → `<data>/projects/x.db`.
  OpenCode does **not** create missing parent directories (`auth export` exits 1).
- v2 stores credentials in the database. API keys set in the environment are also
  picked up (listed as `environment`), but that is not used here.
- `opencode auth export` prints all stored credentials, secrets included, as a JSON
  array of `{id, integrationID, label, active, value}`. On a brand-new DB it creates
  the DB and prints `[]`.
- `opencode auth import [file]` reads that JSON (stdin when no file). Into a fresh
  DB it preserves `active` flags. It **skips** any credential whose `id` already
  exists — it never updates a value — and credentials new to a non-empty DB arrive
  inactive.
- `opencode service set disabled true` (our `setup_args`) does not create the DB.
- Everything else in the data dir is already per-project or per-run inside OpenCode
  (`snapshot/<project-id>`, `memory/<project-id>`, `worktree/<id>`, per-run logs,
  ID-keyed `artifacts/`), so the database is the only shared writer we must split.
- OpenCode's own project ID (the repo's sorted root commit; `global` outside git) is
  **not** used: every clone/worktree of a repo shares it (two checkouts on different
  nodes would contend for one DB again) and all non-git directories collapse to one.

## Design

### When it applies

`Opencode.run` already detects the filesystem type of `${AGENT_TOOLS}/opencode` (the
data dir, not the project). That detection stays. New: the host-side environment
variable `AGTBOX_OPENCODE_NETFS` overrides it, for testing on local disks:

- `1` — use the network-filesystem path below.
- `0` — use the local path (unchanged: shared `opencode.db`, no lock, exec).
- unset — detect as today (unknown type → network path, with the existing warning).
- any other value — warn and detect as if unset.

It is read on the launcher's host only and is not forwarded into the sandbox.

### Project identity

`project_id(app_dir)` = `<base>-<hash>`:

- `<base>`: basename of `realpath(app_dir)`, with every character outside
  `[A-Za-z0-9._-]` replaced by `_`; `root` if empty (project is `/`).
- `<hash>`: first 12 hex digits of SHA-256 of `realpath(app_dir)` (UTF-8, or
  `os.fsencode`).

Renaming or moving a project starts a fresh database; the old one stays on disk.

### Per-project files

All in `${AGENT_TOOLS}/opencode/projects/` (per-arch, inside the sandbox's data bind,
so other OpenCode sessions can inspect them):

| File | Purpose |
|---|---|
| `<id>.db` (+ SQLite `-wal`/`-shm`) | the project's OpenCode database |
| `<id>.path` | the project's realpath, one line — maps a DB back to its project |
| `<id>.lock` | the project's launch lock (existing `launch_lock`, unchanged format and messages) |

The launcher creates `projects/` (`mkdir -p`) and writes `<id>.path` on every launch
(cheap; self-heals if deleted). The sandbox gets `OPENCODE_DB=projects/<id>.db`,
appended to `ctx.env` by `Opencode.run`. The host's own `OPENCODE_DB` is still not
forwarded.

Accepted trade-off: the lock is visible and writable inside the sandbox, so an agent
could delete it. `launch_lock` already compares inodes, so a deleted-and-recreated
lock is never removed by the wrong owner; it does not prevent a second launch after
such a deletion. The old global lock (`~/.local/share/agent-box/opencode-<arch>.launch.lock`)
is no longer used.

### Master credentials

`~/.local/share/agent-box/opencode/credentials.json` — outside the per-arch
toolchain dir (one login serves every arch) and outside every sandbox bind (agents
cannot read or modify it). Directory mode `0700`, file mode `0600`, `auth export`
format. Guarded by `~/.local/share/agent-box/opencode/credentials.lock` (same
`launch_lock`, held only for the read-merge-write). Written via a temp file in the
same directory + `os.replace`, so a crash never leaves a partial file.

No bootstrap from the old shared `opencode.db`: until the first session exits after a
login, the master is absent and new projects start with no credentials.

**Seed (new project only).** "New" = `projects/<id>.db` does not exist, checked on
the host **before** any sandbox command runs. If new and the master exists and is
non-empty, run `opencode auth import --standalone` in the sandbox with the master on
stdin. Existing project DBs are never re-seeded.

**Push back (every launch).** After the agent exits — any exit status, including
after SIGINT/SIGTERM/SIGHUP — and while still holding the project lock, run
`opencode auth export --standalone` in the sandbox, capturing stdout, then merge
into the master under the master lock:

- Credentials are keyed by `id`; a project credential inserts or replaces the
  master entry with that `id`.
- For each `integrationID` that has an active credential in the export, the master's
  `active` flags for that integration are set to match (that one true, the rest false).
- Master credentials absent from the export are kept (logouts don't propagate).
- No stale-value protection: an old project holding an outdated value for an `id`
  overwrites the master's. Accepted; fix by logging in again.

If the launcher is SIGKILLed or crashes, no push happens; the session's changes stay
in its project DB only.

### Sandbox support: `Sandbox.capture`

New base-class method `capture(ctx, cmd, stdin=None) -> (returncode, stdout)`: runs
`build_run_argv(ctx, cmd)` via `subprocess.run(input=stdin, stdout=PIPE)`, stderr
passed through. Secrets travel only through pipes, never through files in a bind.
Works for both sandboxes (podman always passes `-i`; `-t` only with a TTY). The
existing `run_setup` stays as is.

### Launch sequence (network path)

1. Compute `id`; `mkdir -p projects/`; note `is_new`; write `<id>.path`; append
   `OPENCODE_DB` to `ctx.env`.
2. Acquire `projects/<id>.lock` (fails with the existing owner/host message).
3. `sandbox.run_setup(ctx)` (`service set disabled true`).
4. If `is_new` and the master is present: `capture(…auth import…, stdin=master)`.
5. `code = sandbox.wait(ctx)`.
6. `capture(…auth export…)`; merge into the master under its lock.
7. Release the project lock; `SystemExit(code)`.

### Errors

Each warning starts `Agent Box: warning:` and is printed to stderr. Neither failure changes the exit status.

- Seed failure (unreadable master, invalid JSON, import non-zero): warn, launch anyway.
- Push-back failure (export non-zero, invalid JSON, master lock held, write error):
  warn; the master is left as it was.
- Master lock already held: warn and skip the merge. Don't block or retry: another
  project is merging right now, and its owner message is shown.

### Existing data

The old shared `${AGENT_TOOLS}/opencode/opencode.db` is left untouched. The network
path never opens it; past sessions stay in it, readable with `AGTBOX_OPENCODE_NETFS=0`
or on a local-disk host. No migration and no deletion.

## Testing

Unit tests (`test/test_opencode.py`):

- `project_id`: sanitization, `/`, symlinked path resolves to realpath, stable hash,
  different paths with the same basename differ.
- Merge: insert, replace by `id`, keep master-only entries, active flags per
  integration, empty export is a no-op, master file mode `0600`, atomic replace.
- `AGTBOX_OPENCODE_NETFS`: `1` forces network path on a local fs, `0` forces local on
  NFS, invalid value warns and detects.

Stub-sandbox tests (existing pattern):

- Network path sets `OPENCODE_DB=projects/<id>.db`, creates `projects/` and
  `<id>.path`, locks `projects/<id>.lock`.
- Two different projects run concurrently; the same project is blocked with the
  owner message.
- Import runs only when the DB did not exist and the master exists; master content is
  delivered on stdin.
- Export + merge run after a non-zero agent exit and after SIGTERM.
- Seed or push-back failure warns and preserves the agent's exit status and the master.
- Local path is unchanged (no `OPENCODE_DB`, no lock, exec).

Manual: on this (local-disk) host, `AGTBOX_OPENCODE_NETFS=1 ./bin/agtbox.py opencode`
in two projects at once; log in in one, exit, open a brand-new third project and
confirm the key is present.

## Docs

Update the OpenCode storage/locking sections of `CLAUDE.md` and `README.md`:
per-project DBs on network filesystems, the `projects/` layout, the master credential
file and its semantics (seed new projects only, push back on exit, no logout
propagation, no stale protection), and `AGTBOX_OPENCODE_NETFS`.

## Out of scope

- Cleanup of databases for deleted or moved projects (use the `.path` files).
- Updating existing project DBs from the master, or protecting against stale values.
- Bootstrapping the master from the old shared database.
- Sign-in (OAuth) credentials: they are exported/imported like keys, but rotating
  refresh tokens can go stale in other projects' copies.
- Any change to the local-filesystem path.
