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
