import json
import os
import shlex
import sys
from pathlib import Path

from agtbox import core
from agtbox.agents.base import Agent
from agtbox.locking import LockHeld, filesystem_type, launch_lock
from agtbox.opencode_store import (ensure_master_dir, master_lock_path,
                                   merge_credentials, parse_credentials,
                                   project_id, read_master, write_master)
from agtbox.sandboxes.base import SignalGuard

# Linux `stat -f -c %T` names, plus macOS `/sbin/mount` names.
NETWORK_FSTYPES = frozenset({
    "nfs", "nfs4", "lustre", "gpfs", "cifs", "smb2", "smb3", "smbfs", "ceph",
    "afpfs", "webdav",
})
# Host-only test hook: 1 forces the network path, 0 the local one.
NETFS_OVERRIDE = "AGTBOX_OPENCODE_NETFS"
CREDENTIAL_TIMEOUT = 60


def print_status(message):
    """Best-effort stderr output: after a terminal hangup a write can fail (EIO)."""
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass


def warn(message):
    print_status(f"Agent Box: warning: {message}")


def lock_held_exit(err):
    """The project-lock contention message; its wording is kept stable."""
    return SystemExit(
        "Agent Box: OpenCode is already locked by another launch.\n"
        f"Owner: {err.details}\nLock file: {err.path}\n"
        "If the original run has stopped or its node is down, remove the lock "
        "and retry:\n"
        f"  rm -- {shlex.quote(str(err.path))}")


def uses_network_path(data):
    """Per-project databases on network storage (or when detection fails)."""
    override = os.environ.get(NETFS_OVERRIDE)
    if override in ("0", "1"):
        return override == "1"
    if override is not None:
        warn(f"{NETFS_OVERRIDE}={override!r} is not 0 or 1; detecting the "
             "filesystem type instead.")
    fstype = filesystem_type(data)
    if fstype is None:
        print_status(f"Agent Box: cannot determine filesystem type for {data}; "
                     "using per-project OpenCode databases.")
        return True
    return fstype in NETWORK_FSTYPES


class Opencode(Agent):
    name = "opencode"
    packages = ("@opencode/cli",)
    obsolete_packages = ("opencode-ai",)
    default_args = ("--standalone",)
    setup_args = ("service", "set", "disabled", "true")

    @property
    def binds(self):
        return [
            core.Bind(f"{core.AGENT_CONFIG}/opencode", f"{core.HOME}/.config/opencode"),
            core.Bind(f"{core.AGENT_TOOLS}/opencode", f"{core.HOME}/.local/share/opencode"),
            core.Bind(f"{core.AGENT_STATE}/opencode", f"{core.HOME}/.local/state/opencode"),
            core.Bind(f"{core.AGENT_CACHE}/opencode", f"{core.HOME}/.cache/opencode"),
            core.skill_bind(f"{core.HOME}/.agents/skills"),
        ]

    def run(self, sandbox, ctx):
        # Inspect the data mount, not the project: that is where the database lives.
        data = Path(core.AGENT_TOOLS) / self.name
        if not uses_network_path(data):
            return super().run(sandbox, ctx)

        # One database and one lock per project, side by side in the data dir.
        # OpenCode does not create the database's parent directory.
        projects = data / "projects"
        projects.mkdir(parents=True, exist_ok=True)
        pid = project_id(ctx.app_dir)
        lock = projects / f"{pid}.lock"
        try:
            with SignalGuard() as guard, launch_lock(lock, ctx.app_dir):
                code = self._locked_run(sandbox, ctx, projects, pid, guard)
        except LockHeld as err:
            raise lock_held_exit(err) from None
        raise SystemExit(code)

    def _locked_run(self, sandbox, ctx, projects, pid, guard):
        db = projects / f"{pid}.db"
        (projects / f"{pid}.path").write_text(os.path.realpath(ctx.app_dir) + "\n")
        is_new = not db.exists()  # before any command runs: `auth export` creates it
        ctx.env.append(("OPENCODE_DB", f"projects/{pid}.db"))
        print_status(f"Agent Box: OpenCode database: {db}\n"
                     f"Agent Box: OpenCode launch lock: {projects / f'{pid}.lock'}")
        sandbox.run_setup(ctx)
        if is_new:
            self._seed(sandbox, ctx, db, guard)
        # exec would discard the host-side cleanup. Wait for the sandbox instead,
        # preserving its terminal and exit status.
        code = sandbox.wait(ctx, guard=guard)
        if db.exists():  # absent if OpenCode failed before creating it
            self._push_back(sandbox, ctx, guard)
        return code

    def _seed(self, sandbox, ctx, db, guard):
        """Give a brand-new project database the master credentials."""
        try:
            credentials = read_master()
        except (OSError, ValueError) as err:
            warn(f"cannot read OpenCode master credentials ({err}); starting "
                 "without them.")
            return
        if not credentials:
            return
        try:
            code, _ = sandbox.capture(ctx, [self.bin, "auth", "import", "--standalone"],
                                      stdin=json.dumps(credentials),
                                      timeout=CREDENTIAL_TIMEOUT, guard=guard)
            failure = f"exit {code}" if code != 0 else None
        except OSError as err:
            failure = str(err)
        if failure:
            warn(f"`opencode auth import` failed ({failure}); this project starts "
                 "without saved credentials; log in here once to fix it.")
            # The agent has not run and the lock is held: these files can only
            # hold what the failed import just created. The agent then creates a
            # fresh database, so this project is not seeded again later.
            for suffix in ("", "-wal", "-shm"):
                Path(f"{db}{suffix}").unlink(missing_ok=True)

    def _push_back(self, sandbox, ctx, guard):
        """Fold this project's credentials into the master."""
        try:
            code, out = sandbox.capture(ctx, [self.bin, "auth", "export", "--standalone"],
                                        timeout=CREDENTIAL_TIMEOUT, guard=guard)
        except OSError as err:
            warn(f"`opencode auth export` failed ({err}); this session's "
                 "credentials were not saved for new projects.")
            return
        if code != 0:
            warn(f"`opencode auth export` failed (exit {code}); this session's "
                 "credentials were not saved for new projects.")
            return
        try:
            exported = parse_credentials(out)
        except ValueError as err:
            warn(f"`opencode auth export` printed unexpected output ({err}); this "
                 "session's credentials were not saved for new projects.")
            return
        if not exported:
            return
        try:
            ensure_master_dir()
            with launch_lock(master_lock_path(), ctx.app_dir):
                write_master(merge_credentials(read_master() or [], exported))
        except LockHeld as err:
            warn("OpenCode master credentials are locked by another launch; this "
                 "session's credential changes were not saved to them.\n"
                 f"Owner: {err.details}\n"
                 "If that launch has stopped or its node is down, remove the lock:\n"
                 f"  rm -- {shlex.quote(str(err.path))}")
        except (OSError, ValueError) as err:
            warn(f"cannot update OpenCode master credentials ({err}); they were "
                 "left unchanged.")
