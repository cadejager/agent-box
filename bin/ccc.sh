#!/usr/bin/env bash
#
# Launches claude-code in a container (podman or charliecloud)

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
  echo "  Claude Code session:"
  echo "    -c       Continue the most recent session"
  echo "    -s ID    Resume session ID (omit ID for the interactive picker)"
  echo "    -f       Fork instead of resume (use with -s or -c)"
  echo "  Pass-through after -- (common claude flags):"
  echo "    --model sonnet|opus|<name>    --effort low|medium|high|xhigh|max"
  echo "    -p (print/non-interactive)    --permission-mode plan|acceptEdits|..."
  echo "    --add-dir DIR    --agent NAME"
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

agent::launch
