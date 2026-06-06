#!/usr/bin/env bash
#
# Launches codex (OpenAI Codex CLI) in a container (podman or charliecloud)

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
  echo "Usage: ${0} [-a APP_DIR] [-v VOLUME] [-t TYPE] [-c] [-s SESSION] [-f] [-r] [-h] [-- ARGS...]"
  echo "  Container args:"
  echo "    -a       The application directory (default: current dir)"
  echo "             Will be mounted at the same path inside the container"
  echo "    -v       Additional volume to mount (can be specified multiple times)"
  echo "             Path will be mounted at the same location inside the container"
  echo "    -r       Rebuild images"
  echo "    -t       The container engine to use (podman or charliecloud)"
  echo "  Codex args:"
  echo "    -c       Resume (reattach) your most recent session"
  echo "    -s       Resume a specific session by ID"
  echo "    -f       Fork instead of resume (with -s, or the latest session)"
  echo "    --       Pass all following arguments straight through to codex"
  echo "             (e.g. -- -m MODEL, or -- -c model_reasoning_effort=high)"
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

# Pass-through: anything after `--` (or any bare args) goes to the agent.
shift $((OPTIND - 1))
EXTRA_ARGS=("$@")

# Build codex arguments. Unlike claude/opencode, codex uses SUBCOMMANDS
# (resume / fork), not flags, and the session id is positional -- so the
# subcommand tokens must be the FIRST elements of AGENT_ARGS.
#   -c        -> resume --last   (reattach the most recent session)
#   -s ID     -> resume ID
#   -f        -> fork --last
#   -f -s ID  -> fork ID
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

# shellcheck source=lib/agent-run.sh
source "$( dirname -- "${BASH_SOURCE[0]}" )/lib/agent-run.sh"
agent::launch
