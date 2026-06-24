#!/usr/bin/env python3
# agtbox.py -- run an AI coding agent (claude, opencode, codex) inside an
# unprivileged sandbox over a small per-user toolchain that is auto-installed on
# first use. Two engines: bubblewrap (bwrap) on Linux -- over the host's own
# system packages -- and podman elsewhere (e.g. macOS) over a slim Linux image.
#
#   agtbox.py [-a DIR] [-v VOL] [-r VOL] [-t podman|bwrap] [-b] <claude|opencode|codex|bash> [-- agent args...]
#
# Launcher flags are parsed with argparse; the agent's own args go AFTER a `--`
# separator and are passed VERBATIM (e.g. `agtbox.py claude -- --resume`). A bare
# `agtbox.py claude` (no agent args) needs no `--`. The final engine command
# REPLACES this process via os.execvp. Argv is always built as a list -- there is
# never a host shell, except the deliberate `bash -c <install-script>`: a verbatim
# bash script run INSIDE the sandbox (passed as argv to the engine, not a host shell).

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = os.environ.get("HOME")
if not HOME:
    print("Error: HOME is not set.", file=sys.stderr)
    sys.exit(1)

# Persistent per-user dirs (all under $HOME, bound into the sandbox). The
# toolchain plus every global install the agents make live here and persist
# across runs; the rest of $HOME is an empty tmpfs inside the sandbox, so the
# agents can only write to these dirs (and the project) -- not the real home.
# The toolchain is namespaced by CPU arch (.../agent-box/<uname -m>) so ONE shared
# filesystem can serve nodes of different architectures -- e.g. an HPC cluster whose
# x86_64 and aarch64 nodes mount the same $HOME. node/uv/the CLIs/gh/glab and every
# global install are arch-specific native binaries; pip/uv/npm install for the current
# platform into one flat prefix and don't namespace by arch, so a single shared tree
# would hand the wrong-arch binaries to the other node (Exec format error). Each arch
# gets its own toolchain + .stamp here. Config/state/cache below stay un-namespaced --
# they're arch-independent, so logins/history/caches are shared across arches.
ARCH = os.uname().machine                                # x86_64, aarch64, ...
AGENT_TOOLS = f"{HOME}/.local/share/agent-box/{ARCH}"    # node, npm, uv, the CLIs, global installs (rw, per-arch)
AGENT_CONFIG = f"{HOME}/.config/agent-box"        # per-tool config (rw)
AGENT_STATE = f"{HOME}/.local/state/agent-box"    # per-tool state (rw)
AGENT_CACHE = f"{HOME}/.cache/agent-box"          # npm/pip/uv + per-tool caches (rw)
NPM_PKGS = ["@anthropic-ai/claude-code", "opencode-ai", "@openai/codex"]

# Repo root (the podman engine's Containerfile lives under container/) and the
# name of the image that engine builds and runs.
PROJ_DIR = str(Path(__file__).resolve().parent.parent)
IMAGE = "agent-box"

# Per-tool state wiring: "<host source path>:<path inside the sandbox>". Each host
# dir is bound straight onto the path the tool looks for, namespaced under the
# matching XDG base: config -> AGENT_CONFIG, data -> AGENT_TOOLS, state -> AGENT_STATE,
# disposable cache -> AGENT_CACHE.
BIND_DIRS = [
    f"{AGENT_CONFIG}/claude:{HOME}/.claude",
    f"{AGENT_CONFIG}/codex:{HOME}/.codex",
    f"{AGENT_CONFIG}/opencode:{HOME}/.config/opencode",
    f"{AGENT_TOOLS}/opencode:{HOME}/.local/share/opencode",
    f"{AGENT_STATE}/opencode:{HOME}/.local/state/opencode",
    f"{AGENT_CACHE}/opencode:{HOME}/.cache/opencode",
    f"{AGENT_CONFIG}/git:{HOME}/.config/git",
    f"{AGENT_CONFIG}/gh:{HOME}/.config/gh",
    f"{AGENT_CONFIG}/glab:{HOME}/.config/glab-cli",
    f"{AGENT_CONFIG}/ssh:{HOME}/.ssh",
]
# Single config files bound straight in (seeded "{}" if absent -- claude.json must
# be valid JSON). NB: a *file* bind can't be rewritten via temp+rename (EBUSY on
# the mountpoint), which is why git uses the ~/.config/git DIR bind above instead.
BIND_FILES = [f"{AGENT_CONFIG}/claude.json:{HOME}/.claude.json"]
# Empty files seeded INSIDE an already-bound dir (not separately bound), so the
# tool writes them natively: git only targets ~/.config/git/config for --global
# writes if it already exists, and its lock+rename then stays inside the bound dir.
SEED_FILES = [f"{AGENT_CONFIG}/git/config"]

# The sandbox runs with --clearenv (see bwrap_common), so ONLY these reach the
# agent -- a real allowlist, not the host's whole environment (which would leak
# any exported secret). ENV_FORWARD is forwarded when set (auth/model config;
# terminal/locale so the TUIs render; proxy vars for installs/APIs behind a
# proxy); ENV_LITERAL is always applied.
ENV_FORWARD = [
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "OPENAI_BASE_URL", "OPENAI_API_KEY", "CODEX_API_KEY",
    "OPENCODE_ENABLE_EXA",   # opencode's Exa web search: off by default, on if you export it
    "TERM", "COLORTERM", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
]
ENV_LITERAL = [
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1",
    "OPENCODE_EXPERIMENTAL_LSP_TOOL=true",
]

# The environment BOTH engines set: HOME + PATH + routing every package manager's
# global installs into the persistent toolchain and its caches into AGENT_CACHE.
# Kept as "K=V" strings from one source so bwrap (--setenv K V) and podman (-e K=V)
# can't drift apart.
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

# ---- runtime state (the bash globals set during main/launch) ----

PROG = os.path.basename(sys.argv[0])
APP_DIR = os.getcwd()
VOLUMES: list[str] = []
RO_VOLUMES: list[str] = []
ENGINE = ""
REBUILD = False
TOOL = ""
EXTRA_ARGS: list[str] = []
AGENT_BIN = ""


def _split_pair(pair):
    """Split a "src:dst" bind-table entry. Mirrors bash ${e%%:*} / ${e#*:}:
    first ':' separates source from destination (paths have no ':')."""
    src, _, dst = pair.partition(":")
    return src, dst


def _kv(entry):
    """Split a "K=V" env entry. Mirrors bash ${kv%%=*} / ${kv#*=}."""
    key, _, val = entry.partition("=")
    return key, val


# ---- engine-agnostic arg emitters --------------------------------------------
# bwrap and podman take the same env/binds in different shapes; these emit either
# from one source so the two run paths can't drift. style="bwrap" -> --setenv K V
# / --bind src dst (--ro-bind for read-only); style="podman" -> -e K=V / -v src:dst
# (:ro for read-only).

def _fmt_env(style, pairs):
    """Format (key, value) env pairs as launcher args for the given engine style."""
    args = []
    for key, val in pairs:
        args += ["--setenv", key, val] if style == "bwrap" else ["-e", f"{key}={val}"]
    return args


def env_args(style):
    """The env allowlist both engines apply at run time: AGENT_ENV and ENV_LITERAL
    always; ENV_FORWARD only for vars actually set on the host (an unset/empty var
    must not shadow mounted config)."""
    pairs = [_kv(e) for e in AGENT_ENV]
    pairs += [(v, os.environ[v]) for v in ENV_FORWARD if os.environ.get(v)]
    pairs += [_kv(e) for e in ENV_LITERAL]
    return _fmt_env(style, pairs)


def bind_args(style):
    """The per-tool config binds + extra -v/-r volumes both engines apply (NOT the
    toolchain/cache/project binds, which are part of each engine's own base). Config
    dirs/files and -v volumes are read-write; -r volumes read-only."""
    args = []
    for e in BIND_DIRS + BIND_FILES:
        src, dst = _split_pair(e)
        args += ["--bind", src, dst] if style == "bwrap" else ["-v", f"{src}:{dst}"]
    for m in VOLUMES:
        args += ["--bind", m, m] if style == "bwrap" else ["-v", f"{m}:{m}"]
    for m in RO_VOLUMES:
        args += ["--ro-bind", m, m] if style == "bwrap" else ["-v", f"{m}:{m}:ro"]
    return args


def _agt_env(narch, goarch):
    """The AGT_* inputs the install here-doc reads (as (key, value) pairs)."""
    return [
        ("AGT_TOOLS", AGENT_TOOLS),
        ("AGT_NARCH", narch),
        ("AGT_GOARCH", goarch),
        ("AGT_NPM_PKGS", " ".join(NPM_PKGS)),
    ]


def detect_engine():
    """Pick the sandbox engine. -t forces it (validated by argparse choices);
    otherwise prefer bwrap (on Linux: no image to build, faster start, tighter
    per-mount read-only binds) and fall back to podman (macOS, or a Linux host
    without bwrap)."""
    global ENGINE
    if not ENGINE:
        if shutil.which("bwrap"):
            ENGINE = "bwrap"
        elif shutil.which("podman"):
            ENGINE = "podman"
        else:
            print("Error: no sandbox engine found (need bwrap or podman).", file=sys.stderr)
            sys.exit(1)
    if not shutil.which(ENGINE):
        print(f"Error: engine '{ENGINE}' is selected but not installed.", file=sys.stderr)
        sys.exit(1)


def normalize_paths():
    """Resolve APP_DIR and every extra volume to an absolute path."""
    global APP_DIR, VOLUMES, RO_VOLUMES
    APP_DIR = os.path.realpath(APP_DIR)
    VOLUMES = [os.path.realpath(v) for v in VOLUMES]
    RO_VOLUMES = [os.path.realpath(v) for v in RO_VOLUMES]
    # A bind source must exist or the engine fails with an opaque error -- check
    # up front and fail with a clear message instead.
    for p in [APP_DIR, *VOLUMES, *RO_VOLUMES]:
        if not os.path.exists(p):
            print(f"Error: path does not exist: {p}", file=sys.stderr)
            sys.exit(1)
    # A path given as BOTH -v (rw) and -r (ro) would be a duplicate bind target and
    # bwrap would reject it. Read-write wins -- drop it from RO_VOLUMES and warn.
    kept = []
    for ro in RO_VOLUMES:
        if ro in VOLUMES:
            print(f"Warning: {ro} given as both -v (rw) and -r (ro); binding read-write.", file=sys.stderr)
        else:
            kept.append(ro)
    RO_VOLUMES = kept


def ensure_sources():
    """Create the host-side bind sources so a fresh user can launch: the toolchain +
    cache dirs, the per-tool config dirs, the seed JSON files, and the empty
    seed-only files (git config). Never clobbers existing config."""
    for d in (AGENT_TOOLS, f"{AGENT_CACHE}/npm", f"{AGENT_CACHE}/pip", f"{AGENT_CACHE}/uv"):
        os.makedirs(d, exist_ok=True)
    for e in BIND_DIRS:
        os.makedirs(_split_pair(e)[0], exist_ok=True)
    # ssh refuses keys/config in a group/world-accessible ~/.ssh -- keep the source 0700
    # so the bind carries through perms ssh will accept (private keys still need 0600).
    os.chmod(f"{AGENT_CONFIG}/ssh", 0o700)
    for e in BIND_FILES:
        f = _split_pair(e)[0]
        os.makedirs(os.path.dirname(f), exist_ok=True)
        if not os.path.exists(f):
            with open(f, "w") as fh:
                fh.write("{}")
    for f in SEED_FILES:
        os.makedirs(os.path.dirname(f), exist_ok=True)
        if not os.path.exists(f):
            with open(f, "w"):
                pass


def install_script():
    """The toolchain install script: a quoted here-doc (NO launcher-side expansion)
    that reads its inputs from the AGT_* env vars the runner sets (avoids
    nested-quoting hazards). Shared by both engines, and always targets linux -- the
    toolchain only ever RUNS inside Linux (the bwrap sandbox, or the podman Linux
    container), even when the host is macOS. Idempotent: re-running upgrades in
    place. node tracks the latest LTS, gh/glab their latest releases. Needs
    curl/tar/xz/python3 (from the host under bwrap; from the image under podman)."""
    # A verbatim bash script -- run via the engine's `bash -c`, NOT rewritten in
    # Python. A plain raw string (no f-string substitutions): the install reads its
    # inputs from the AGT_* env vars the runner sets, with no launcher-side expansion.
    return r'''set -euo pipefail
mkdir -p "${AGT_TOOLS}/node" "${AGT_TOOLS}/bin"

echo 'Agent Box: installing node (latest LTS)...' >&2
nv=$(curl -fsSL https://nodejs.org/dist/index.json \
  | python3 -c 'import json,sys; print(next(r["version"] for r in json.load(sys.stdin) if r["lts"]))')
curl -fsSL "https://nodejs.org/dist/${nv}/node-${nv}-linux-${AGT_NARCH}.tar.xz" \
  | tar -xJ --strip-components=1 -C "${AGT_TOOLS}/node"

echo 'Agent Box: installing the agent CLIs...' >&2
"${AGT_TOOLS}/node/bin/npm" install -g --prefix "${AGT_TOOLS}" --no-fund --no-audit ${AGT_NPM_PKGS}

# node + the agent CLIs above are REQUIRED (fail hard). uv, gh, glab are auxiliary --
# the agents run without them -- and on locked-down networks their download hosts
# (astral.sh / github.com / gitlab.com) may be blocked while the node + npm registries
# are allowed. So install each best-effort: a failure warns and continues instead of
# aborting the whole toolchain (which would also skip the .stamp below and re-download
# everything on the next run). Each is an explicit && chain so a mid-step failure stops
# that tool cleanly; the trailing || records it. Retry later with AGTBOX_REINSTALL=1.
skipped=

echo 'Agent Box: installing uv...' >&2
{ curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR="${AGT_TOOLS}/bin" UV_NO_MODIFY_PATH=1 sh; } \
  || { echo 'Agent Box: WARNING -- uv install failed (astral.sh blocked?); skipping.' >&2; skipped="${skipped} uv"; }

echo 'Agent Box: installing gh (GitHub CLI)...' >&2
{ gv=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest \
       | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])') \
  && curl -fsSL "https://github.com/cli/cli/releases/download/${gv}/gh_${gv#v}_linux_${AGT_GOARCH}.tar.gz" \
       | tar -xz -C /tmp \
  && install -m755 "/tmp/gh_${gv#v}_linux_${AGT_GOARCH}/bin/gh" "${AGT_TOOLS}/bin/gh"; } \
  || { echo 'Agent Box: WARNING -- gh install failed (github.com blocked?); skipping.' >&2; skipped="${skipped} gh"; }

echo 'Agent Box: installing glab (GitLab CLI)...' >&2
{ lv=$(curl -fsSL https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases \
       | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["tag_name"])') \
  && curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/${lv}/downloads/glab_${lv#v}_linux_${AGT_GOARCH}.tar.gz" \
       | tar -xz -C /tmp \
  && install -m755 /tmp/bin/glab "${AGT_TOOLS}/bin/glab"; } \
  || { echo 'Agent Box: WARNING -- glab install failed (gitlab.com blocked?); skipping.' >&2; skipped="${skipped} glab"; }

if [ -n "${skipped}" ]; then
  echo "Agent Box: core toolchain ready; skipped:${skipped} (retry later with AGTBOX_REINSTALL=1)." >&2
fi

date > "${AGT_TOOLS}/.stamp"
'''


def arch_pair(machine):
    """Map a `uname -m` value to the (NARCH, GOARCH) release-tarball arch names
    (node.js, and gh/glab respectively). Errors + exits 1 on an unsupported arch,
    mirroring the bash `return 1` followed by `|| exit 1` at every call site."""
    if machine == "aarch64":
        return "arm64", "arm64"
    if machine == "x86_64":
        return "x64", "amd64"
    print(f"Error: unsupported architecture '{machine}'.", file=sys.stderr)
    sys.exit(1)


def bwrap_common():
    """Build and return the bwrap args shared by install + run: the locked-down
    system binds (read-only), an empty tmpfs $HOME, the persistent toolchain +
    cache (read-write), shared network, and -- starting from a wiped env
    (--clearenv, so no host secrets leak) -- the env (PATH + npm/pip/uv routing into
    the persistent dirs + the env union/allowlist)."""
    # /etc is bound read-only, but /etc/resolv.conf is commonly a symlink into /run
    # (systemd-resolved, SLES netconfig) -- and /run is NOT in the sandbox, so the link
    # dangles and every lookup fails ("Could not resolve host"). Bind the RESOLVED file
    # at its OWN real path (e.g. /run/netconfig/resolv.conf), NOT onto /etc/resolv.conf:
    # binding onto the symlink makes bwrap follow it to the missing /run target and die
    # with "Can't create file at /etc/resolv.conf: No such file or directory". Recreating
    # the file at its real path instead lets the symlink already inside the bound /etc
    # resolve. realpath() canonicalises the link; --ro-bind-try skips it if absent on the
    # host. A plain-file resolv.conf realpaths to /etc/resolv.conf -- a harmless rebind.
    resolv = os.path.realpath("/etc/resolv.conf")
    bw = [
        "--clearenv",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
        "--ro-bind-try", resolv, resolv,
        # Same /etc-symlink-into-an-unbound-dir trap for the TLS trust store: on SLES/
        # openSUSE the CA bundle lives in /var/lib/ca-certificates and /etc/ssl/{certs,
        # ca-bundle.pem} are symlinks into it, so without /var the sandbox has NO CAs and
        # every HTTPS fetch fails "unable to get local issuer certificate". Bind the store
        # so those symlinks resolve. --ro-bind-try: absent on distros that keep certs under
        # /etc or /usr (already bound), so it's skipped there.
        "--ro-bind-try", "/var/lib/ca-certificates", "/var/lib/ca-certificates",
        "--ro-bind-try", "/bin", "/bin", "--ro-bind-try", "/sbin", "/sbin", "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64", "--ro-bind-try", "/opt", "/opt", "--ro-bind-try", "/cpe", "/cpe",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        "--tmpfs", HOME,
        "--bind", AGENT_TOOLS, AGENT_TOOLS,
        "--bind", AGENT_CACHE, AGENT_CACHE,
        "--die-with-parent", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
    ]
    bw += env_args("bwrap")
    return bw


def install_via_bwrap():
    """Install the toolchain from INSIDE a bwrap sandbox (toolchain + cache rw,
    system ro, $HOME tmpfs) so the installer scripts can't touch the host. Arch is
    the host's -- bwrap is Linux-only, so host arch == run arch."""
    script = install_script()
    narch, goarch = arch_pair(os.uname().machine)
    bw = bwrap_common()
    bw += _fmt_env("bwrap", _agt_env(narch, goarch))
    subprocess.run(["bwrap", *bw, "--", "/usr/bin/bash", "-c", script], check=True)


def install_via_podman():
    """Install the toolchain from INSIDE the podman image (toolchain + cache rw);
    the container is itself the isolation. Arch comes from the IMAGE, not the host --
    a macOS host is a different OS/arch from the Linux container that runs the
    toolchain."""
    script = install_script()
    container_arch = subprocess.run(
        ["podman", "run", "--rm", IMAGE, "uname", "-m"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    narch, goarch = arch_pair(container_arch)
    # No --userns=keep-id: rootless podman already maps the container's root to the
    # invoking host user, so files written to the mounted toolchain are owned by you
    # AND $HOME is writable -- which opencode's postinstall needs (it runs the freshly
    # downloaded binary, which mkdir's ~/.local/share/opencode as a self-check).
    pd = [
        "run", "--rm", "--security-opt", "label=disable",
        "-v", f"{AGENT_TOOLS}:{AGENT_TOOLS}",
        "-v", f"{AGENT_CACHE}:{AGENT_CACHE}",
    ]
    # Same env routing as a real run -- HOME/PATH so npm's `env node` shebang
    # resolves, and npm/pip/uv cache+prefix pointed at the mounted dirs (else npm
    # falls back to an unwritable ~/.npm) -- plus the install script's AGT_* inputs.
    # (AGENT_ENV only, as the bash does -- not the ENV_FORWARD/ENV_LITERAL allowlist.)
    pd += _fmt_env("podman", [_kv(e) for e in AGENT_ENV] + _agt_env(narch, goarch))
    subprocess.run(["podman", *pd, "--", IMAGE, "/usr/bin/bash", "-c", script], check=True)


def install_tools():
    """Install the toolchain via the selected engine."""
    if ENGINE == "bwrap":
        install_via_bwrap()
    elif ENGINE == "podman":
        install_via_podman()


def ensure_tools():
    """Install the toolchain on first use (or when AGTBOX_REINSTALL=1). The .stamp
    is written only after a fully successful install, so a missing stamp means a
    fresh or interrupted install and we (re)run; we also re-install if the requested
    tool's binary is absent.

    That last check is PRESENCE only (`os.path.lexists`): the binary runs inside the
    sandbox, never on the host, so the host must not test its executability. On macOS
    the toolchain lives across the podman-machine VM mount, where the host can't
    follow the npm bin symlink or see its exec bit -- `os.path.isfile`/`os.access(X_OK)`
    came back false and it reinstalled the toolchain on every single run."""
    if (os.environ.get("AGTBOX_REINSTALL") == "1"
            or not os.path.exists(f"{AGENT_TOOLS}/.stamp")
            or not os.path.lexists(AGENT_BIN)):
        print(f"Agent Box: setting up the toolchain in {AGENT_TOOLS} (one-time)...", file=sys.stderr)
        install_tools()


def run_bwrap():
    """Build the bwrap run argv (as a list, no eval) and exec it (replacing this
    process, like bash `exec`)."""
    bw = bwrap_common()
    bw += ["--bind", APP_DIR, APP_DIR, "--chdir", APP_DIR]
    bw += bind_args("bwrap")
    # `--` ends bwrap's option parsing, so a `-`-leading tool arg can't be misread.
    argv = ["bwrap", *bw, "--", AGENT_BIN, *EXTRA_ARGS]
    os.execvp("bwrap", argv)


def refresh_certs():
    """Copy the host's company CA certs into container/certs/ so the image build can
    bake them into its trust store -- needed behind a TLS-intercepting proxy, since
    the podman image starts from a fresh Debian trust store that lacks them. Source
    defaults to ~/.local/share/certs, overridable via AGENT_CERTS_DIR; missing/empty
    is fine (the cp is best-effort). podman engine only -- bwrap reuses the host's
    /etc trust store via its --ro-bind /etc, so it needs no cert logic."""
    src = os.environ.get("AGENT_CERTS_DIR") or f"{HOME}/.local/share/certs"
    dst = f"{PROJ_DIR}/container/certs"
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    # Best-effort `cp "${src}"/* "${dst}/"`: missing/empty source is fine.
    try:
        for name in os.listdir(src):
            # Match bash `cp "${src}"/* "${dst}/"`: no dotglob (skip dotfiles) and
            # a non-recursive cp (skip dirs) -- certs are flat *.crt files.
            if name.startswith("."):
                continue
            s = os.path.join(src, name)
            if not os.path.isfile(s):
                continue
            try:
                shutil.copy2(s, dst)
            except OSError:
                pass
    except OSError:
        pass


def build_image():
    """Lazily build the podman image (the read-only rootfs). Built on first use and
    rebuilt with -b. node/uv/the CLIs are NOT baked in -- they come from the bound
    toolchain (~/.local/share/agent-box), so a rebuild never disturbs them."""
    if REBUILD:
        subprocess.run(["podman", "image", "rm", IMAGE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    exists = subprocess.run(["podman", "image", "exists", IMAGE]).returncode == 0
    if not exists:
        print(f"Agent Box: building the {IMAGE} image (one-time)...", file=sys.stderr)
        refresh_certs()
        subprocess.run(
            ["podman", "build", "-t", IMAGE, "-f",
             f"{PROJ_DIR}/container/Containerfile", f"{PROJ_DIR}/container"],
            check=True,
        )


def derive_tz():
    """A fresh podman container's clock is UTC -- bwrap inherits host-local time via
    its /etc bind, but podman has no host /etc/localtime. Pass the host zone as TZ
    instead of binding /etc/localtime (portable to macOS, where that symlink points
    into a different tree but the IANA name after zoneinfo/ is the same). Appends to
    ENV_LITERAL so it flows through the engine's env loop. podman branch only."""
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        return
    marker = "/zoneinfo/"
    idx = link.rfind(marker)
    if idx != -1:
        tz = link[idx + len(marker):]
        if tz:
            ENV_LITERAL.append(f"TZ={tz}")


def run_podman():
    """Build the podman run argv (list, no eval) and exec it (replacing this process,
    like bash `exec`). Everything is mounted at the SAME host path and HOME is the
    host's, so the bind tables + AGENT_ENV + AGENT_BIN + PATH are reused verbatim
    from the bwrap path (--bind a b -> -v a:b, --ro-bind a b -> -v a:b:ro, --setenv
    K V -> -e K=V). The image is just the read-only rootfs; the toolchain comes from
    the bound ${AGENT_TOOLS} on PATH."""
    # No --userns=keep-id: rootless podman maps the container's root to the invoking
    # host user, so files the agent writes (project + bound dirs) are owned by you and
    # $HOME is writable. (keep-id would map you to uid 1000 inside, leaving $HOME's
    # auto-created mount parents root-owned and unwritable -- which breaks tools that
    # touch an unbound path under $HOME.) label=disable so SELinux doesn't block the
    # binds (and we don't relabel the shared dirs with :z/:Z).
    pd = ["run", "-it", "--rm", "--security-opt", "label=disable"]
    pd += env_args("podman")
    pd += ["-v", f"{AGENT_TOOLS}:{AGENT_TOOLS}", "-v", f"{AGENT_CACHE}:{AGENT_CACHE}"]
    pd += ["-v", f"{APP_DIR}:{APP_DIR}", "-w", APP_DIR]
    pd += bind_args("podman")
    # `--` ends podman's option parsing before the image + command.
    argv = ["podman", *pd, "--", IMAGE, AGENT_BIN, *EXTRA_ARGS]
    os.execvp("podman", argv)


def launch():
    """Pick the engine, normalise paths, ensure sources + toolchain, then run. Under
    podman the image must exist before the toolchain install runs inside it."""
    detect_engine()
    if REBUILD and ENGINE != "podman":
        print("Warning: -b (rebuild) applies to the podman engine only; ignoring.", file=sys.stderr)
    normalize_paths()
    ensure_sources()
    if ENGINE == "podman":
        derive_tz()
        build_image()
        ensure_tools()
        run_podman()
    else:
        ensure_tools()
        run_bwrap()


def main():
    """Parse the launcher flags up to a `--` separator, then exec the agent with the
    args after it: `agtbox.py [flags] <tool> -- [agent args...]`. The `--` is only
    needed when passing args to the agent (a bare `agtbox.py <tool>` works too).
    Launcher flags are valid anywhere before `--`; everything after `--` is the
    agent's argv, untouched. argparse owns help/validation: `-h` -> stdout/exit 0,
    a bad flag/tool/engine -> stderr/exit 2."""
    global APP_DIR, VOLUMES, RO_VOLUMES, ENGINE, REBUILD, TOOL, EXTRA_ARGS, AGENT_BIN
    argv = sys.argv[1:]
    if "--" in argv:
        sep = argv.index("--")
        left, EXTRA_ARGS = argv[:sep], argv[sep + 1:]
    else:
        left, EXTRA_ARGS = argv, []

    p = argparse.ArgumentParser(
        prog=PROG, allow_abbrev=False,
        description="Run an AI coding agent (claude, opencode, codex) in an unprivileged "
                    "sandbox -- bwrap on Linux, else podman.",
        epilog="Pass arguments to the agent after `--`, e.g. `%(prog)s claude -- --resume`. "
               "The toolchain installs into ~/.local/share/agent-box on first use "
               "(AGTBOX_REINSTALL=1 forces a reinstall).")
    p.add_argument("-a", dest="app_dir", default=os.getcwd(), metavar="DIR",
                   help="project directory, bound at the same path inside (default: cwd)")
    p.add_argument("-v", dest="volumes", action="append", default=[], metavar="VOL",
                   help="extra dir, bound read-write at the same path (repeatable)")
    p.add_argument("-r", dest="ro_volumes", action="append", default=[], metavar="VOL",
                   help="extra dir, bound read-only at the same path (repeatable)")
    p.add_argument("-t", dest="engine", choices=("podman", "bwrap"),
                   help="engine (default: auto -- bwrap on Linux, else podman)")
    p.add_argument("-b", dest="rebuild", action="store_true",
                   help="rebuild the podman image (podman engine only)")
    p.add_argument("tool", choices=("claude", "opencode", "codex", "bash"),
                   help="the agent to run (or `bash` for an audit shell in the sandbox)")
    ns = p.parse_args(left)

    APP_DIR = ns.app_dir
    VOLUMES = ns.volumes
    RO_VOLUMES = ns.ro_volumes
    ENGINE = ns.engine or ""
    REBUILD = ns.rebuild
    TOOL = ns.tool
    # `bash` is the system shell (host /usr under bwrap, the image under podman), NOT
    # a per-user toolchain binary -- so it lives at /usr/bin/bash, not in AGENT_TOOLS.
    AGENT_BIN = "/usr/bin/bash" if TOOL == "bash" else f"{AGENT_TOOLS}/bin/{TOOL}"
    launch()


if __name__ == "__main__":
    main()
