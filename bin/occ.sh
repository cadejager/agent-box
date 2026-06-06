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
RESUME=false
SESSION=""
FORK=false
VOLUMES=()

usage() {
  echo "Usage: ${0} [-a DIR] [-v VOL] [-t TYPE] [-c] [-r [ID]] [-f] [-b] [-h] [-- ARGS...]"
  agent::usage_container
  echo "  Opencode session:"
  echo "    -c       Continue the last session"
  echo "    -r [ID]  Continue session ID (omit ID to just launch; pick in the UI)"
  echo "    -f       Fork the session (use with -r or -c)"
  echo "  Pass-through after -- (common opencode flags):"
  echo "    -m provider/model    --agent NAME    --pure"
  echo "    headless: run \"MESSAGE\" [--format json] [--variant high|max|minimal]"
  echo "    -h       Show this help"
  exit 1
}

while getopts "a:v:t:crfbh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    v) VOLUMES+=("$OPTARG") ;;
    t) CONTAINER_TYPE=$OPTARG ;;
    c) CONTINUE=true ;;
    r) RESUME=true
       # -r takes an OPTIONAL id (getopts can't, so do it by hand): grab the
       # next token only if it isn't another option.
       next="${!OPTIND:-}"
       if [[ -n "${next}" && "${next}" != -* ]]; then
         SESSION="${next}"
         OPTIND=$((OPTIND + 1))
       fi
       ;;
    f) FORK=true ;;
    b) REBUILD=true ;;
    h|?) usage ;;
  esac
done

# Pass-through: everything after `--` is forwarded to the agent verbatim.
shift $((OPTIND - 1))
EXTRA_ARGS=("$@")

# Build opencode arguments. opencode's --session requires an id, so a bare -r
# (no id) just launches opencode -- you switch sessions in its UI.
AGENT_ARGS=()
if [[ "${CONTINUE}" == "true" ]]; then
  AGENT_ARGS+=(--continue)
fi
if [[ "${RESUME}" == "true" && -n "${SESSION}" ]]; then
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
