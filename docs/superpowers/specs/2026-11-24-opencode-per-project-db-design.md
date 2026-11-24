# OpenCode: one database per project on network filesystems — design

Scope: the agent-box harness (`agtbox/agents/opencode.py`, `agtbox/locking.py`,
`agtbox/sandboxes/{base,bwrap,podman}.py`, tests, docs). Behavior on local
filesystems is unchanged.

## Goal

On hosts whose OpenCode data directory is on a network filesystem (NFS homes on HPC
systems, where tmpfs is the only local storage), let OpenCode run in several
projects at once — from one host or many — instead of serializing every launch
behind one global lock. Each project gets its own SQLite database and its own lock.
Logging in once (API keys) should carry over to every new project and every CPU
architecture without re-authenticating.

## Background (verified against `@opencode/cli` 2.4.1)

- `OPENCODE_DB` selects the database. A relative value resolves under the data dir
  (`~/.local/share/opencode`): `OPENCODE_DB=projects/x.db` → `<data>/projects/x.db`.
  OpenCode does **not** create missing parent directories (`auth export` exits 1).
- v2 stores credentials in the database. API keys set in the environment are also
  picked up (listed as `environment`), but that is not used here.
- `opencode auth export` prints all stored credentials, secrets included, as a JSON
  array of `{id, integrationID, label, active, value}`. On a missing DB it creates
  the DB and prints `[]`.
- `opencode auth import [file]` reads that JSON (stdin when no file). Into a fresh
  DB it preserves `active` flags. It **skips** any credential whose `id` already
  exists — it never updates a value — and credentials new to a non-empty DB arrive
  inactive.
- A provider whose only credential is inactive still works (same model list as when
  active); with no credential its models are absent. `active` only chooses among
  several credentials for one provider.
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

`project_id(app_dir)` = `<base>-<hash>`, computed from `realpath(ctx.app_dir)`:

- `<base>`: its basename, with every character outside `[A-Za-z0-9._-]` replaced by
  `_`; `root` if empty (project is `/`).
- `<hash>`: first 12 hex digits of SHA-256 of `os.fsencode(realpath)`.

The ID is per path as seen on the launching node. Renaming or moving a project
starts a fresh database (the old one stays on disk); one project reached through
different mount paths on different nodes gets different databases; two node-local
paths that happen to be equal (e.g. `/tmp/x` on two nodes) share one. The docs say so.

### Per-project files

All in `${AGENT_TOOLS}/opencode/projects/` (per-arch, inside the sandbox's data bind,
so other OpenCode sessions can inspect them):

| File | Purpose |
|---|---|
| `<id>.db` (+ SQLite `-wal`/`-shm`) | the project's OpenCode database |
| `<id>.path` | the project's realpath, one line — maps a DB back to its project |
| `<id>.lock` | the project's launch lock (`launch_lock`; owner format and duplicate-launch message unchanged) |

The sandbox gets `OPENCODE_DB=projects/<id>.db`, appended to `ctx.env` by
`Opencode.run`. The host's own `OPENCODE_DB` is still not forwarded.

Accepted trade-off: the lock is visible and writable inside the sandbox, so an agent
could delete it. `launch_lock` already compares inodes, so a deleted-and-recreated
lock is never removed by the wrong owner; it does not prevent a second launch after
such a deletion. The old global lock (`~/.local/share/agent-box/opencode-<arch>.launch.lock`)
is no longer used.

### Lock contention as an exception

`launch_lock` currently raises `SystemExit` (with OpenCode-specific text) when the
lock exists. It changes to raise `LockHeld`, carrying the lock path and the owner
details string (or "owner information unavailable"). Callers decide:

- Project lock: converts `LockHeld` into today's `SystemExit` message — "OpenCode is
  already locked by another launch", owner (host, PID, start, project), lock file,
  `rm -- <lock>` recovery line. Behavior is identical to today.
- Master lock: converts it into a warning (below).

### Master credentials

`~/.local/share/agent-box/opencode/credentials.json` — outside the per-arch
toolchain dir (one login serves every arch) and outside every sandbox bind (agents
cannot read or modify it). `auth export` format. Guarded by
`~/.local/share/agent-box/opencode/credentials.lock` (`launch_lock`, held only for
the read-merge-write, no retry).

Writing: `mkdir -p` the directory, then `chmod 0700` it (also when it already
existed — `mkdir` honours umask and ignores existing dirs). Write with
`tempfile.mkstemp` in that directory (always `0600`), `fsync`, then `os.replace`, so a
crash or NFS hiccup never leaves a partial master.

No bootstrap from the old shared `opencode.db`: until the first session exits after a
login, the master is absent and new projects start with no credentials.

**Seed (new project only).** "New" = `projects/<id>.db` does not exist, checked on
the host **after** acquiring the project lock and before any sandbox command runs.
If new and the master exists and holds a non-empty JSON array, run
`opencode auth import --standalone` in the sandbox with the master on stdin. If the
master is absent, empty, or unreadable/invalid (warn for the latter), nothing runs
and the project stays "new" until a master exists. If the import exits non-zero,
warn and delete `<id>.db`, `<id>.db-wal`, `<id>.db-shm` — those can only contain what
the failed import itself just created (the agent has not run yet; the lock is held),
so the next launch retries the seed. A DB the agent has used is never deleted.

**Push back (every launch whose DB exists).** After the agent exits — any exit status,
including after SIGINT/SIGTERM/SIGHUP — and while still holding the project lock:
if `<id>.db` does not exist (OpenCode failed before creating it), skip; otherwise
run `opencode auth export --standalone` in the sandbox (timeout 60 s), capture
stdout, parse it as a JSON array, and merge into the master under the master lock.
A session that used only unauthenticated providers (e.g. a local LM Studio) exports
`[]`, which leaves the master unchanged.

Merge rules:

- Credentials are keyed by `id`. A project credential inserts a new master entry or
  replaces the existing entry's fields **except `active`**, which a replace never
  changes by itself. New entries are inserted with `active: false`.
- Then, per `integrationID`: if the export marks one or more of its credentials
  active, the first of them (in export order) becomes the master's only active
  credential for that integration; otherwise that integration's master flags are
  left as they were.
- Master credentials absent from the export are kept (logouts don't propagate).
- No stale-value protection: an old project holding an outdated value for an `id`
  overwrites the master's. Accepted; fix by logging in again.

If the launcher is SIGKILLed or crashes, no push happens; the session's changes stay
in its project DB only.

### Sandbox support: `Sandbox.capture`

New base-class method `capture(ctx, cmd, stdin=None, timeout=None) -> (returncode, stdout)`:
runs `build_run_argv(ctx, cmd, tty=False)` via `subprocess.run` with
`input=stdin` (or `stdin=subprocess.DEVNULL` when `None`), `stdout=PIPE`, stderr
passed through, and the timeout (a timeout counts as failure). Secrets travel only
through pipes, never through files in a bind.

`build_run_argv(ctx, cmd=None, tty=None)` gains a `tty` override. Podman uses it to
omit `-t` when `tty=False` (today it adds `-t` whenever the *launcher's*
stdin/stdout are TTYs, which would break piped stdin and merge stderr/CRLF into
captured stdout); `None` keeps today's detection. Bwrap accepts and ignores it.
The existing `run_setup` stays as is.

### Signals

`wait()` today installs its handlers (forward SIGTERM/SIGHUP, no-op SIGINT) only
around the agent and restores the defaults when the agent exits, so the export and
merge would run with default handlers: SIGTERM (e.g. Slurm ending the job) would kill
the launcher and leave the project lock behind; Ctrl-C would raise
`KeyboardInterrupt` and lose the exit status.

Change: the handler installation becomes a reusable context manager that
`Opencode.run` holds from just after acquiring the project lock until just before
releasing it (setup, seed, agent, export, merge). `wait()` keeps forwarding signals
to the agent while it runs; outside the agent, a received SIGTERM/SIGHUP is
recorded rather than acted on. `capture` children are started in their own process
group (`start_new_session=True`) so terminal Ctrl-C doesn't reach them; a received
SIGTERM/SIGHUP is forwarded to a running `capture` child (the 60 s timeout bounds it
either way). The launcher's exit status is the agent's.

Warnings printed after the agent exits are best-effort: a failed write to stderr
(e.g. `EIO` after the terminal hung up) is swallowed, never raised.

### Launch sequence (network path)

1. Compute `id`; `mkdir -p projects/`.
2. Acquire `projects/<id>.lock` (`LockHeld` → today's `SystemExit` message).
3. Enter the signal context. Write `<id>.path`; note `is_new`; append
   `OPENCODE_DB` to `ctx.env`.
4. `sandbox.run_setup(ctx)` (`service set disabled true`).
5. If `is_new` and the master is a non-empty array: seed (above).
6. `code = sandbox.wait(ctx)`.
7. If `<id>.db` exists: export and merge (above).
8. Leave the signal context; release the project lock; `SystemExit(code)`.

### Errors

All warnings: `Agent Box: warning: …` on stderr, best-effort; none change the exit
status.

- Seed failure (unreadable/invalid master, import non-zero or timeout): warn; delete
  the just-created DB files as above; launch anyway.
- Push-back failure (export non-zero/timeout, invalid JSON, write error): warn; the
  master is left as it was.
- Master lock held (`LockHeld`): warn that this session's credential changes were not
  saved to the master, with the owner details and the `rm -- …/credentials.lock`
  recovery line (in case a crash left it behind). No retry: the other session is
  merging its own credentials, and this project keeps its credentials in its own DB.

### Existing data

The old shared `${AGENT_TOOLS}/opencode/opencode.db` is left untouched. The network
path never opens it; past sessions stay in it, readable with `AGTBOX_OPENCODE_NETFS=0`
or on a local-disk host. No migration and no deletion.

## Testing

Existing tests that encode the old design are rewritten, not kept:
`test_lock_is_not_inside_a_sandbox_bind` (the lock is now inside the data bind, next
to the DB) and `test_nfs_holds_global_lock_through_setup_and_exit` (now per-project
lock) in `test/test_opencode.py`. `LaunchLock` tests change from expecting
`SystemExit` to `LockHeld`; one test checks the project-lock message is unchanged.

Unit tests:

- `project_id`: sanitization, `/`, symlinked path resolves to realpath, stable hash,
  different paths with the same basename differ.
- Merge: insert (inactive), replace by `id` without touching `active`, keep
  master-only entries, per-integration active (first active wins, none → unchanged),
  empty export is a no-op, master file and dir modes, atomic replace (no partial file
  on a simulated write failure).
- `AGTBOX_OPENCODE_NETFS`: `1` forces network path on a local fs, `0` forces local on
  NFS, invalid value warns and detects.
- Podman `build_run_argv(..., tty=False)` has no `-t` even with `isatty` patched true;
  `capture` uses `DEVNULL` when there is no stdin.

Stub-sandbox tests (existing pattern):

- Network path sets `OPENCODE_DB=projects/<id>.db`, creates `projects/` and
  `<id>.path`, locks `projects/<id>.lock`.
- Two different projects run concurrently; the same project is blocked with the
  owner message.
- Import runs only when the DB did not exist and the master is a non-empty array;
  master content is delivered on stdin. A failed import deletes the new DB files and
  the next launch seeds again.
- No export when the agent never created the DB.
- Export + merge run after a non-zero agent exit, after SIGTERM to the launcher, and
  a SIGINT during the export doesn't lose the agent's exit status or leave the lock.
- Master lock held: warning printed, master unchanged, agent's exit status kept.
- Seed or push-back failure warns and preserves the agent's exit status and the master.
- Local path is unchanged (no `OPENCODE_DB`, no lock, exec).

Manual: on this (local-disk) host, `AGTBOX_OPENCODE_NETFS=1 ./bin/agtbox.py opencode`
in two projects at once; log in in one, exit, open a brand-new third project and
confirm the key is present. Repeat the seed/export once with `-s podman` from an
interactive terminal.

## Docs

Update the OpenCode storage/locking sections of `CLAUDE.md` and `README.md`:
per-project DBs on network filesystems, the `projects/` layout, project-ID caveats,
the master credential file and its semantics (seed new projects only, push back on
exit, no logout propagation, no stale protection, a contended master lock skips one
push), and `AGTBOX_OPENCODE_NETFS`.

## Out of scope

- Cleanup of databases for deleted or moved projects (use the `.path` files).
- Updating existing project DBs from the master, or protecting against stale values.
- Bootstrapping the master from the old shared database.
- Retrying a contended master lock.
- Sign-in (OAuth) credentials: they are exported/imported like keys, but rotating
  refresh tokens can go stale in other projects' copies.
- Any change to the local-filesystem path.
