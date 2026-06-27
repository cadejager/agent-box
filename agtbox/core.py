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
# Generic (non-agent) env. Agent-specific vars (ANTHROPIC_*, OPENAI_*, OPENCODE_*,
# CLAUDE_CODE_*) move into the agent plugins (Task 4).
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
