#!/usr/bin/env bash
#
# Launches codex (OpenAI Codex CLI) in a container (podman or charliecloud)

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
  echo "Usage: ${0} [-a DIR] [-v VOL] [-t TYPE] [-c] [-r ID] [-f] [-b] [-h] [-- ARGS...]"
  agent::usage_container
  echo "  Codex session (mapped to resume/fork subcommands):"
  echo "    -c       Resume the most recent session"
  echo "    -r ID    Resume session ID"
  echo "    -f       Fork instead of resume (use with -r, or alone for the latest)"
  echo "  Pass-through after -- (common codex flags):"
  echo "    -m MODEL    -c model_reasoning_effort=high   (codex's own -c = config)"
  echo "    --oss --local-provider ollama    --sandbox workspace-write"
  echo "    --search    --ask-for-approval on-request"
  echo "    -h       Show this help"
  exit 1
}

while getopts "a:v:t:cr:fbh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    v) VOLUMES+=("$OPTARG") ;;
    t) CONTAINER_TYPE=$OPTARG ;;
    c) CONTINUE=true ;;
    r) SESSION=$OPTARG ;;
    f) FORK=true ;;
    b) REBUILD=true ;;
    h|?) usage ;;
  esac
done

# Pass-through: everything after `--` is forwarded to the agent verbatim.
shift $((OPTIND - 1))
EXTRA_ARGS=("$@")

# Build codex arguments. Unlike claude/opencode, codex uses SUBCOMMANDS
# (resume / fork), not flags, and the session id is positional -- so the
# subcommand tokens must be the FIRST elements of AGENT_ARGS.
#   -c        -> resume --last   (reattach the most recent session)
#   -r ID     -> resume ID
#   -f        -> fork --last
#   -f -r ID  -> fork ID
# fork and resume are mutually exclusive subcommands; with no flag codex starts
# a fresh session.
AGENT_ARGS=()
if [[ "${FORK}" == "true" ]]; then
  AGENT_ARGS+=(fork)
  if [[ -n "${SESSION}" ]]; then
    AGENT_ARGS+=("${SESSION}")
  else
    AGENT_ARGS+=(--last)
  fi
elif [[ "${CONTINUE}" == "true" || -n "${SESSION}" ]]; then
  AGENT_ARGS+=(resume)
  if [[ -n "${SESSION}" ]]; then
    AGENT_ARGS+=("${SESSION}")
  else
    AGENT_ARGS+=(--last)
  fi
fi

# Agent-specific container configuration
IMAGE="codex"
CONTAINERFILE="Containerfile.codex"
AGENT_BIN="/usr/local/bin/codex"
CONFIG_MOUNTS=(
  "${HOME}/.codex/:/root/.codex/"
)
# Forward an API key only when set, so it doesn't shadow a mounted ~/.codex login.
# (The local-model endpoint can't be set via env -- OPENAI_BASE_URL is ignored by
# current codex -- it must live in ~/.codex/config.toml.)
ENV_FORWARD=(OPENAI_API_KEY CODEX_API_KEY)
ENV_LITERAL=()

agent::launch
