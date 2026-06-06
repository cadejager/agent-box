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
RESUME=false
SESSION=""
FORK=false
VOLUMES=()

usage() {
  echo "Usage: ${0} [-a DIR] [-v VOL] [-t TYPE] [-c] [-r [ID]] [-f] [-b] [-h] [-- ARGS...]"
  agent::usage_container
  echo "  Codex session (mapped to resume/fork subcommands):"
  echo "    -c       Resume the most recent session (resume --last)"
  echo "    -r [ID]  Resume session ID, or open the picker if no ID"
  echo "    -f       Fork instead of resume (with -c/-r, or alone for the picker)"
  echo "  Pass-through after -- (common codex flags):"
  echo "    -m MODEL    -c model_reasoning_effort=high   (codex's own -c = config)"
  echo "    --oss --local-provider lmstudio  --sandbox workspace-write"
  echo "    --search    --ask-for-approval on-request"
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
       # next token only if it isn't another option, so bare `-r` opens codex's
       # interactive picker.
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

# Build codex arguments. codex uses SUBCOMMANDS (resume / fork), not flags, and
# the session id is positional, so the subcommand tokens come first. The session
# "target" is shared by resume and fork:
#   -r ID -> ID    |    -c -> --last (most recent)    |    -r (bare) -> picker
AGENT_ARGS=()
target=()
if [[ -n "${SESSION}" ]]; then
  target=("${SESSION}")
elif [[ "${CONTINUE}" == "true" ]]; then
  target=(--last)
fi
if [[ "${FORK}" == "true" ]]; then
  AGENT_ARGS=(fork "${target[@]}")
elif [[ "${CONTINUE}" == "true" || "${RESUME}" == "true" ]]; then
  AGENT_ARGS=(resume "${target[@]}")
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
