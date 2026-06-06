#!/usr/bin/env bash
#
# Launches claude-code in a container (podman or charliecloud)

set -eo pipefail

# Get the dir of the project
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/.." &> /dev/null && pwd )

# Defaults
APP_DIR=$(pwd)
CONTAINER_TYPE=""
REBUILD=false
CONTINUE=false
EFFORT=""
SESSION=""
FORK=false
VOLUMES=()

usage() {
  echo "Usage: ${0} [-a APP_DIR] [-v VOLUME] [-t TYPE] [-c]  [-e EFFORT] [-s SESSION] [-f] [-r] [-h]"
  echo "  Container args:"
  echo "    -a       The application directory (default: current dir)"
  echo "             Will be mounted at the same path inside the container"
  echo "    -v       Additional volume to mount (can be specified multiple times)"
  echo "             Path will be mounted at the same location inside the container"
  echo "    -r       Rebuild images"
  echo "    -t       The container engine to use (podman or charliecloud)"
  echo "  Claude Code args:"
  echo "    -c       Continue the last session"
  echo "    -e       Effort level for the current session (low, medium, high, xhigh, max)"
  echo "    -s       Resume a conversation by session ID, or open interactive picker with optional search term"
  echo "    -f       When resuming, create a new session ID instead of reusing the original (use with -s or -c)"
  echo ""
  echo "  -h       Display this message"
  exit 1
}

while getopts "a:v:t:ce:s:frh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    v) VOLUMES+=("$OPTARG") ;;
    t) CONTAINER_TYPE=$OPTARG ;;
    c) CONTINUE=true ;;
    e) EFFORT=$OPTARG ;;
    s) SESSION=$OPTARG ;;
    f) FORK=true ;;
    r) REBUILD=true ;;
    h|?) usage ;;
  esac
done

# Determine container type if not specified
if [[ -z "${CONTAINER_TYPE}" ]]; then
  if command -v ch-run >/dev/null 2>&1; then
    CONTAINER_TYPE="charliecloud"
  elif command -v podman >/dev/null 2>&1; then
    CONTAINER_TYPE="podman"
  else
    echo "Error: No supported container engine (podman or charliecloud) found."
    exit 1
  fi
fi

# Ensure APP_DIR is absolute
APP_DIR=$(realpath "$APP_DIR")

# Convert all volume paths to absolute paths
for i in "${!VOLUMES[@]}"; do
  VOLUMES[i]=$(realpath "${VOLUMES[i]}")
done

# Build claude-code arguments
CLAUDE_CODE_ARGS=""
if [[ "${CONTINUE}" == "true" ]]; then
  CLAUDE_CODE_ARGS="${CLAUDE_CODE_ARGS} --continue"
fi
if [[ -n "${SESSION}" ]]; then
  CLAUDE_CODE_ARGS="${CLAUDE_CODE_ARGS} --resume ${SESSION}"
fi
if [[ "${FORK}" == "true" ]]; then
  CLAUDE_CODE_ARGS="${CLAUDE_CODE_ARGS} --fork-session"
fi
if [[ -n "${EFFORT}" ]]; then
  CLAUDE_CODE_ARGS="${CLAUDE_CODE_ARGS} --effort ${EFFORT}"
fi

# Host env vars to forward into the container, but only when they are set, so we
# don't inject empty values that could shadow config from the mounted
# ~/.claude.json.
ANTHROPIC_ENV=(ANTHROPIC_BASE_URL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_AUTH_TOKEN)

# build_env_args PREFIX VAR... -> for each VAR that is set, echo PREFIXVAR='value'
build_env_args() {
  local prefix="$1"; shift
  local out="" var
  for var in "$@"; do
    if [[ -n "${!var:-}" ]]; then
      out="${out} ${prefix}${var}='${!var}'"
    fi
  done
  printf '%s' "${out}"
}

if [[ "${CONTAINER_TYPE}" == "podman" ]]; then
  if [[ "true" == "${REBUILD}" ]]; then
    podman image rm agent-base 2>/dev/null || true
    podman image rm claude-code 2>/dev/null || true
  fi

  pushd "${PROJ_DIR}/agents" > /dev/null || exit
  if ! podman image exists agent-base; then
    podman build -t agent-base -f Containerfile.base .
  fi
  if ! podman image exists claude-code; then
    rm -rf certs
    mkdir certs
    cp "${HOME}"/.local/share/certs/* certs/ 2>/dev/null || true
    podman build -t claude-code -f Containerfile.claude-code .
  fi
  popd > /dev/null || exit

  CMD="podman run -it --rm -v '${APP_DIR}':'${APP_DIR}' -w '${APP_DIR}'"
  # Add additional volumes
  for vol in "${VOLUMES[@]}"; do
    CMD="${CMD} -v '${vol}':'${vol}'"
  done
  ENV_ARGS="$(build_env_args '-e ' "${ANTHROPIC_ENV[@]}") -e CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"
  CMD="${CMD} \
    -v '${HOME}/.claude/':'/root/.claude/' \
    -v '${HOME}/.claude.json':'/root/.claude.json' \
    ${ENV_ARGS} \
    claude-code /usr/local/bin/claude${CLAUDE_CODE_ARGS}"
  
  eval "${CMD}"

elif [[ "${CONTAINER_TYPE}" == "charliecloud" ]]; then
  # Charliecloud storage directory
  CH_STORAGE="${PROJ_DIR}/agents/.charliecloud"
  mkdir -p "${CH_STORAGE}"

  if [[ "true" == "${REBUILD}" ]]; then
    rm -rf "${CH_STORAGE}/agent-base"
    ch-image delete agent-base 2>/dev/null || true
    rm -rf "${CH_STORAGE}/claude-code"
    ch-image delete claude-code 2>/dev/null || true
  fi

  pushd "${PROJ_DIR}/agents" > /dev/null || exit
  if [[ ! -d "${CH_STORAGE}/agent-base" ]]; then
    ch-image build -t agent-base -f Containerfile.base .
    ch-convert -i ch-image -o dir agent-base "${CH_STORAGE}/agent-base"
  fi
  if [[ ! -d "${CH_STORAGE}/claude-code" ]]; then
    rm -rf certs
    mkdir certs
    cp "${HOME}"/.local/share/certs/* certs/ 2>/dev/null || true
    ch-image build -t claude-code -f Containerfile.claude-code .
    ch-convert -i ch-image -o dir claude-code "${CH_STORAGE}/claude-code"
  fi
  popd > /dev/null || exit

  CMD="ch-run --write-fake --private-tmp -b '${APP_DIR}':'${APP_DIR}' --cd '${APP_DIR}'"
  # Add additional volumes
  for vol in "${VOLUMES[@]}"; do
    CMD="${CMD} -b '${vol}':'${vol}'"
  done
  ENV_ARGS="$(build_env_args '--set-env=' "${ANTHROPIC_ENV[@]}") --set-env=CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"
  CMD="${CMD} \
    -b ${HOME}/.claude/:/root/.claude/ \
    -b ${HOME}/.claude.json:/root/.claude.json \
    ${ENV_ARGS} \
    ${CH_STORAGE}/claude-code -- /usr/local/bin/claude${CLAUDE_CODE_ARGS}"
  
  eval "${CMD}"

else
  echo "Error: Unsupported container type: ${CONTAINER_TYPE}"
  exit 1
fi
