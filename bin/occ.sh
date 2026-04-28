#!/usr/bin/env bash
#
# Launches opencode in a container (podman or charliecloud)

# Get the dir of the project
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/.." &> /dev/null && pwd )

# Defaults
APP_DIR=$(pwd)
BUILD_DIR=""
CONTAINER_TYPE=""
REBUILD=false
CONTINUE=false
SESSION=""
FORK=false

usage() {
  echo "Usage: ${0} [-a APP_DIR] [-b BUILD_DIR] [-t TYPE] [-c] [-s SESSION] [-f] [-r] [-h]"
  echo "  Container args:"
  echo "    -a       The application directory to bind to /app (default: current dir)"
  echo "    -b       The build directory to bind to /build"
  echo "    -r       Rebuild images"
  echo "    -t       The container engine to use (podman or charliecloud)"
  echo "  Opencode args:"
  echo "    -c       Continue the last session"
  echo "    -s       Continue a specific session by ID"
  echo "    -f       Fork the session (use with -c or -s)"
  echo ""
  echo "  -h       Display this message"
  exit 1
}

while getopts "a:b:t:cs:frh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    b) BUILD_DIR=$OPTARG ;;
    t) CONTAINER_TYPE=$OPTARG ;;
    c) CONTINUE=true ;;
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

# Build opencode arguments
OPENCODE_ARGS=""
if [[ "${CONTINUE}" == "true" ]]; then
  OPENCODE_ARGS="${OPENCODE_ARGS} --continue"
fi
if [[ -n "${SESSION}" ]]; then
  OPENCODE_ARGS="${OPENCODE_ARGS} --session ${SESSION}"
fi
if [[ "${FORK}" == "true" ]]; then
  OPENCODE_ARGS="${OPENCODE_ARGS} --fork"
fi

if [[ "${CONTAINER_TYPE}" == "podman" ]]; then
  if [[ "true" == "${REBUILD}" ]]; then
    podman image rm agent-base 2>/dev/null || true
    podman image rm opencode 2>/dev/null || true
  fi

  pushd "${PROJ_DIR}/agents" > /dev/null
  if ! podman image exists agent-base; then
    podman build -t agent-base -f Containerfile.base .
  fi
  if ! podman image exists opencode; then
    rm -rf certs
    mkdir certs
    cp ${HOME}/.local/share/certs/* certs/ 2>/dev/null || true
    podman build -t opencode -f Containerfile.opencode .
  fi
  popd > /dev/null

  CMD="podman run -it --rm -v ${APP_DIR}:/app"
  if [[ -n "${BUILD_DIR}" ]]; then
    CMD="${CMD} -v ${BUILD_DIR}:/build"
  fi
  CMD="${CMD} -v ${HOME}/.config/opencode/:/root/.config/opencode/ \
    -v ${HOME}/.local/share/opencode/:/root/.local/share/opencode/ \
    -e OPENCODE_ENABLE_EXA=1 \
    -e OPENCODE_EXPERIMENTAL_LSP_TOOL=true \
    opencode /usr/local/bin/opencode${OPENCODE_ARGS}"
  
  eval "${CMD}"

elif [[ "${CONTAINER_TYPE}" == "charliecloud" ]]; then
  # Charliecloud storage directory
  CH_STORAGE="${PROJ_DIR}/agents/.charliecloud"
  mkdir -p "${CH_STORAGE}"

  if [[ "true" == "${REBUILD}" ]]; then
    rm -rf "${CH_STORAGE}/agent-base"
    ch-image delete agent-base 2>/dev/null || true
    rm -rf "${CH_STORAGE}/opencode"
    ch-image delete opencode 2>/dev/null || true
  fi

  pushd "${PROJ_DIR}/agents" > /dev/null
  if [[ ! -d "${CH_STORAGE}/agent-base" ]]; then
    ch-image build -t agent-base -f Containerfile.base .
    ch-convert -i ch-image -o dir agent-base "${CH_STORAGE}/agent-base"
  fi
  if [[ ! -d "${CH_STORAGE}/opencode" ]]; then
    rm -rf certs
    mkdir certs
    cp ${HOME}/.local/share/certs/* certs/ 2>/dev/null || true
    ch-image build -t opencode -f Containerfile.opencode .
    ch-convert -i ch-image -o dir opencode "${CH_STORAGE}/opencode"
  fi
  popd > /dev/null

  CMD="ch-run --write-fake --private-tmp -b ${APP_DIR}:/app"
  if [[ -n "${BUILD_DIR}" ]]; then
    CMD="${CMD} -b ${BUILD_DIR}:/build"
  fi
  CMD="${CMD} -b ${HOME}/.config/opencode/:/root/.config/opencode/ \
    -b ${HOME}/.local/share/opencode/:/root/.local/share/opencode/ \
    --set-env=OPENCODE_ENABLE_EXA=1 \
    --set-env=OPENCODE_EXPERIMENTAL_LSP_TOOL=true \
    --cd /app \
    ${CH_STORAGE}/opencode -- /usr/local/bin/opencode${OPENCODE_ARGS}"
  
  eval "${CMD}"

else
  echo "Error: Unsupported container type: ${CONTAINER_TYPE}"
  exit 1
fi
