import os
import subprocess
from agtbox import core
from agtbox.context import RunContext
from agtbox.sandboxes.base import Sandbox


class Bwrap(Sandbox):
    name = "bwrap"
    priority = 20            # preferred over podman when available

    def fmt_env(self, pairs):
        out = []
        for k, v in pairs:
            out += ["--setenv", k, v]
        return out

    def fmt_bind(self, src, dst, ro):
        return ["--ro-bind" if ro else "--bind", src, dst]

    def prepare(self, ctx):
        core.ensure_identity_files()

    def base_args(self, ctx):
        resolv = os.path.realpath("/etc/resolv.conf")
        bw = [
            "--clearenv",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
            "--ro-bind-try", resolv, resolv,
            "--ro-bind", f"{core.AGENT_STATE}/passwd", "/etc/passwd",
            "--ro-bind", f"{core.AGENT_STATE}/group", "/etc/group",
            "--ro-bind-try", "/var/lib/ca-certificates", "/var/lib/ca-certificates",
            "--ro-bind-try", "/bin", "/bin", "--ro-bind-try", "/sbin", "/sbin",
            "--ro-bind-try", "/lib", "/lib", "--ro-bind-try", "/lib64", "/lib64",
            "--ro-bind-try", "/opt", "/opt", "--ro-bind-try", "/cpe", "/cpe",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--tmpfs", core.HOME,
            "--bind", core.AGENT_TOOLS, core.AGENT_TOOLS,
            "--bind", core.AGENT_CACHE, core.AGENT_CACHE,
            "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        ]
        bw += self.env_args(ctx)
        return bw

    def build_run_argv(self, ctx):
        bw = self.base_args(ctx)
        bw += ["--bind", ctx.app_dir, ctx.app_dir, "--chdir", ctx.app_dir]
        bw += self.bind_args(ctx)
        return ["bwrap", *bw, "--", ctx.agent.bin, *ctx.extra_args]

    def install_machine(self):
        return os.uname().machine        # bwrap is Linux-only: host arch == run arch

    def install(self, script, pairs):
        # Run the install in a nested bwrap with the SAME locked-down base as a real
        # run, over a minimal context (no project/config binds). `pairs` is already
        # resolved by ensure_tools (full allowlist for bwrap), so no env assembly here.
        bw = self.base_args(RunContext(agent=None, binds=[], env=list(pairs), app_dir=core.HOME))
        subprocess.run(["bwrap", *bw, "--", "/usr/bin/bash", "-c", script], check=True)
