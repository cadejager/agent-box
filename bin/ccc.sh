#!/usr/bin/env bash
#
# Launches claude-code in a container (podman or charliecloud)

export CLAUDE_CONFIG_DIR='~/.claude'
unset ANTHROPIC_BASE_URL
export ANTHROPIC_BASE_URL='https://aiportal-api.aws.lanl.gov'
export ANTHROPIC_DEFAULT_SONNET_MODEL='anthropic.claude-sonnet-4-5-20250929-v1:0'
export NODE_USE_SYSTEM_CA='1'
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS='1'
export DISABLE_PROMPT_CACHING='1'

# Get the dir of the project
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/.." &> /dev/null && pwd )

# Defaults
APP_DIR=$(pwd)
BUILD_DIR=""
CONTAINER_TYPE=""
REBUILD=false
CONTINUE=false
EFFORT=""
SESSION=""
FORK=false

usage() {
  echo "Usage: ${0} [-a APP_DIR] [-b BUILD_DIR] [-t TYPE] [-c]  [-e EFFORT] [-s SESSION] [-f] [-r] [-h]"
  echo "  Container args:"
  echo "    -a       The application directory to bind to /app (default: current dir)"
  echo "    -b       The build directory to bind to /build"
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

while getopts "a:b:t:ce:s:frh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    b) BUILD_DIR=$OPTARG ;;
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
if [[ -n "${BUILD_DIR}" ]]; then
  BUILD_DIR=$(realpath "$BUILD_DIR")
fi

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

if [[ "${CONTAINER_TYPE}" == "podman" ]]; then
  if [[ "true" == "${REBUILD}" ]]; then
    podman image rm agent-base 2>/dev/null || true
    podman image rm claude-code 2>/dev/null || true
  fi

  pushd "${PROJ_DIR}/agents" > /dev/null
  if ! podman image exists agent-base; then
    podman build -t agent-base -f Containerfile.base .
  fi
  if ! podman image exists claude-code; then
    rm -rf certs
    mkdir certs
    cp ${HOME}/.local/share/certs/* certs/ 2>/dev/null || true
    podman build -t claude-code -f Containerfile.claude-code .
  fi
  popd > /dev/null

  CMD="podman run -it --rm -v ${APP_DIR}:/app"
  if [[ -n "${BUILD_DIR}" ]]; then
    CMD="${CMD} -v ${BUILD_DIR}:/build"
  fi
  CMD="${CMD} \
    -v ${HOME}/.claude/:/root/.claude \
    -e ANTHROPIC_BASE_URL='${ANTHROPIC_BASE_URL}' \
    -e ANTHROPIC_DEFAULT_SONNET_MODEL='${ANTHROPIC_DEFAULT_SONNET_MODEL}' \
    -e ANTHROPIC_AUTH_TOKEN='${ANTHROPIC_AUTH_TOKEN}' \
    -e CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 \
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

  pushd "${PROJ_DIR}/agents" > /dev/null
  if [[ ! -d "${CH_STORAGE}/agent-base" ]]; then
    ch-image build -t agent-base -f Containerfile.base .
    ch-convert -i ch-image -o dir agent-base "${CH_STORAGE}/agent-base"
  fi
  if [[ ! -d "${CH_STORAGE}/claude-code" ]]; then
    rm -rf certs
    mkdir certs
    cp ${HOME}/.local/share/certs/* certs/ 2>/dev/null || true
    ch-image build -t claude-code -f Containerfile.claude-code .
    ch-convert -i ch-image -o dir claude-code "${CH_STORAGE}/claude-code"
  fi
  popd > /dev/null

  CMD="ch-run --write-fake --private-tmp -b ${APP_DIR}:/app"
  if [[ -n "${BUILD_DIR}" ]]; then
    CMD="${CMD} -b ${BUILD_DIR}:/build"
  fi
  CMD="${CMD} \
    -b ${HOME}/.claude/:/root/.claude/ \
    --set-env ANTHROPIC_BASE_URL='${ANTHROPIC_BASE_URL}' \
    --set-env ANTHROPIC_DEFAULT_SONNET_MODEL='${ANTHROPIC_DEFAULT_SONNET_MODEL}' \
    --set-env ANTHROPIC_AUTH_TOKEN='${ANTHROPIC_AUTH_TOKEN}' \
    --set-env CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 \
    --cd /app \
    ${CH_STORAGE}/claude-code -- /root/.local/bin/claude${CLAUDE_CODE_ARGS}"
  
  eval "${CMD}"

else
  echo "Error: Unsupported container type: ${CONTAINER_TYPE}"
  exit 1
fi
