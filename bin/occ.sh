#!/usr/bin/env bash
#
# Launches opencode in a container (podman or charliecloud)

set -eo pipefail

# Defaults
APP_DIR=$(pwd)
CONTAINER_TYPE=""
REBUILD=false
CONTINUE=false
SESSION=""
FORK=false
VOLUMES=()

usage() {
  echo "Usage: ${0} [-a APP_DIR] [-v VOLUME] [-t TYPE] [-c] [-s SESSION] [-f] [-r] [-h]"
  echo "  Container args:"
  echo "    -a       The application directory (default: current dir)"
  echo "             Will be mounted at the same path inside the container"
  echo "    -v       Additional volume to mount (can be specified multiple times)"
  echo "             Path will be mounted at the same location inside the container"
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

while getopts "a:v:t:cs:frh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    v) VOLUMES+=("$OPTARG") ;;
    t) CONTAINER_TYPE=$OPTARG ;;
    c) CONTINUE=true ;;
    s) SESSION=$OPTARG ;;
    f) FORK=true ;;
    r) REBUILD=true ;;
    h|?) usage ;;
  esac
done

# Build opencode arguments
AGENT_ARGS=""
if [[ "${CONTINUE}" == "true" ]]; then
  AGENT_ARGS="${AGENT_ARGS} --continue"
fi
if [[ -n "${SESSION}" ]]; then
  AGENT_ARGS="${AGENT_ARGS} --session ${SESSION}"
fi
if [[ "${FORK}" == "true" ]]; then
  AGENT_ARGS="${AGENT_ARGS} --fork"
fi

# Agent-specific container configuration
IMAGE="opencode"
CONTAINERFILE="Containerfile.opencode"
AGENT_BIN="/usr/local/bin/opencode"
CONFIG_MOUNTS=(
  "${HOME}/.config/opencode/:/root/.config/opencode/"
  "${HOME}/.local/share/opencode/:/root/.local/share/opencode/"
)
ENV_FORWARD=()
ENV_LITERAL=(OPENCODE_ENABLE_EXA=1 OPENCODE_EXPERIMENTAL_LSP_TOOL=true)

# shellcheck source=lib/agent-run.sh
source "$( dirname -- "${BASH_SOURCE[0]}" )/lib/agent-run.sh"
agent::launch
