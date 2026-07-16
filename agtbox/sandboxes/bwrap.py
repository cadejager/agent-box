import grp
import os
import subprocess
from agtbox import core
from agtbox.sandboxes.base import Sandbox


def ensure_identity_files():
    """Generate synthetic passwd/group files in AGENT_STATE for LDAP-backed users,
    which bwrap binds over /etc/passwd and /etc/group so the sandbox can resolve the
    invoking user/groups. Only appends the current user/groups when missing from the
    host files. bwrap-only -- podman resolves identity from the image/host mapping."""
    os.makedirs(core.AGENT_STATE, exist_ok=True)

    def sync_file(filename, host_path, current_lines):
        dst = f"{core.AGENT_STATE}/{filename}"
        try:
            with open(host_path, "r") as f:
                content = f.read().splitlines()
        except OSError:
            content = []

        for line, marker in current_lines:
            if not any(existing.startswith(f"{marker}:") for existing in content):
                content.append(line)

        with open(dst, "w") as f:
            f.write("\n".join(content) + "\n")

    username = os.environ.get("USER") or str(os.getuid())
    shell = os.environ.get("SHELL") or "/bin/bash"

    passwd_lines = [
        # core.HOME is the module constant (os.environ["HOME"], validated at import).
        (f"{username}:x:{os.getuid()}:{os.getgid()}::{core.HOME}:{shell}", username),
    ]

    groups = []
    seen = set()
    for gid in [os.getgid(), *os.getgroups()]:
        if gid in seen:
            continue
        seen.add(gid)
        try:
            group = grp.getgrgid(gid)
            groups.append((f"{group.gr_name}:x:{group.gr_gid}:{','.join(group.gr_mem)}", group.gr_name))
        except KeyError:
            groups.append((f"{gid}:x:{gid}:", str(gid)))

    sync_file("passwd", "/etc/passwd", passwd_lines)
    sync_file("group", "/etc/group", groups)


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
        ensure_identity_files()

    def base_args(self, env_pairs):
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
        bw += self.fmt_env(env_pairs)
        return bw

    def build_run_argv(self, ctx):
        bw = self.base_args(ctx.env)
        bw += ["--bind", ctx.app_dir, ctx.app_dir, "--chdir", ctx.app_dir]
        bw += self.bind_args(ctx)
        return ["bwrap", *bw, "--", ctx.agent.bin, *ctx.extra_args]

    def install_machine(self):
        return os.uname().machine        # bwrap is Linux-only: host arch == run arch

    def install(self, script, pairs):
        # Run the install in a nested bwrap with the SAME locked-down base as a real
        # run, over a minimal env (no project/config binds). `pairs` is the fully
        # resolved install env from ensure_tools, so no env assembly here. Generate
        # the identity files ourselves so install never depends on prepare() first --
        # base_args binds AGENT_STATE/passwd, which must exist.
        ensure_identity_files()
        bw = self.base_args(list(pairs))
        subprocess.run(["bwrap", *bw, "--", "/usr/bin/bash", "-c", script], check=True)
