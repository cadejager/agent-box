#!/usr/bin/env bash
# shellcheck shell=bash
#
# agtbox.sh -- run an AI coding agent (claude, opencode, codex) inside an
# unprivileged sandbox over a small per-user toolchain that is auto-installed on
# first use. Two engines: bubblewrap (bwrap) on Linux -- over the host's own
# system packages -- and podman elsewhere (e.g. macOS) over a slim Linux image.
#
#   agtbox.sh [-a DIR] [-v VOL] [-r VOL] [-t podman|bwrap] [-b] [-h] claude|opencode|codex [tool args...]
#
# Flags come BEFORE the tool name; everything AFTER it is passed to the tool
# VERBATIM (use the tool's own flags, e.g. `claude --resume`, `codex resume`).
# Requires bash >= 4 and either bwrap (bubblewrap) or podman.

set -eo pipefail

# Persistent per-user dirs (all under $HOME, bound into the sandbox). The
# toolchain plus every global install the agents make live here and persist
# across runs; the rest of $HOME is an empty tmpfs inside the sandbox, so the
# agents can only write to these dirs (and the project) -- not the real home.
AGENT_TOOLS="${HOME}/.local/share/agent-box"   # node, npm, uv, the CLIs, global installs (rw)
AGENT_CONFIG="${HOME}/.config/agent-box"        # per-tool config (rw)
AGENT_STATE="${HOME}/.local/state/agent-box"    # per-tool state (rw)
AGENT_CACHE="${HOME}/.cache/agent-box"          # npm/pip/uv + per-tool caches (rw)
NPM_PKGS=(@anthropic-ai/claude-code opencode-ai @openai/codex)

# Repo root (the podman engine's Containerfile lives under container/) and the
# name of the image that engine builds and runs.
PROJ_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
IMAGE="agent-box"

# Per-tool state wiring: "<host source path>:<path inside the sandbox>". Each host
# dir is bound straight onto the path the tool looks for, namespaced under the
# matching XDG base: config -> AGENT_CONFIG, data -> AGENT_TOOLS, state -> AGENT_STATE,
# disposable cache -> AGENT_CACHE.
BIND_DIRS=(
  "${AGENT_CONFIG}/claude:${HOME}/.claude"
  "${AGENT_CONFIG}/codex:${HOME}/.codex"
  "${AGENT_CONFIG}/opencode:${HOME}/.config/opencode"
  "${AGENT_TOOLS}/opencode:${HOME}/.local/share/opencode"
  "${AGENT_STATE}/opencode:${HOME}/.local/state/opencode"
  "${AGENT_CACHE}/opencode:${HOME}/.cache/opencode"
  "${AGENT_CONFIG}/git:${HOME}/.config/git"
  "${AGENT_CONFIG}/gh:${HOME}/.config/gh"
  "${AGENT_CONFIG}/glab:${HOME}/.config/glab-cli"
  "${AGENT_CONFIG}/ssh:${HOME}/.ssh"
)
# Single config files bound straight in (seeded "{}" if absent -- claude.json must
# be valid JSON). NB: a *file* bind can't be rewritten via temp+rename (EBUSY on
# the mountpoint), which is why git uses the ~/.config/git DIR bind above instead.
BIND_FILES=( "${AGENT_CONFIG}/claude.json:${HOME}/.claude.json" )
# Empty files seeded INSIDE an already-bound dir (not separately bound), so the
# tool writes them natively: git only targets ~/.config/git/config for --global
# writes if it already exists, and its lock+rename then stays inside the bound dir.
SEED_FILES=( "${AGENT_CONFIG}/git/config" )

# The sandbox runs with --clearenv (see bwrap_common), so ONLY these reach the
# agent -- a real allowlist, not the host's whole environment (which would leak
# any exported secret). ENV_FORWARD is forwarded when set (auth/model config;
# terminal/locale so the TUIs render; proxy vars for installs/APIs behind a
# proxy); ENV_LITERAL is always applied.
ENV_FORWARD=(
  ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_DEFAULT_SONNET_MODEL
  OPENAI_API_KEY CODEX_API_KEY
  TERM COLORTERM LANG LANGUAGE LC_ALL LC_CTYPE
  HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy
)
ENV_LITERAL=(
  CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
  OPENCODE_ENABLE_EXA=1
  OPENCODE_EXPERIMENTAL_LSP_TOOL=true
)

# The environment BOTH engines set: HOME + PATH + routing every package manager's
# global installs into the persistent toolchain and its caches into AGENT_CACHE.
# Kept as "K=V" strings from one source so bwrap (--setenv K V) and podman (-e K=V)
# can't drift apart.
AGENT_ENV=(
  "HOME=${HOME}"
  "PATH=${AGENT_TOOLS}/bin:${AGENT_TOOLS}/node/bin:/usr/bin:/bin"
  "npm_config_prefix=${AGENT_TOOLS}"
  "npm_config_cache=${AGENT_CACHE}/npm"
  "PIP_PREFIX=${AGENT_TOOLS}"
  "PYTHONUSERBASE=${AGENT_TOOLS}"
  "PIP_CACHE_DIR=${AGENT_CACHE}/pip"
  "PIP_BREAK_SYSTEM_PACKAGES=1"
  "UV_CACHE_DIR=${AGENT_CACHE}/uv"
  "UV_TOOL_DIR=${AGENT_TOOLS}/uv/tools"
  "UV_TOOL_BIN_DIR=${AGENT_TOOLS}/bin"
)

usage() {
  echo "Usage: ${0##*/} [-a DIR] [-v VOL] [-r VOL] [-t podman|bwrap] [-b] [-h] <claude|opencode|codex> [tool args...]"
  echo
  echo "  Run an AI coding agent in an unprivileged sandbox (bwrap on Linux, else"
  echo "  podman). Flags go BEFORE the tool name; everything after it is passed to"
  echo "  the tool verbatim (run '${0##*/} <tool> --help' for the tool's own help)."
  echo "  The toolchain is installed into ~/.local/share/agent-box on first use"
  echo "  (AGTBOX_REINSTALL=1 forces a reinstall)."
  echo
  echo "  Flags:"
  echo "    -a DIR   Project directory, bound at the same path inside (default: cwd)"
  echo "    -v VOL   Extra dir, bound read-write at the same path (repeatable)"
  echo "    -r VOL   Extra dir, bound read-only at the same path (repeatable)"
  echo "    -t ENG   Engine: podman or bwrap (default: auto -- bwrap on Linux, else podman)"
  echo "    -b       Rebuild the podman image (podman engine only)"
  echo "    -h       Show this help"
  exit 1
}

# Pick the sandbox engine. Force it with -t; otherwise prefer bwrap (on Linux: no
# image to build, faster start, tighter per-mount read-only binds) and fall back to
# podman (macOS, or a Linux host without bwrap).
agent::detect_engine() {
  if [[ -n "${ENGINE}" ]]; then
    case "${ENGINE}" in
      bwrap|podman) ;;
      *) echo "Error: unknown engine '${ENGINE}' (expected podman or bwrap)." >&2; usage ;;
    esac
  elif command -v bwrap >/dev/null 2>&1; then
    ENGINE=bwrap
  elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
  else
    echo "Error: no sandbox engine found (need bwrap or podman)." >&2
    exit 1
  fi
  command -v "${ENGINE}" >/dev/null 2>&1 || {
    echo "Error: engine '${ENGINE}' is selected but not installed." >&2
    exit 1
  }
}

# Resolve APP_DIR and every extra volume to an absolute path.
agent::normalize_paths() {
  APP_DIR=$(realpath "${APP_DIR}")
  local i j
  for i in "${!VOLUMES[@]}"; do
    VOLUMES[i]=$(realpath "${VOLUMES[i]}")
  done
  for i in "${!RO_VOLUMES[@]}"; do
    RO_VOLUMES[i]=$(realpath "${RO_VOLUMES[i]}")
  done
  # A path given as BOTH -v (rw) and -r (ro) would be a duplicate bind target and
  # bwrap would reject it. Read-write wins -- drop it from RO_VOLUMES and warn.
  local kept=()
  for i in "${!RO_VOLUMES[@]}"; do
    local dup=false
    for j in "${!VOLUMES[@]}"; do
      if [[ "${RO_VOLUMES[i]}" == "${VOLUMES[j]}" ]]; then
        dup=true
        break
      fi
    done
    if [[ "${dup}" == "true" ]]; then
      echo "Warning: ${RO_VOLUMES[i]} given as both -v (rw) and -r (ro); binding read-write." >&2
    else
      kept+=("${RO_VOLUMES[i]}")
    fi
  done
  RO_VOLUMES=("${kept[@]}")
}

# Create the host-side bind sources so a fresh user can launch: the toolchain +
# cache dirs, the per-tool config dirs, the seed JSON files, and the empty
# seed-only files (git config). Never clobbers existing config.
agent::ensure_sources() {
  mkdir -p "${AGENT_TOOLS}" "${AGENT_CACHE}/npm" "${AGENT_CACHE}/pip" "${AGENT_CACHE}/uv"
  local e f
  for e in "${BIND_DIRS[@]}"; do
    mkdir -p "${e%%:*}"
  done
  # ssh refuses keys/config in a group/world-accessible ~/.ssh -- keep the source 0700
  # so the bind carries through perms ssh will accept (private keys still need 0600).
  chmod 700 "${AGENT_CONFIG}/ssh"
  for e in "${BIND_FILES[@]}"; do
    f="${e%%:*}"
    mkdir -p "$(dirname "${f}")"
    [[ -e "${f}" ]] || printf '{}' > "${f}"
  done
  for f in "${SEED_FILES[@]}"; do
    mkdir -p "$(dirname "${f}")"
    [[ -e "${f}" ]] || : > "${f}"
  done
}

# Populate the global BW array with the bwrap args shared by install + run: the
# locked-down system binds (read-only), an empty tmpfs $HOME, the persistent
# toolchain + cache (read-write), shared network, and -- starting from a wiped
# env (--clearenv, so no host secrets leak) -- the env (PATH + npm/pip/uv routing
# into the persistent dirs + the env union/allowlist).
agent::bwrap_common() {
  BW=(
    --clearenv
    --ro-bind /usr /usr  --ro-bind /etc /etc
    --ro-bind-try /bin /bin     --ro-bind-try /sbin /sbin  --ro-bind-try /lib /lib
    --ro-bind-try /lib64 /lib64  --ro-bind-try /opt /opt  --ro-bind-try /cpe /cpe
    --dev /dev  --proc /proc  --tmpfs /tmp
    --tmpfs "${HOME}"
    --bind "${AGENT_TOOLS}" "${AGENT_TOOLS}"
    --bind "${AGENT_CACHE}" "${AGENT_CACHE}"
    --die-with-parent  --unshare-pid  --unshare-ipc  --unshare-uts
  )
  local var kv
  for kv in "${AGENT_ENV[@]}"; do BW+=(--setenv "${kv%%=*}" "${kv#*=}"); done
  for var in "${ENV_FORWARD[@]}"; do
    [[ -n "${!var:-}" ]] && BW+=(--setenv "${var}" "${!var}")
  done
  for kv in "${ENV_LITERAL[@]}"; do
    BW+=(--setenv "${kv%%=*}" "${kv#*=}")
  done
}

# The toolchain install script: a quoted here-doc (NO launcher-side expansion) that
# reads its inputs from the AGT_* env vars the runner sets (avoids nested-quoting
# hazards). Shared by both engines, and always targets linux -- the toolchain only
# ever RUNS inside Linux (the bwrap sandbox, or the podman Linux container), even
# when the host is macOS. Idempotent: re-running upgrades in place. node tracks the
# latest LTS, gh/glab their latest releases. Needs curl/tar/xz/python3 (from the
# host under bwrap; from the image under podman).
agent::install_script() {
  cat <<'INSTALL'
set -euo pipefail
mkdir -p "${AGT_TOOLS}/node" "${AGT_TOOLS}/bin"

echo 'Agent Box: installing node (latest LTS)...' >&2
nv=$(curl -fsSL https://nodejs.org/dist/index.json \
  | python3 -c 'import json,sys; print(next(r["version"] for r in json.load(sys.stdin) if r["lts"]))')
curl -fsSL "https://nodejs.org/dist/${nv}/node-${nv}-linux-${AGT_NARCH}.tar.xz" \
  | tar -xJ --strip-components=1 -C "${AGT_TOOLS}/node"

echo 'Agent Box: installing the agent CLIs...' >&2
"${AGT_TOOLS}/node/bin/npm" install -g --prefix "${AGT_TOOLS}" --no-fund --no-audit ${AGT_NPM_PKGS}

echo 'Agent Box: installing uv...' >&2
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="${AGT_TOOLS}/bin" UV_NO_MODIFY_PATH=1 sh

echo 'Agent Box: installing gh (GitHub CLI)...' >&2
gv=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')
curl -fsSL "https://github.com/cli/cli/releases/download/${gv}/gh_${gv#v}_linux_${AGT_GOARCH}.tar.gz" \
  | tar -xz -C /tmp
install -m755 "/tmp/gh_${gv#v}_linux_${AGT_GOARCH}/bin/gh" "${AGT_TOOLS}/bin/gh"

echo 'Agent Box: installing glab (GitLab CLI)...' >&2
lv=$(curl -fsSL https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["tag_name"])')
curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/${lv}/downloads/glab_${lv#v}_linux_${AGT_GOARCH}.tar.gz" \
  | tar -xz -C /tmp
install -m755 /tmp/bin/glab "${AGT_TOOLS}/bin/glab"

date > "${AGT_TOOLS}/.stamp"
INSTALL
}

# Map `uname -m` to the (node, go) arch names the node.js and gh/glab release
# tarballs use, printed as "<narch> <goarch>". $1 = a `uname -m` value.
agent::arch_pair() {
  case "$1" in
    aarch64) echo "arm64 arm64" ;;
    x86_64)  echo "x64 amd64" ;;
    *) echo "Error: unsupported architecture '$1'." >&2; return 1 ;;
  esac
}

# Install the toolchain from INSIDE a bwrap sandbox (toolchain + cache rw, system
# ro, $HOME tmpfs) so the installer scripts can't touch the host. Arch is the host's
# -- bwrap is Linux-only, so host arch == run arch.
agent::install_via_bwrap() {
  local script pair narch goarch
  script=$(agent::install_script)
  pair=$(agent::arch_pair "$(uname -m)") || exit 1
  narch="${pair%% *}"; goarch="${pair##* }"
  agent::bwrap_common
  bwrap "${BW[@]}" \
    --setenv AGT_TOOLS "${AGENT_TOOLS}" \
    --setenv AGT_NARCH "${narch}" \
    --setenv AGT_GOARCH "${goarch}" \
    --setenv AGT_NPM_PKGS "${NPM_PKGS[*]}" \
    -- /usr/bin/bash -c "${script}"
}

# Install the toolchain from INSIDE the podman image (toolchain + cache rw); the
# container is itself the isolation. Arch comes from the IMAGE, not the host -- a
# macOS host is a different OS/arch from the Linux container that runs the toolchain.
agent::install_via_podman() {
  local script pair narch goarch kv
  script=$(agent::install_script)
  pair=$(agent::arch_pair "$(podman run --rm "${IMAGE}" uname -m)") || exit 1
  narch="${pair%% *}"; goarch="${pair##* }"
  # No --userns=keep-id: rootless podman already maps the container's root to the
  # invoking host user, so files written to the mounted toolchain are owned by you
  # AND $HOME is writable -- which opencode's postinstall needs (it runs the freshly
  # downloaded binary, which mkdir's ~/.local/share/opencode as a self-check).
  local PD=(run --rm --security-opt label=disable
    -v "${AGENT_TOOLS}:${AGENT_TOOLS}"
    -v "${AGENT_CACHE}:${AGENT_CACHE}")
  # Same env routing as a real run -- HOME/PATH so npm's `env node` shebang
  # resolves, and npm/pip/uv cache+prefix pointed at the mounted dirs (else npm
  # falls back to an unwritable ~/.npm) -- plus the install script's AGT_* inputs.
  for kv in "${AGENT_ENV[@]}"; do PD+=(-e "${kv}"); done
  PD+=(
    -e "AGT_TOOLS=${AGENT_TOOLS}"
    -e "AGT_NARCH=${narch}"
    -e "AGT_GOARCH=${goarch}"
    -e "AGT_NPM_PKGS=${NPM_PKGS[*]}"
  )
  podman "${PD[@]}" -- "${IMAGE}" /usr/bin/bash -c "${script}"
}

# Install the toolchain via the selected engine.
agent::install_tools() {
  case "${ENGINE}" in
    bwrap)  agent::install_via_bwrap ;;
    podman) agent::install_via_podman ;;
  esac
}

# Install the toolchain on first use (or when AGTBOX_REINSTALL=1). The .stamp
# is written only after a fully successful install, so a missing stamp means a
# fresh or interrupted install and we (re)run; the AGENT_BIN check also re-installs
# if the requested tool's binary went missing.
agent::ensure_tools() {
  if [[ "${AGTBOX_REINSTALL:-}" == "1" \
     || ! -e "${AGENT_TOOLS}/.stamp" \
     || ! -x "${AGENT_BIN}" ]]; then
    echo "Agent Box: setting up the toolchain in ${AGENT_TOOLS} (one-time)..." >&2
    agent::install_tools
  fi
}

# Build the bwrap run argv (as an array, no eval) and exec it.
agent::run_bwrap() {
  agent::bwrap_common
  BW+=(--bind "${APP_DIR}" "${APP_DIR}" --chdir "${APP_DIR}")
  local e m
  for e in "${BIND_DIRS[@]}";  do BW+=(--bind "${e%%:*}" "${e#*:}"); done
  for e in "${BIND_FILES[@]}"; do BW+=(--bind "${e%%:*}" "${e#*:}"); done
  for m in "${VOLUMES[@]}";    do BW+=(--bind    "${m}" "${m}"); done
  for m in "${RO_VOLUMES[@]}"; do BW+=(--ro-bind "${m}" "${m}"); done
  # `--` ends bwrap's option parsing, so a `-`-leading tool arg can't be misread.
  exec bwrap "${BW[@]}" -- "${AGENT_BIN}" "${EXTRA_ARGS[@]}"
}

# Copy the host's company CA certs into container/certs/ so the image build can
# bake them into its trust store -- needed behind a TLS-intercepting proxy, since
# the podman image starts from a fresh Debian trust store that lacks them. Source
# defaults to ~/.local/share/certs, overridable via AGENT_CERTS_DIR; missing/empty
# is fine (the cp is best-effort). podman engine only -- bwrap reuses the host's
# /etc trust store via its --ro-bind /etc, so it needs no cert logic.
agent::refresh_certs() {
  local src="${AGENT_CERTS_DIR:-${HOME}/.local/share/certs}"
  local dst="${PROJ_DIR}/container/certs"
  rm -rf "${dst}"
  mkdir -p "${dst}"
  cp "${src}"/* "${dst}/" 2>/dev/null || true
}

# Lazily build the podman image (the read-only rootfs). Built on first use and
# rebuilt with -b. node/uv/the CLIs are NOT baked in -- they come from the bound
# toolchain (~/.local/share/agent-box), so a rebuild never disturbs them.
agent::build_image() {
  if [[ "${REBUILD}" == "true" ]]; then
    podman image rm "${IMAGE}" 2>/dev/null || true
  fi
  if ! podman image exists "${IMAGE}"; then
    echo "Agent Box: building the ${IMAGE} image (one-time)..." >&2
    agent::refresh_certs
    podman build -t "${IMAGE}" -f "${PROJ_DIR}/container/Containerfile" "${PROJ_DIR}/container"
  fi
}

# A fresh podman container's clock is UTC -- bwrap inherits host-local time via its
# /etc bind, but podman has no host /etc/localtime. Pass the host zone as TZ instead
# of binding /etc/localtime (portable to macOS, where that symlink points into a
# different tree but the IANA name after zoneinfo/ is the same). Appends to
# ENV_LITERAL so it flows through the engine's env loop. podman branch only.
agent::derive_tz() {
  local tz
  tz=$(readlink "/etc/localtime" 2>/dev/null | sed -n 's#.*/zoneinfo/##p') || true
  [[ -n "${tz}" ]] && ENV_LITERAL+=("TZ=${tz}")
  return 0
}

# Build the podman run argv (array, no eval) and exec it. Everything is mounted at
# the SAME host path and HOME is the host's, so the bind tables + AGENT_ENV +
# AGENT_BIN + PATH are reused verbatim from the bwrap path (--bind a b -> -v a:b,
# --ro-bind a b -> -v a:b:ro, --setenv K V -> -e K=V). The image is just the
# read-only rootfs; the toolchain comes from the bound ${AGENT_TOOLS} on PATH.
agent::run_podman() {
  # No --userns=keep-id: rootless podman maps the container's root to the invoking
  # host user, so files the agent writes (project + bound dirs) are owned by you and
  # $HOME is writable. (keep-id would map you to uid 1000 inside, leaving $HOME's
  # auto-created mount parents root-owned and unwritable -- which breaks tools that
  # touch an unbound path under $HOME.) label=disable so SELinux doesn't block the
  # binds (and we don't relabel the shared dirs with :z/:Z).
  local PD=(run -it --rm --security-opt label=disable)
  local kv var e m
  for kv in "${AGENT_ENV[@]}";    do PD+=(-e "${kv}"); done
  for var in "${ENV_FORWARD[@]}"; do [[ -n "${!var:-}" ]] && PD+=(-e "${var}=${!var}"); done
  for kv in "${ENV_LITERAL[@]}";  do PD+=(-e "${kv}"); done
  PD+=(-v "${AGENT_TOOLS}:${AGENT_TOOLS}" -v "${AGENT_CACHE}:${AGENT_CACHE}")
  PD+=(-v "${APP_DIR}:${APP_DIR}" -w "${APP_DIR}")
  for e in "${BIND_DIRS[@]}";  do PD+=(-v "${e%%:*}:${e#*:}"); done
  for e in "${BIND_FILES[@]}"; do PD+=(-v "${e%%:*}:${e#*:}"); done
  for m in "${VOLUMES[@]}";    do PD+=(-v "${m}:${m}"); done
  for m in "${RO_VOLUMES[@]}"; do PD+=(-v "${m}:${m}:ro"); done
  # `--` ends podman's option parsing before the image + command.
  exec podman "${PD[@]}" -- "${IMAGE}" "${AGENT_BIN}" "${EXTRA_ARGS[@]}"
}

# Pick the engine, normalise paths, ensure sources + toolchain, then run. Under
# podman the image must exist before the toolchain install runs inside it.
agent::launch() {
  agent::detect_engine
  [[ "${REBUILD}" == "true" && "${ENGINE}" != "podman" ]] && \
    echo "Warning: -b (rebuild) applies to the podman engine only; ignoring." >&2
  agent::normalize_paths
  agent::ensure_sources
  if [[ "${ENGINE}" == "podman" ]]; then
    agent::derive_tz
    agent::build_image
    agent::ensure_tools
    agent::run_podman
  else
    agent::ensure_tools
    agent::run_bwrap
  fi
}

# ---- main: parse flags, then the tool name, then pass the rest verbatim ----

APP_DIR=$(pwd)
VOLUMES=()
RO_VOLUMES=()
ENGINE=""
REBUILD=false

# getopts stops at the first non-option token (the tool name).
while getopts "a:v:r:t:bh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    v) VOLUMES+=("$OPTARG") ;;
    r) RO_VOLUMES+=("$OPTARG") ;;
    t) ENGINE=$OPTARG ;;
    b) REBUILD=true ;;
    h|?) usage ;;
  esac
done
shift $((OPTIND - 1))

# First positional is the agent to run; the rest are its own args, verbatim.
TOOL="${1:-}"
if [[ -z "${TOOL}" ]]; then
  echo "Error: no agent specified (expected claude, opencode, or codex)." >&2
  usage
fi
shift
EXTRA_ARGS=("$@")

case "${TOOL}" in
  claude|opencode|codex) ;;
  *)
    echo "Error: unknown agent '${TOOL}' (expected claude, opencode, or codex)." >&2
    usage
    ;;
esac
AGENT_BIN="${AGENT_TOOLS}/bin/${TOOL}"

agent::launch
