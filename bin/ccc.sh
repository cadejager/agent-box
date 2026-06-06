#!/usr/bin/env bash
#
# Launches claude-code in a container (podman or charliecloud)

set -eo pipefail

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
  echo "Usage: ${0} [-a APP_DIR] [-v VOLUME] [-t TYPE] [-c]  [-e EFFORT] [-s SESSION] [-f] [-r] [-h] [-- ARGS...]"
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
  echo "    --       Pass all following arguments straight through to claude"
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

# Pass-through: anything after `--` (or any bare args) goes to the agent.
shift $((OPTIND - 1))
EXTRA_ARGS=("$@")

# Build claude-code arguments
AGENT_ARGS=()
if [[ "${CONTINUE}" == "true" ]]; then
  AGENT_ARGS+=(--continue)
fi
if [[ -n "${SESSION}" ]]; then
  AGENT_ARGS+=(--resume "${SESSION}")
fi
if [[ "${FORK}" == "true" ]]; then
  AGENT_ARGS+=(--fork-session)
fi
if [[ -n "${EFFORT}" ]]; then
  AGENT_ARGS+=(--effort "${EFFORT}")
fi

# Agent-specific container configuration
IMAGE="claude-code"
CONTAINERFILE="Containerfile.claude-code"
AGENT_BIN="/usr/local/bin/claude"
CONFIG_MOUNTS=(
  "${HOME}/.claude/:/root/.claude/"
  "${HOME}/.claude.json:/root/.claude.json"
)
# Forward these host env vars only when set, so empty values don't shadow the
# mounted ~/.claude.json.
ENV_FORWARD=(ANTHROPIC_BASE_URL ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_AUTH_TOKEN)
ENV_LITERAL=(CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1)

# shellcheck source=lib/agent-run.sh
source "$( dirname -- "${BASH_SOURCE[0]}" )/lib/agent-run.sh"
agent::launch
