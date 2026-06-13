#!/usr/bin/env bash
# shellcheck shell=bash
#
# agtbox.sh -- run an AI coding agent (claude, opencode, codex) inside an
# unprivileged bubblewrap sandbox over the host's system packages plus a small
# per-user toolchain that is auto-installed on first use.
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
AGENT_STATE="${HOME}/.local/state/agent-box"    # per-tool state (rw)
AGENT_CACHE="${HOME}/.cache/agent-box"          # npm/pip/uv + per-tool caches (rw)
NPM_PKGS=(@anthropic-ai/claude-code opencode-ai @openai/codex)

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

# Create the host-side bind sources so a fresh user can launch: the toolchain +
# cache dirs, the per-tool config dirs, the seed JSON files, and the empty
# seed-only files (git config). Never clobbers existing config.
agent::ensure_sources() {
  mkdir -p "${AGENT_TOOLS}" "${AGENT_CACHE}/npm" "${AGENT_CACHE}/pip" "${AGENT_CACHE}/uv"
  local e f
  for e in "${BIND_DIRS[@]}"; do
    mkdir -p "${e%%:*}"
  done
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
    --ro-bind-try /lib64 /lib64  --ro-bind-try /opt /opt
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
# cache rw, system ro, $HOME tmpfs) so the installer scripts can't touch the host.
# Idempotent: re-running upgrades in place. node tracks the latest LTS, gh/glab
# their latest releases. Needs host curl/tar/xz/python3.
agent::install_tools() {
  local narch goarch
  case "$(uname -m)" in
    aarch64) narch=arm64; goarch=arm64 ;;
    x86_64)  narch=x64;   goarch=amd64 ;;
    *) echo "Error: unsupported architecture '$(uname -m)'." >&2; exit 1 ;;
  esac
  # A quoted here-doc -- NO launcher-side expansion; it reads its inputs from the
  # AGT_* env vars set on the bwrap command below (avoids nested-quoting hazards).
  local script
  script=$(cat <<'INSTALL'
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
)
  agent::bwrap_common
  bwrap "${BW[@]}" \
    --setenv AGT_TOOLS "${AGENT_TOOLS}" \
    --setenv AGT_NARCH "${narch}" \
    --setenv AGT_GOARCH "${goarch}" \
    --setenv AGT_NPM_PKGS "${NPM_PKGS[*]}" \
    -- /usr/bin/bash -c "${script}"
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

# Normalise paths, ensure sources + toolchain, then run.
agent::launch() {
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
