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
RESUME=false
SESSION=""
FORK=false
VOLUMES=()
HOST_PORTS=()

usage() {
  echo "Usage: ${0} [-a DIR] [-v VOL] [-t TYPE] [-c] [-r [ID]] [-f] [-b] [-p PORT] [-h] [-- ARGS...]"
  agent::usage_container
  echo "  Claude Code session:"
  echo "    -c       Continue the most recent session"
  echo "    -r [ID]  Resume session ID, or open the interactive picker if no ID"
  echo "    -f       Fork instead of resume (use with -r or -c)"
  echo "  Pass-through after -- (common claude flags):"
  echo "    --model sonnet|opus|<name>    --effort low|medium|high|xhigh|max"
  echo "    -p (print/non-interactive)    --permission-mode plan|acceptEdits|..."
  echo "    --add-dir DIR    --agent NAME"
  echo "    -h       Show this help"
  exit 1
}

while getopts "a:v:t:p:crfbh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    v) VOLUMES+=("$OPTARG") ;;
    p) HOST_PORTS+=("$OPTARG") ;;
    t) CONTAINER_TYPE=$OPTARG ;;
    c) CONTINUE=true ;;
    r) RESUME=true ;;
    f) FORK=true ;;
    b) REBUILD=true ;;
    h|?) usage ;;
  esac
done

# Pass-through: everything after `--` is forwarded to the agent verbatim.
shift $((OPTIND - 1))
# -r takes an OPTIONAL session id: grab the first leftover token unless it
# looks like an option, so bare -r opens the picker. Done after getopts so
# it works regardless of where -r sits in a combined cluster (e.g. -rc ID).
if [[ "${RESUME}" == "true" && -z "${SESSION}" && $# -gt 0 && "$1" != -* ]]; then
  SESSION="$1"
  shift
fi
EXTRA_ARGS=("$@")

# Build claude-code arguments
AGENT_ARGS=()
if [[ "${CONTINUE}" == "true" ]]; then
  AGENT_ARGS+=(--continue)
fi
if [[ "${RESUME}" == "true" ]]; then
  if [[ -n "${SESSION}" ]]; then
    AGENT_ARGS+=(--resume "${SESSION}")
  else
    AGENT_ARGS+=(--resume)
  fi
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
