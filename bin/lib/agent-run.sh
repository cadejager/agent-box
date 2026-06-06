#!/usr/bin/env bash
# shellcheck shell=bash
#
# Shared implementation for the containerised agent launchers (ccc.sh, occ.sh).
#
# A launcher sources this file, sets the variables below, then calls
# `agent::launch`. The heavy lifting -- engine detection, lazy image builds for
# both podman and Charliecloud, and constructing the run command -- lives here,
# so the per-agent wrappers only describe what differs between agents.
#
# Variables the wrapper must set before calling agent::launch:
#   APP_DIR        host dir mounted at the same path inside the container
#   VOLUMES        array of extra host paths to bind at the same path
#   CONTAINER_TYPE "podman" | "charliecloud" | "" to auto-detect
#   REBUILD        "true" to rebuild images first
#   IMAGE          image/tag name (e.g. claude-code)
#   CONTAINERFILE  Containerfile under agents/ (e.g. Containerfile.claude-code)
#   AGENT_BIN      in-container binary to run (e.g. /usr/local/bin/claude)
#   AGENT_ARGS     extra args appended to AGENT_BIN (leading space, may be empty)
#   CONFIG_MOUNTS  array of "hostpath:containerpath" mounts for agent config
#   ENV_FORWARD    array of host env var names to forward when set (may be empty)
#   ENV_LITERAL    array of "VAR=VALUE" always set in the container (may be empty)
#
# Those wrapper-provided globals are referenced here without local assignment:
# shellcheck disable=SC2154

# Repo root, derived from this file's location (bin/lib/agent-run.sh).
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/../.." &> /dev/null && pwd )

# Pick an engine if the wrapper did not force one with -t. Prefers Charliecloud.
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
  local i
  for i in "${!VOLUMES[@]}"; do
    VOLUMES[i]=$(realpath "${VOLUMES[i]}")
  done
}

# Copy the host CA bundle into agents/certs/ for the image build. Run from the
# agents/ directory; missing certs are skipped.
agent::refresh_certs() {
  rm -rf certs
  mkdir certs
  cp "${HOME}"/.local/share/certs/* certs/ 2>/dev/null || true
}

# Echo container env-flag args: PREFIXVAR='value' for each ENV_FORWARD var that
# is set, then PREFIXVAR=VALUE for each ENV_LITERAL entry. PREFIX is "-e " for
# podman or "--set-env=" for Charliecloud.
agent::env_args() {
  local prefix="$1" out="" var kv
  for var in "${ENV_FORWARD[@]}"; do
    if [[ -n "${!var:-}" ]]; then
      out="${out} ${prefix}${var}='${!var}'"
    fi
  done
  for kv in "${ENV_LITERAL[@]}"; do
    out="${out} ${prefix}${kv}"
  done
  printf '%s' "${out}"
}

# Lazily build agent-base then the agent image with podman.
agent::build_podman() {
  if [[ "${REBUILD}" == "true" ]]; then
    podman image rm agent-base 2>/dev/null || true
    podman image rm "${IMAGE}" 2>/dev/null || true
  fi
  pushd "${PROJ_DIR}/agents" > /dev/null || exit
  if ! podman image exists agent-base; then
    podman build -t agent-base -f Containerfile.base .
  fi
  if ! podman image exists "${IMAGE}"; then
    agent::refresh_certs
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
    ch-image build -t agent-base -f Containerfile.base .
    ch-convert -i ch-image -o dir agent-base "${CH_STORAGE}/agent-base"
  fi
  if [[ ! -d "${CH_STORAGE}/${IMAGE}" ]]; then
    agent::refresh_certs
    ch-image build -t "${IMAGE}" -f "${CONTAINERFILE}" .
    ch-convert -i ch-image -o dir "${IMAGE}" "${CH_STORAGE}/${IMAGE}"
  fi
  popd > /dev/null || exit
}

# Build the podman run command (mounting APP_DIR/volumes/config at the same
# paths) and exec it.
agent::run_podman() {
  local cmd mount
  cmd="podman run -it --rm -v '${APP_DIR}':'${APP_DIR}' -w '${APP_DIR}'"
  for mount in "${VOLUMES[@]}"; do
    cmd="${cmd} -v '${mount}':'${mount}'"
  done
  for mount in "${CONFIG_MOUNTS[@]}"; do
    cmd="${cmd} -v '${mount%%:*}':'${mount#*:}'"
  done
  cmd="${cmd}$(agent::env_args '-e ') ${IMAGE} ${AGENT_BIN}${AGENT_ARGS}"
  eval "${cmd}"
}

# Build the Charliecloud ch-run command and exec it.
agent::run_charliecloud() {
  local cmd mount
  cmd="ch-run --write-fake --private-tmp -b '${APP_DIR}':'${APP_DIR}' --cd '${APP_DIR}'"
  for mount in "${VOLUMES[@]}"; do
    cmd="${cmd} -b '${mount}':'${mount}'"
  done
  for mount in "${CONFIG_MOUNTS[@]}"; do
    cmd="${cmd} -b '${mount%%:*}':'${mount#*:}'"
  done
  cmd="${cmd}$(agent::env_args '--set-env=') ${CH_STORAGE}/${IMAGE} -- ${AGENT_BIN}${AGENT_ARGS}"
  eval "${cmd}"
}

# Detect engine, normalise paths, build images, run.
agent::launch() {
  agent::detect_engine
  agent::normalize_paths
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
