#!/usr/bin/env bash
# shellcheck shell=bash
#
# agtbox.sh -- run an AI coding agent inside a rootless container.
#
#   agtbox.sh [-a DIR] [-v VOL] [-r VOL] [-t podman|charliecloud] [-b] [-h] \
#             claude|opencode|codex [tool args...]
#
# One launcher for all three agents. Container flags come BEFORE the tool name;
# everything AFTER the tool name is passed to the tool VERBATIM -- use the tool's
# own flags (e.g. `claude --resume ID`, `opencode --session ID`, `codex resume`).
# Requires bash >= 4 (arrays, ${!var}).

set -eo pipefail

# Repo root, derived from this file's location (bin/agtbox.sh).
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/.." &> /dev/null && pwd )

# Engine-agnostic mounts shared by every agent: persist the pip + npm download
# caches across the ephemeral --rm container so re-installs are fast. These bind
# the DEFAULT cache paths, so no env vars are needed -- the tools just find a warm
# cache. The host root lives outside the repo (no .gitignore entry needed) and is
# safe to delete to reclaim space. Sources end in "/" so ensure_config_sources
# auto-creates them.
SHARED_MOUNTS=(
  "${HOME}/.cache/podman-ai-agents/pip/:/root/.cache/pip/"
  "${HOME}/.cache/podman-ai-agents/npm/:/root/.npm/"
)

usage() {
  echo "Usage: ${0##*/} [-a DIR] [-v VOL] [-r VOL] [-t podman|charliecloud] [-b] [-h] <claude|opencode|codex> [tool args...]"
  echo
  echo "  Run an AI coding agent in a rootless container. Container flags go BEFORE"
  echo "  the tool name; everything after the tool name is passed to the tool"
  echo "  verbatim (run '${0##*/} <tool> --help' for the tool's own help)."
  echo
  echo "  Container args:"
  echo "    -a DIR   App directory, mounted at the same path inside (default: cwd)"
  echo "    -v VOL   Extra volume, mounted at the same path inside (repeatable)"
  echo "    -r VOL   Extra volume, mounted READ-ONLY at the same path (repeatable; podman only -- charliecloud mounts rw)"
  echo "    -t TYPE  Engine: podman or charliecloud (default: auto-detect)"
  echo "    -b       Rebuild images"
  echo "    -h       Show this help"
  echo
  echo "  Native session flags (typed after the tool name -- no remapping):"
  echo "    claude     --continue | --resume [ID] | --fork-session"
  echo "    opencode   --continue | --session ID  | --fork"
  echo "    codex      resume [ID] | fork [ID]"
  exit 1
}

# Pick an engine if not forced with -t. Prefers Charliecloud.
agent::detect_engine() {
  if [[ -n "${CONTAINER_TYPE}" ]]; then
    return
  fi
  if command -v ch-run >/dev/null 2>&1; then
    CONTAINER_TYPE="charliecloud"
  elif command -v podman >/dev/null 2>&1; then
    CONTAINER_TYPE="podman"
  else
    echo "Error: No supported container engine (podman or charliecloud) found." >&2
    exit 1
  fi
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
  # Guard: a path given as BOTH -v (rw) and -r (ro) would be a duplicate
  # mount target and podman would reject the run. Read-write wins -- drop the
  # path from RO_VOLUMES and warn.
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
      echo "Warning: ${RO_VOLUMES[i]} given as both -v (rw) and -r (ro); mounting read-write." >&2
    else
      kept+=("${RO_VOLUMES[i]}")
    fi
  done
  RO_VOLUMES=("${kept[@]}")
}

# Make sure each CONFIG_MOUNTS host source exists before it is bind-mounted --
# podman and ch-run both refuse a missing bind source. A trailing slash means a
# directory; otherwise a file (create the parent dir, then touch it). This lets
# a fresh user who has never run the host-native tool still launch.
agent::ensure_config_sources() {
  local mount host
  for mount in "${CONFIG_MOUNTS[@]}" "${SHARED_MOUNTS[@]}"; do
    host="${mount%%:*}"
    if [[ "${host}" == */ ]]; then
      mkdir -p "${host}"
    else
      mkdir -p "$(dirname "${host}")"
      [[ -e "${host}" ]] || touch "${host}"
    fi
  done
}

# Copy the host CA bundle into agents/certs/ for the image build. These are
# baked into agent-base (which every agent image inherits) so containers can get
# through TLS-intercepting corporate proxies. Run from the agents/ directory.
# Source defaults to ~/.local/share/certs, overridable via AGENT_CERTS_DIR; a
# missing/empty source is fine (an empty certs/ dir still satisfies the COPY,
# and update-ca-certificates just adds nothing).
agent::refresh_certs() {
  local src="${AGENT_CERTS_DIR:-${HOME}/.local/share/certs}"
  local dst="${PROJ_DIR}/agents/certs"
  rm -rf "${dst}"
  mkdir -p "${dst}"
  cp "${src}"/* "${dst}/" 2>/dev/null || true
}

# Lazily build agent-base then the agent image with podman.
agent::build_podman() {
  if [[ "${REBUILD}" == "true" ]]; then
    podman image rm agent-base 2>/dev/null || true
    podman image rm "${IMAGE}" 2>/dev/null || true
  fi
  pushd "${PROJ_DIR}/agents" > /dev/null || exit
  if ! podman image exists agent-base; then
    agent::refresh_certs
    podman build -t agent-base -f Containerfile.base .
  fi
  if ! podman image exists "${IMAGE}"; then
    podman build -t "${IMAGE}" -f "${CONTAINERFILE}" .
  fi
  popd > /dev/null || exit
}

# Lazily build agent-base then the agent image with Charliecloud, storing the
# unpacked images under agents/.charliecloud/.
agent::build_charliecloud() {
  mkdir -p "${CH_STORAGE}"
  if [[ "${REBUILD}" == "true" ]]; then
    rm -rf "${CH_STORAGE:?}/agent-base"
    ch-image delete agent-base 2>/dev/null || true
    rm -rf "${CH_STORAGE:?}/${IMAGE:?}"
    ch-image delete "${IMAGE}" 2>/dev/null || true
  fi
  pushd "${PROJ_DIR}/agents" > /dev/null || exit
  if [[ ! -d "${CH_STORAGE}/agent-base" ]]; then
    agent::refresh_certs
    ch-image build -t agent-base -f Containerfile.base .
    ch-convert -i ch-image -o dir agent-base "${CH_STORAGE}/agent-base"
  fi
  if [[ ! -d "${CH_STORAGE}/${IMAGE}" ]]; then
    ch-image build -t "${IMAGE}" -f "${CONTAINERFILE}" .
    ch-convert -i ch-image -o dir "${IMAGE}" "${CH_STORAGE}/${IMAGE}"
  fi
  popd > /dev/null || exit
}

# Build the podman run argv (mounting APP_DIR/volumes/config at the same paths)
# and exec it. Built as an array -- no eval, no quoting games.
agent::run_podman() {
  local args=(run -it --rm -v "${APP_DIR}:${APP_DIR}" -w "${APP_DIR}")
  local mount var kv
  for mount in "${VOLUMES[@]}"; do
    args+=(-v "${mount}:${mount}")
  done
  for mount in "${RO_VOLUMES[@]}"; do
    args+=(-v "${mount}:${mount}:ro")
  done
  for mount in "${CONFIG_MOUNTS[@]}"; do
    args+=(-v "${mount}")
  done
  for mount in "${SHARED_MOUNTS[@]}"; do
    args+=(-v "${mount}")
  done
  for var in "${ENV_FORWARD[@]}"; do
    if [[ -n "${!var:-}" ]]; then
      args+=(-e "${var}=${!var}")
    fi
  done
  for kv in "${ENV_LITERAL[@]}"; do
    args+=(-e "${kv}")
  done
  # `--` ends podman's own option parsing before the image + command, so a
  # passed-through tool arg that starts with `-` can never be misread as a
  # podman flag (and mirrors the ch-run path below).
  args+=(-- "${IMAGE}" "${AGENT_BIN}" "${AGENT_ARGS[@]}" "${EXTRA_ARGS[@]}")
  podman "${args[@]}"
}

# Build the Charliecloud ch-run argv and exec it.
agent::run_charliecloud() {
  local args=(--write-fake --private-tmp -b "${APP_DIR}:${APP_DIR}" --cd "${APP_DIR}")
  local mount var kv
  for mount in "${VOLUMES[@]}"; do
    args+=(-b "${mount}:${mount}")
  done
  # ch-run has NO read-only bind option, so RO_VOLUMES are mounted READ-WRITE
  # here. Warn once (listing the paths) so the caller knows the -r guarantee is
  # not enforced under Charliecloud.
  if [[ ${#RO_VOLUMES[@]} -gt 0 ]]; then
    echo "Warning: Charliecloud cannot enforce read-only binds; mounting read-write: ${RO_VOLUMES[*]}" >&2
    for mount in "${RO_VOLUMES[@]}"; do
      args+=(-b "${mount}:${mount}")
    done
  fi
  for mount in "${CONFIG_MOUNTS[@]}"; do
    args+=(-b "${mount}")
  done
  for mount in "${SHARED_MOUNTS[@]}"; do
    args+=(-b "${mount}")
  done
  # --env-no-expand makes ch-run pass the value verbatim; without it ch-run does
  # search-path/$-expansion on values that podman's -e leaves untouched.
  for var in "${ENV_FORWARD[@]}"; do
    if [[ -n "${!var:-}" ]]; then
      args+=(--env-no-expand "--set-env=${var}=${!var}")
    fi
  done
  for kv in "${ENV_LITERAL[@]}"; do
    args+=(--env-no-expand "--set-env=${kv}")
  done
  args+=("${CH_STORAGE}/${IMAGE}" -- "${AGENT_BIN}" "${AGENT_ARGS[@]}" "${EXTRA_ARGS[@]}")
  ch-run "${args[@]}"
}

# Derive the host timezone (IANA name) and forward it so containers report
# host-local time instead of UTC. tzdata in agent-base resolves the name. Tried
# in order: timedatectl, /etc/timezone, the /etc/localtime symlink, then $TZ.
# If nothing is derivable, TZ is left unset and the container stays UTC. Appended
# to ENV_LITERAL so both the podman (-e) and ch-run (--set-env) loops forward it.
agent::derive_tz() {
  local tz=""
  if command -v timedatectl >/dev/null 2>&1; then
    tz=$(timedatectl show -p Timezone --value 2>/dev/null)
  fi
  [[ -z "${tz}" && -r /etc/timezone ]] && tz=$(cat /etc/timezone 2>/dev/null)
  [[ -z "${tz}" ]] && tz=$(readlink -f /etc/localtime 2>/dev/null | sed -n 's#.*/zoneinfo/##p')
  [[ -z "${tz}" ]] && tz="${TZ:-}"
  [[ -n "${tz}" ]] && ENV_LITERAL+=("TZ=${tz}")
}

# Detect engine, normalise paths, build images, run.
agent::launch() {
  agent::detect_engine
  agent::derive_tz
  agent::normalize_paths
  agent::ensure_config_sources
  case "${CONTAINER_TYPE}" in
    podman)
      agent::build_podman
      agent::run_podman
      ;;
    charliecloud)
      CH_STORAGE="${PROJ_DIR}/agents/.charliecloud"
      agent::build_charliecloud
      agent::run_charliecloud
      ;;
    *)
      echo "Error: Unsupported container type: ${CONTAINER_TYPE}" >&2
      exit 1
      ;;
  esac
}

# ---- main: parse container flags, then the tool name, then pass the rest ----

# Defaults
APP_DIR=$(pwd)
CONTAINER_TYPE=""
REBUILD=false
VOLUMES=()
RO_VOLUMES=()
AGENT_ARGS=()   # no flag mapping any more -- tool args pass through verbatim

# getopts stops at the first non-option token (the tool name), so container
# flags are parsed here and everything from the tool name on is left in "$@".
while getopts "a:v:t:r:bh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    v) VOLUMES+=("$OPTARG") ;;
    r) RO_VOLUMES+=("$OPTARG") ;;
    t) CONTAINER_TYPE=$OPTARG ;;
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

# Per-agent container configuration (the only thing that differs between agents).
case "${TOOL}" in
  claude)
    IMAGE="claude-code"
    CONTAINERFILE="Containerfile.claude-code"
    AGENT_BIN="/usr/local/bin/claude"
    CONFIG_MOUNTS=(
      "${HOME}/.claude/:/root/.claude/"
      "${HOME}/.claude.json:/root/.claude.json"
    )
    # Forward these host env vars only when set, so empty values don't shadow the
    # mounted ~/.claude.json.
    ENV_FORWARD=(ANTHROPIC_BASE_URL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_AUTH_TOKEN)
    ENV_LITERAL=(CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1)
    ;;
  opencode)
    IMAGE="opencode"
    CONTAINERFILE="Containerfile.opencode"
    AGENT_BIN="/usr/local/bin/opencode"
    CONFIG_MOUNTS=(
      "${HOME}/.config/opencode/:/root/.config/opencode/"
      "${HOME}/.local/share/opencode/:/root/.local/share/opencode/"
      "${HOME}/.cache/opencode/:/root/.cache/opencode/"
      "${HOME}/.local/state/opencode/:/root/.local/state/opencode/"
    )
    ENV_FORWARD=()
    ENV_LITERAL=(OPENCODE_ENABLE_EXA=1 OPENCODE_EXPERIMENTAL_LSP_TOOL=true)
    ;;
  codex)
    IMAGE="codex"
    CONTAINERFILE="Containerfile.codex"
    AGENT_BIN="/usr/local/bin/codex"
    CONFIG_MOUNTS=(
      "${HOME}/.codex/:/root/.codex/"
    )
    # Forward an API key only when set, so it doesn't shadow a mounted ~/.codex
    # login. (The local-model endpoint can't be set via env -- OPENAI_BASE_URL is
    # ignored by current codex -- it must live in ~/.codex/config.toml.)
    ENV_FORWARD=(OPENAI_API_KEY CODEX_API_KEY)
    ENV_LITERAL=()
    ;;
  *)
    echo "Error: unknown agent '${TOOL}' (expected claude, opencode, or codex)." >&2
    usage
    ;;
esac

agent::launch
