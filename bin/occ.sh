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
RO_VOLUMES=()

usage() {
  echo "Usage: ${0} [-a DIR] [-v VOL] [-r VOL] [-t TYPE] [-c] [-s [ID]] [-f] [-b] [-h] [-- ARGS...]"
  agent::usage_container
  echo "  Opencode session:"
  echo "    -c       Continue the last session"
  echo "    -s [ID]  Continue session ID (omit ID to just launch; pick in the UI)"
  echo "    -f       Fork the session (use with -s or -c)"
  echo "  Pass-through after -- (common opencode flags):"
  echo "    -m provider/model    --agent NAME    --pure"
  echo "    headless: run \"MESSAGE\" [--format json] [--variant high|max|minimal]"
  echo "    -h       Show this help"
  exit 1
}

while getopts "a:v:t:r:csfbh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    v) VOLUMES+=("$OPTARG") ;;
    r) RO_VOLUMES+=("$OPTARG") ;;
    t) CONTAINER_TYPE=$OPTARG ;;
    c) CONTINUE=true ;;
    s) RESUME=true ;;
    f) FORK=true ;;
    b) REBUILD=true ;;
    h|?) usage ;;
  esac
done

# Pass-through: everything after `--` is forwarded to the agent verbatim.
shift $((OPTIND - 1))
# -s takes an OPTIONAL session id: grab the first leftover token unless it
# looks like an option, so bare -s opens the picker. Done after getopts so
# it works regardless of where -s sits in a combined cluster (e.g. -sc ID).
if [[ "${RESUME}" == "true" && -z "${SESSION}" && $# -gt 0 && "$1" != -* ]]; then
  SESSION="$1"
  shift
fi
EXTRA_ARGS=("$@")

# Build opencode arguments. opencode's --session requires an id, so a bare -s
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
# opencode spreads its state across four XDG dirs: ~/.config (user config),
# ~/.local/share (auth.json + session db), ~/.cache, and ~/.local/state. Mount
# all four so login, sessions, and cache survive the ephemeral --rm container.
CONFIG_MOUNTS=(
  "${HOME}/.config/opencode/:/root/.config/opencode/"
  "${HOME}/.local/share/opencode/:/root/.local/share/opencode/"
  "${HOME}/.cache/opencode/:/root/.cache/opencode/"
  "${HOME}/.local/state/opencode/:/root/.local/state/opencode/"
)
ENV_FORWARD=()
ENV_LITERAL=(OPENCODE_ENABLE_EXA=1 OPENCODE_EXPERIMENTAL_LSP_TOOL=true)

agent::launch
