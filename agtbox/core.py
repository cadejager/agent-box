"""Core constants, the Bind type, env tables, and host-side source setup."""
import grp
import os
import sys
from dataclasses import dataclass

HOME = os.environ.get("HOME")
if not HOME:
    print("Error: HOME is not set.", file=sys.stderr)
    sys.exit(1)

ARCH = os.uname().machine
AGENT_TOOLS = f"{HOME}/.local/share/agent-box/{ARCH}"
AGENT_CONFIG = f"{HOME}/.config/agent-box"
AGENT_STATE = f"{HOME}/.local/state/agent-box"
AGENT_CACHE = f"{HOME}/.cache/agent-box"
IMAGE = "agent-box"


@dataclass
class Bind:
    """A host->sandbox mount. kind: 'dir'/'file' both emit a bind arg (file is
    seeded '{}' if absent); 'seed' emits NO bind arg -- it is written inside an
    already-bound dir (see core.ensure_sources)."""
    src: str
    dst: str
    kind: str = "dir"


AGENT_ENV = [
    f"HOME={HOME}",
    f"PATH={AGENT_TOOLS}/bin:{AGENT_TOOLS}/node/bin:/usr/bin:/bin",
    f"npm_config_prefix={AGENT_TOOLS}",
    f"npm_config_cache={AGENT_CACHE}/npm",
    f"PIP_PREFIX={AGENT_TOOLS}",
    f"PYTHONUSERBASE={AGENT_TOOLS}",
    f"PIP_CACHE_DIR={AGENT_CACHE}/pip",
    "PIP_BREAK_SYSTEM_PACKAGES=1",
    f"UV_CACHE_DIR={AGENT_CACHE}/uv",
    f"UV_TOOL_DIR={AGENT_TOOLS}/uv/tools",
    f"UV_TOOL_BIN_DIR={AGENT_TOOLS}/bin",
]
# Generic (non-agent) env, applied for every agent. Agent-specific vars
# (ANTHROPIC_*, OPENAI_*, OPENCODE_*, CLAUDE_CODE_*) live on the agent plugins.
ENV_FORWARD_GENERIC = [
    "TERM", "COLORTERM", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
]
ENV_LITERAL_GENERIC = []


def arch_pair(machine):
    if machine == "aarch64":
        return "arm64", "arm64"
    if machine == "x86_64":
        return "x64", "amd64"
    print(f"Error: unsupported architecture '{machine}'.", file=sys.stderr)
    sys.exit(1)


def _kv(entry):
    key, _, val = entry.partition("=")
    return key, val


def resolve_env(extra_forward, extra_literal):
    """Resolve the env allowlist to (key, value) pairs: AGENT_ENV + generic and
    agent literals always; generic + agent forwards only when actually set on the
    host (an unset var must not shadow mounted config)."""
    pairs = [_kv(e) for e in AGENT_ENV]
    for name in [*ENV_FORWARD_GENERIC, *extra_forward]:
        if os.environ.get(name):
            pairs.append((name, os.environ[name]))
    pairs += [_kv(e) for e in [*ENV_LITERAL_GENERIC, *extra_literal]]
    return pairs


SHARED_BINDS = [
    Bind(f"{AGENT_CONFIG}/git", f"{HOME}/.config/git"),
    Bind(f"{AGENT_CONFIG}/gh", f"{HOME}/.config/gh"),
    Bind(f"{AGENT_CONFIG}/glab", f"{HOME}/.config/glab-cli"),
    Bind(f"{AGENT_CONFIG}/ssh", f"{HOME}/.ssh"),
    Bind(f"{AGENT_CONFIG}/git/config", f"{HOME}/.config/git/config", "seed"),
]


def ensure_identity_files():
    """Generate synthetic passwd/group files in AGENT_STATE for LDAP users.
    Only append the current user/groups if they are missing from the host files.
    """
    os.makedirs(AGENT_STATE, exist_ok=True)

    def sync_file(filename, host_path, current_lines):
        dst = f"{AGENT_STATE}/{filename}"
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
    home_dir = os.environ.get("HOME") or HOME
    shell = os.environ.get("SHELL") or "/bin/bash"

    passwd_lines = [
        (f"{username}:x:{os.getuid()}:{os.getgid()}::{home_dir}:{shell}", username),
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


def ensure_sources(binds):
    """Create host-side bind sources so a fresh user can launch. `binds` is the
    full merged list (shared + agent). dir/file -> create the dir; file -> seed
    '{}' if absent; seed -> create an empty file inside its (already-created) dir.
    NB: synthetic passwd/group identity files are NOT generated here -- they are a
    bwrap-only concern, generated in Bwrap.prepare."""
    for d in (AGENT_TOOLS, f"{AGENT_CACHE}/npm", f"{AGENT_CACHE}/pip", f"{AGENT_CACHE}/uv"):
        os.makedirs(d, exist_ok=True)
    for b in binds:
        if b.kind == "dir":
            os.makedirs(b.src, exist_ok=True)
        elif b.kind == "file":
            os.makedirs(os.path.dirname(b.src), exist_ok=True)
            if not os.path.exists(b.src):
                with open(b.src, "w") as fh:
                    fh.write("{}")
        elif b.kind == "seed":
            os.makedirs(os.path.dirname(b.src), exist_ok=True)
            if not os.path.exists(b.src):
                open(b.src, "w").close()
    os.chmod(f"{AGENT_CONFIG}/ssh", 0o700)  # ssh refuses group/world-accessible ~/.ssh


def normalize_paths(app_dir, volumes, ro_volumes):
    """Resolve app_dir and every extra volume to an absolute path."""
    app_dir = os.path.realpath(app_dir)
    volumes = [os.path.realpath(v) for v in volumes]
    ro_volumes = [os.path.realpath(v) for v in ro_volumes]
    # A bind source must exist or the engine fails with an opaque error -- check
    # up front and fail with a clear message instead.
    for p in [app_dir, *volumes, *ro_volumes]:
        if not os.path.exists(p):
            print(f"Error: path does not exist: {p}", file=sys.stderr)
            sys.exit(1)
    # A path given as BOTH -w (rw) and -r (ro) would be a duplicate bind target and
    # bwrap would reject it. Read-write wins -- drop it from ro_volumes and warn.
    kept = []
    for ro in ro_volumes:
        if ro in volumes:
            print(f"Warning: {ro} given as both -w (rw) and -r (ro); binding read-write.", file=sys.stderr)
        else:
            kept.append(ro)
    ro_volumes = kept
    return app_dir, volumes, ro_volumes
