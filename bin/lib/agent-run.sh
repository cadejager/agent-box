#!/usr/bin/env bash
# shellcheck shell=bash
#
# Shared implementation for the containerised agent launchers (ccc.sh, occ.sh,
# cdx.sh).
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
#   AGENT_ARGS     array of args appended to AGENT_BIN (may be empty)
#   EXTRA_ARGS     array of pass-through args (after `--`), appended last (may be empty)
#   CONFIG_MOUNTS  array of "hostpath:containerpath" mounts for agent config
#   ENV_FORWARD    array of host env var names to forward when set and non-empty (may be empty)
#   ENV_LITERAL    array of "VAR=VALUE" always set in the container (may be empty)
#
# The lib itself also sets SHARED_MOUNTS -- engine-agnostic bind mounts applied to
# every agent (currently the pip + npm download caches; see its definition below).
#
# Those wrapper-provided globals are referenced here without local assignment:
# shellcheck disable=SC2154

# Repo root, derived from this file's location (bin/lib/agent-run.sh).
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/../.." &> /dev/null && pwd )

# Engine-agnostic mounts shared by every agent: persist the pip + npm download
# caches across the ephemeral --rm container so re-installs are fast. These bind
# the DEFAULT cache paths, so no env vars are needed -- the tools just find a warm
# cache. The host root lives outside the repo (no .gitignore entry needed) and is
# safe to delete to reclaim space. Sources end in "/" so agent::ensure_config_sources
# auto-creates them.
SHARED_MOUNTS=(
  "${HOME}/.cache/podman-ai-agents/pip/:/root/.cache/pip/"
  "${HOME}/.cache/podman-ai-agents/npm/:/root/.npm/"
)

# Shared "Container args" section for the launchers' -h output. Each wrapper
# calls this, then prints its own tool-specific session + pass-through lines.
agent::usage_container() {
  echo "  Container args:"
  echo "    -a DIR   App directory, mounted at the same path inside (default: cwd)"
  echo "    -v VOL   Extra volume, mounted at the same path inside (repeatable)"
  echo "    -t TYPE  Engine: podman or charliecloud (default: auto-detect)"
  echo "    -b       Rebuild images"
}

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
  args+=("${IMAGE}" "${AGENT_BIN}" "${AGENT_ARGS[@]}" "${EXTRA_ARGS[@]}")
  podman "${args[@]}"
}

# Build the Charliecloud ch-run argv and exec it.
agent::run_charliecloud() {
  local args=(--write-fake --private-tmp -b "${APP_DIR}:${APP_DIR}" --cd "${APP_DIR}")
  local mount var kv
  for mount in "${VOLUMES[@]}"; do
    args+=(-b "${mount}:${mount}")
  done
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
# If nothing is derivable, TZ is left unset and the container stays UTC (current
# behaviour). Appended to ENV_LITERAL so both the podman (-e) and ch-run
# (--set-env) emission loops forward it unchanged.
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
