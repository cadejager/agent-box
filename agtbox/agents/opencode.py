import sys
from pathlib import Path

from agtbox import core
from agtbox.agents.base import Agent
from agtbox.locking import filesystem_type, launch_lock


class Opencode(Agent):
    name = "opencode"
    packages = ("@opencode/cli",)
    obsolete_packages = ("opencode-ai",)
    default_args = ("--standalone",)
    setup_args = ("service", "set", "disabled", "true")
    env_forward = ("OPENCODE_ENABLE_EXA",)
    env_literal = ("OPENCODE_EXPERIMENTAL_LSP_TOOL=true",)

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
        data = Path(core.AGENT_TOOLS) / "opencode"
        fstype = filesystem_type(data)
        if fstype is not None and fstype not in ("nfs", "nfs4"):
            return super().run(sandbox, ctx)

        lock = Path(core.AGENT_STATE) / "opencode" / "launch.lock"
        if fstype is None:
            print(f"Agent Box: cannot determine filesystem type for {data}; "
                  "using the OpenCode launch lock.", file=sys.stderr)
        with launch_lock(lock, ctx.app_dir):
            print(f"Agent Box: OpenCode launch lock: {lock}", file=sys.stderr)
            sandbox.run_setup(ctx)
            # exec would discard the host-side lock cleanup. Wait for the sandbox
            # instead, preserving its terminal and exit status.
            raise SystemExit(sandbox.wait(ctx))
