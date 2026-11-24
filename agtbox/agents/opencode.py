import shlex
import sys
from pathlib import Path

from agtbox import core
from agtbox.agents.base import Agent
from agtbox.locking import LockHeld, filesystem_type, launch_lock

# Linux `stat -f -c %T` names, plus macOS `/sbin/mount` names.
NETWORK_FSTYPES = frozenset({
    "nfs", "nfs4", "lustre", "gpfs", "cifs", "smb2", "smb3", "smbfs", "ceph",
    "afpfs", "webdav",
})


def lock_held_exit(err):
    """The project-lock contention message; its wording is kept stable."""
    return SystemExit(
        "Agent Box: OpenCode is already locked by another launch.\n"
        f"Owner: {err.details}\nLock file: {err.path}\n"
        "If the original run has stopped or its node is down, remove the lock "
        "and retry:\n"
        f"  rm -- {shlex.quote(str(err.path))}")


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
        ]

    def run(self, sandbox, ctx):
        # Inspect the data mount, not the project: the shared database contains
        # credentials as well as history. Keep its existing location and contents.
        data = Path(core.AGENT_TOOLS) / self.name
        fstype = filesystem_type(data)
        if fstype is not None and fstype not in NETWORK_FSTYPES:
            return super().run(sandbox, ctx)

        # Same filesystem as the data dir, but outside AGENT_TOOLS, which the
        # sandbox mounts read-write: the agent must not see or remove the lock.
        lock = Path(core.AGENT_TOOLS).parent / f"{self.name}-{core.ARCH}.launch.lock"
        if fstype is None:
            print(f"Agent Box: cannot determine filesystem type for {data}; "
                  "using the OpenCode launch lock.", file=sys.stderr)
        try:
            with launch_lock(lock, ctx.app_dir):
                print(f"Agent Box: OpenCode launch lock: {lock}", file=sys.stderr)
                sandbox.run_setup(ctx)
                # exec would discard the host-side lock cleanup. Wait for the sandbox
                # instead, preserving its terminal and exit status.
                code = sandbox.wait(ctx)
        except LockHeld as err:
            raise lock_held_exit(err) from None
        raise SystemExit(code)
