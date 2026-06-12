#!/usr/bin/env bash
# shellcheck shell=bash
#
# agtbox.sh -- run an AI coding agent (claude, opencode, codex) inside an
# unprivileged bubblewrap sandbox. No container image, no engine: the agents run
# against the host's system packages plus a small per-user toolchain that is
# auto-installed on first use.
#
#   agtbox.sh [-a DIR] [-v VOL] [-r VOL] [-h] claude|opencode|codex [tool args...]
#
# Flags come BEFORE the tool name; everything AFTER it is passed to the tool
# VERBATIM (use the tool's own flags, e.g. `claude --resume`, `codex resume`).
# Requires bash >= 4 and bwrap (bubblewrap).

set -eo pipefail

# Persistent per-user dirs (all under $HOME, bound into the sandbox). The
# toolchain plus every global install the agents make live here and persist
# across runs; the rest of $HOME is an empty tmpfs inside the sandbox, so the
# agents can only write to these dirs (and the project) -- not the real home.
AGENT_TOOLS="${HOME}/.local/share/agent-box"   # node, npm, uv, the CLIs, global installs (rw)
AGENT_CONFIG="${HOME}/.config/agent-box"        # per-tool config (rw)
AGENT_CACHE="${HOME}/.cache/agent-box"          # npm/pip/uv download caches (rw)
NODE_VERSION="v24.11.0"                          # pinned toolchain node
NPM_PKGS=(@anthropic-ai/claude-code opencode-ai @openai/codex)

# Per-tool config wiring: "<subdir under AGENT_CONFIG>:<path under $HOME>". Each
# is bound straight onto the path the tool looks for -- no symlinks.
CONFIG_DIRS=(
  claude:.claude
  codex:.codex
  opencode:.config/opencode
  opencode/share:.local/share/opencode
  opencode/state:.local/state/opencode
  opencode/cache:.cache/opencode
)
CONFIG_FILES=( claude.json:.claude.json )       # seeded "{}" if absent; file-bound

# Every tool's env, always set (a tool ignores env it doesn't read). ENV_FORWARD
# is passed through only when set; ENV_LITERAL is always applied; derive_tz adds TZ.
ENV_FORWARD=(
  ANTHROPIC_BASE_URL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_AUTH_TOKEN
  OPENAI_API_KEY CODEX_API_KEY
)
ENV_LITERAL=(
  CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
  OPENCODE_ENABLE_EXA=1
  OPENCODE_EXPERIMENTAL_LSP_TOOL=true
)

usage() {
  echo "Usage: ${0##*/} [-a DIR] [-v VOL] [-r VOL] [-h] <claude|opencode|codex> [tool args...]"
  echo
  echo "  Run an AI coding agent in an unprivileged bubblewrap sandbox. Flags go"
  echo "  BEFORE the tool name; everything after it is passed to the tool verbatim"
  echo "  (run '${0##*/} <tool> --help' for the tool's own help). The toolchain is"
  echo "  installed into ~/.local/share/agent-box on first use (AGTBOX_REINSTALL=1"
  echo "  forces a reinstall)."
  echo
  echo "  Flags:"
  echo "    -a DIR   Project directory, bound at the same path inside (default: cwd)"
  echo "    -v VOL   Extra dir, bound read-write at the same path (repeatable)"
  echo "    -r VOL   Extra dir, bound read-only at the same path (repeatable)"
  echo "    -h       Show this help"
  exit 1
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

# Add the host timezone to the env so the sandbox reports local time.
agent::derive_tz() {
  local tz=""
  tz=$(readlink -f /etc/localtime 2>/dev/null | sed -n 's#.*/zoneinfo/##p') || true
  [[ -n "${tz}" ]] && ENV_LITERAL+=("TZ=${tz}")
  return 0
}

# Create the host-side bind sources so a fresh user can launch: the toolchain +
# cache dirs, the per-tool config subdirs, and the seed config files (valid empty
# JSON -- opencode rejects a present-but-invalid file). Never clobbers existing config.
agent::ensure_sources() {
  mkdir -p "${AGENT_TOOLS}" "${AGENT_CACHE}/npm" "${AGENT_CACHE}/pip" "${AGENT_CACHE}/uv"
  local e f
  for e in "${CONFIG_DIRS[@]}"; do
    mkdir -p "${AGENT_CONFIG}/${e%%:*}"
  done
  for e in "${CONFIG_FILES[@]}"; do
    f="${AGENT_CONFIG}/${e%%:*}"
    mkdir -p "$(dirname "${f}")"
    [[ -e "${f}" ]] || printf '{}' > "${f}"
  done
}

# Populate the global BW array with the bwrap args shared by install + run: the
# locked-down system binds (read-only), an empty tmpfs $HOME, the persistent
# toolchain + cache (read-write), shared network, and the env (PATH + npm/pip/uv
# routing into the persistent dirs + the env union).
agent::bwrap_common() {
  BW=(
    --ro-bind /usr /usr  --ro-bind /bin /bin  --ro-bind /sbin /sbin
    --ro-bind /lib /lib  --ro-bind-try /lib64 /lib64  --ro-bind /etc /etc
    --dev /dev  --proc /proc  --tmpfs /tmp
    --tmpfs "${HOME}"
    --bind "${AGENT_TOOLS}" "${AGENT_TOOLS}"
    --bind "${AGENT_CACHE}" "${AGENT_CACHE}"
    --die-with-parent  --unshare-pid  --unshare-ipc  --unshare-uts
    --setenv HOME "${HOME}"
    --setenv PATH "${AGENT_TOOLS}/bin:${AGENT_TOOLS}/node/bin:/usr/bin:/bin"
    --setenv npm_config_prefix "${AGENT_TOOLS}"
    --setenv npm_config_cache "${AGENT_CACHE}/npm"
    --setenv PIP_PREFIX "${AGENT_TOOLS}"
    --setenv PYTHONUSERBASE "${AGENT_TOOLS}"
    --setenv PIP_CACHE_DIR "${AGENT_CACHE}/pip"
    --setenv PIP_BREAK_SYSTEM_PACKAGES "1"
    --setenv UV_CACHE_DIR "${AGENT_CACHE}/uv"
    --setenv UV_TOOL_DIR "${AGENT_TOOLS}/uv/tools"
    --setenv UV_TOOL_BIN_DIR "${AGENT_TOOLS}/bin"
  )
  local var kv
  for var in "${ENV_FORWARD[@]}"; do
    [[ -n "${!var:-}" ]] && BW+=(--setenv "${var}" "${!var}")
  done
  for kv in "${ENV_LITERAL[@]}"; do
    BW+=(--setenv "${kv%%=*}" "${kv#*=}")
  done
}

# Install the toolchain into AGENT_TOOLS from INSIDE a bwrap sandbox (toolchain +
# cache rw, system ro, $HOME tmpfs) so the official installer scripts can't touch
# the host. Idempotent: re-running upgrades in place. Needs host curl/tar/xz.
agent::install_tools() {
  local arch na
  arch=$(uname -m)
  case "${arch}" in
    aarch64) na=arm64 ;;
    x86_64)  na=x64 ;;
    *) echo "Error: unsupported architecture '${arch}' for the node download." >&2; exit 1 ;;
  esac
  agent::bwrap_common
  bwrap "${BW[@]}" -- /usr/bin/bash -c "
    set -euo pipefail
    mkdir -p '${AGENT_TOOLS}/node' '${AGENT_TOOLS}/bin'
    echo 'Agent Box: downloading node ${NODE_VERSION} (${na})...' >&2
    curl -fsSL 'https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${na}.tar.xz' \
      | tar -xJ --strip-components=1 -C '${AGENT_TOOLS}/node'
    echo 'Agent Box: installing agent CLIs...' >&2
    '${AGENT_TOOLS}/node/bin/npm' install -g --prefix '${AGENT_TOOLS}' --cache '${AGENT_CACHE}/npm' \
      --no-fund --no-audit ${NPM_PKGS[*]}
    echo 'Agent Box: installing uv...' >&2
    curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR='${AGENT_TOOLS}/bin' UV_NO_MODIFY_PATH=1 sh
    date > '${AGENT_TOOLS}/.stamp'
  "
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
  for e in "${CONFIG_DIRS[@]}";  do BW+=(--bind "${AGENT_CONFIG}/${e%%:*}" "${HOME}/${e#*:}"); done
  for e in "${CONFIG_FILES[@]}"; do BW+=(--bind "${AGENT_CONFIG}/${e%%:*}" "${HOME}/${e#*:}"); done
  for m in "${VOLUMES[@]}";    do BW+=(--bind    "${m}" "${m}"); done
  for m in "${RO_VOLUMES[@]}"; do BW+=(--ro-bind "${m}" "${m}"); done
  # `--` ends bwrap's option parsing, so a `-`-leading tool arg can't be misread.
  exec bwrap "${BW[@]}" -- "${AGENT_BIN}" "${EXTRA_ARGS[@]}"
}

# Derive TZ, normalise paths, ensure sources + toolchain, then run.
agent::launch() {
  agent::derive_tz
  agent::normalize_paths
  agent::ensure_sources
  agent::ensure_tools
  agent::run_bwrap
}

# ---- main: parse flags, then the tool name, then pass the rest verbatim ----

APP_DIR=$(pwd)
VOLUMES=()
RO_VOLUMES=()

# getopts stops at the first non-option token (the tool name).
while getopts "a:v:r:h" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    v) VOLUMES+=("$OPTARG") ;;
    r) RO_VOLUMES+=("$OPTARG") ;;
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
