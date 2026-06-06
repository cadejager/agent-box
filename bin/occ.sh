#!/usr/bin/env bash
#
# Launches opencode in a container (podman or charliecloud)

set -eo pipefail

# shellcheck source=lib/agent-run.sh
source "$( dirname -- "${BASH_SOURCE[0]}" )/lib/agent-run.sh"

# Defaults
APP_DIR=$(pwd)
CONTAINER_TYPE=""
REBUILD=false
CONTINUE=false
SESSION=""
FORK=false
VOLUMES=()

usage() {
  echo "Usage: ${0} [-a DIR] [-v VOL] [-t TYPE] [-c] [-s SESSION] [-f] [-r] [-h] [-- ARGS...]"
  agent::usage_container
  echo "  Opencode session:"
  echo "    -c       Continue the last session"
  echo "    -s ID    Continue session ID"
  echo "    -f       Fork the session (use with -s or -c)"
  echo "  Pass-through after -- (common opencode flags):"
  echo "    -m provider/model    --agent NAME    --pure"
  echo "    headless: run \"MESSAGE\" [--format json] [--variant high|max|minimal]"
  echo "    -h       Show this help"
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

# Pass-through: everything after `--` is forwarded to the agent verbatim.
shift $((OPTIND - 1))
EXTRA_ARGS=("$@")

# Build opencode arguments
AGENT_ARGS=()
if [[ "${CONTINUE}" == "true" ]]; then
  AGENT_ARGS+=(--continue)
fi
if [[ -n "${SESSION}" ]]; then
  AGENT_ARGS+=(--session "${SESSION}")
fi
if [[ "${FORK}" == "true" ]]; then
  AGENT_ARGS+=(--fork)
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

agent::launch
