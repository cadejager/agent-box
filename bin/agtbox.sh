#!/usr/bin/env bash
# shellcheck shell=bash
#
# agtbox.sh -- run an AI coding agent inside a rootless container (Agent Box).
#
#   agtbox.sh [-a DIR] [-v VOL] [-r VOL] [-t podman|charliecloud] [-b] [-h] \
#             claude|opencode|codex [tool args...]
#
# One launcher, one image (agent-box) holding all three agents. Container flags
# come BEFORE the tool name; everything AFTER the tool name is passed to the tool
# VERBATIM -- use the tool's own flags (e.g. `claude --resume`, `codex resume`).
# Requires bash >= 4 (arrays, ${!var}).

set -eo pipefail

# Repo root, derived from this file's location (bin/agtbox.sh).
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/.." &> /dev/null && pwd )

# One image for all three agents; built from container/Containerfile.
IMAGE="agent-box"
CONTAINERFILE="Containerfile"

# Every tool's env, always exported (a tool ignores env it doesn't read), so the
# launcher needs no per-tool env logic. Forwarded only when set (so empty values
# don't shadow mounted config); literals always set. derive_tz appends TZ.
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
  echo "    -b       Rebuild the image"
  echo "    -h       Show this help"
  echo
  echo "  Native tool flags (typed after the tool name):"
  echo "     claude|opencode|codex"
  echo "       -h    Show tool help"
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
  # Guard: a path given as BOTH -v (rw) and -r (ro) would be a duplicate mount
  # target and podman would reject the run. Read-write wins -- drop it from
  # RO_VOLUMES and warn.
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

# Create the host side of the consolidated layout so a fresh user can launch.
# config-layout.sh (the same script baked into the image) creates every target
# subdir + seed file under ~/.config/agent-box -- the single bind source. Run
# WITHOUT --symlinks here: on the host we only need the dirs/files to exist (the
# in-container symlinks are baked into the image). Both engines refuse a missing
# bind source, and config-layout.sh never clobbers existing config.
agent::ensure_config_sources() {
  bash "${PROJ_DIR}/container/config-layout.sh" "${HOME}/.config/agent-box"
}

# Copy the host CA bundle into container/certs/ for the image build. Baked into
# agent-box so containers get through TLS-intercepting proxies. Source defaults
# to ~/.local/share/certs, overridable via AGENT_CERTS_DIR; missing/empty is fine.
agent::refresh_certs() {
  local src="${AGENT_CERTS_DIR:-${HOME}/.local/share/certs}"
  local dst="${PROJ_DIR}/container/certs"
  rm -rf "${dst}"
  mkdir -p "${dst}"
  cp "${src}"/* "${dst}/" 2>/dev/null || true
}

# Lazily build the one agent-box image with podman.
agent::build_podman() {
  if [[ "${REBUILD}" == "true" ]]; then
    podman image rm "${IMAGE}" 2>/dev/null || true
  fi
  pushd "${PROJ_DIR}/container" > /dev/null || exit
  if ! podman image exists "${IMAGE}"; then
    agent::refresh_certs
    podman build -t "${IMAGE}" -f "${CONTAINERFILE}" .
  fi
  popd > /dev/null || exit
}

# Lazily build the one agent-box image with Charliecloud, stored under
# container/.charliecloud/.
agent::build_charliecloud() {
  mkdir -p "${CH_STORAGE}"
  if [[ "${REBUILD}" == "true" ]]; then
    rm -rf "${CH_STORAGE:?}/${IMAGE:?}"
    ch-image delete "${IMAGE}" 2>/dev/null || true
  fi
  pushd "${PROJ_DIR}/container" > /dev/null || exit
  if [[ ! -d "${CH_STORAGE}/${IMAGE}" ]]; then
    agent::refresh_certs
    ch-image build -t "${IMAGE}" -f "${CONTAINERFILE}" .
    ch-convert -i ch-image -o dir "${IMAGE}" "${CH_STORAGE}/${IMAGE}"
  fi
  popd > /dev/null || exit
}

# Build the podman run argv and exec it. Built as an array -- no eval.
agent::run_podman() {
  local args=(run -it --rm -v "${APP_DIR}:${APP_DIR}" -w "${APP_DIR}")
  local mount var kv
  for mount in "${VOLUMES[@]}"; do
    args+=(-v "${mount}:${mount}")
  done
  for mount in "${RO_VOLUMES[@]}"; do
    args+=(-v "${mount}:${mount}:ro")
  done
  args+=(-v "${HOME}/.config/agent-box/:/root/.config/agent-box/")
  for var in "${ENV_FORWARD[@]}"; do
    if [[ -n "${!var:-}" ]]; then
      args+=(-e "${var}=${!var}")
    fi
  done
  for kv in "${ENV_LITERAL[@]}"; do
    args+=(-e "${kv}")
  done
  # `--` ends podman's option parsing before the image + command, so a passed-
  # through tool arg that starts with `-` can't be misread as a podman flag.
  args+=(-- "${IMAGE}" "${AGENT_BIN}" "${EXTRA_ARGS[@]}")
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
  # here. Warn once so the caller knows the -r guarantee isn't enforced under CC.
  if [[ ${#RO_VOLUMES[@]} -gt 0 ]]; then
    echo "Warning: Charliecloud cannot enforce read-only binds; mounting read-write: ${RO_VOLUMES[*]}" >&2
    for mount in "${RO_VOLUMES[@]}"; do
      args+=(-b "${mount}:${mount}")
    done
  fi
  args+=(-b "${HOME}/.config/agent-box/:/root/.config/agent-box/")
  # --env-no-expand makes ch-run pass the value verbatim (no path/$-expansion).
  for var in "${ENV_FORWARD[@]}"; do
    if [[ -n "${!var:-}" ]]; then
      args+=(--env-no-expand "--set-env=${var}=${!var}")
    fi
  done
  for kv in "${ENV_LITERAL[@]}"; do
    args+=(--env-no-expand "--set-env=${kv}")
  done
  args+=("${CH_STORAGE}/${IMAGE}" -- "${AGENT_BIN}" "${EXTRA_ARGS[@]}")
  ch-run "${args[@]}"
}

# Add timezone to env so container knows the time
agent::derive_tz() {
  local tz=""
  tz=$(readlink -f /etc/localtime 2>/dev/null | sed -n 's#.*/zoneinfo/##p') || true
  [[ -n "${tz}" ]] && ENV_LITERAL+=("TZ=${tz}")
  return 0
}

# Detect engine, derive TZ, normalise paths, ensure config, build, run.
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
      CH_STORAGE="${PROJ_DIR}/container/.charliecloud"
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

# One image serves all three agents; the tool name just selects the binary
# (binary name == tool name). Config + env are tool-independent (set above).
case "${TOOL}" in
  claude|opencode|codex) ;;
  *)
    echo "Error: unknown agent '${TOOL}' (expected claude, opencode, or codex)." >&2
    usage
    ;;
esac
AGENT_BIN="/usr/local/bin/${TOOL}"

agent::launch
