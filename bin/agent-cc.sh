#!/usr/bin/env bash
#
# Launches/updates an AI agent using Charliecloud
#

# The default model to use
MODEL="gpt-oss-64k:20b"

# This gets the dir of the project
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/.." &> /dev/null && pwd )

# Charliecloud storage directory
CH_STORAGE="${PROJ_DIR}/agents/.charliecloud"
mkdir -p "${CH_STORAGE}"

# Get Args
usage() {
  echo "Usage: ${0} [-m MODEL] [-h] [-r] agent"
  echo "  -m       The model to use"
  echo "  -r       Rebuild"
  echo "  -h       Display this message"
  exit 1
}
while getopts "m:rh" opt; do
  case ${opt} in
    m) MODEL=$OPTARG ;;
    r) REBUILD=true ;;
    h|?) usage ;;
  esac
done
shift $((OPTIND-1)) # Shift away the options processed by getopts
if [[ -z "${1}" ]]; then
  echo "Error: Missing COMMAND."
  usage
fi
AGENT="${1}"

if [[ "true" == "${REBUILD}" ]]; then
  rm -rf "${CH_STORAGE}/${AGENT}"
  rm -rf "${CH_STORAGE}/agent-base"
  # Also remove from ch-image storage if exists
  ch-image delete ${AGENT} 2>/dev/null || true
  ch-image delete agent-base 2>/dev/null || true
fi

pushd "${PROJ_DIR}/agents"

# Build base image if it doesn't exist
if [[ ! -d "${CH_STORAGE}/agent-base" ]]; then
  echo "Building agent-base image..."
  ch-image build -t agent-base -f Containerfile.base .
  ch-convert -i ch-image -o dir agent-base "${CH_STORAGE}/agent-base"
fi

# Build agent-specific image if it doesn't exist
if [[ ! -d "${CH_STORAGE}/${AGENT}" ]]; then
  echo "Building ${AGENT} image..."
  rm -rf certs
  mkdir certs
  cp ${HOME}/.local/share/certs/* certs/ 2>/dev/null || true
  ch-image build -t ${AGENT} -f Containerfile.${AGENT} .
  ch-convert -i ch-image -o dir ${AGENT} "${CH_STORAGE}/${AGENT}"
fi

popd

# Run the appropriate agent
if [[ "claude-code" == ${AGENT} ]]; then
  ch-run -b "${PROJ_DIR}":/app --cd /app \
    "${CH_STORAGE}/${AGENT}" -- /root/.local/bin/claude --model "${MODEL}"
elif [[ "codex" == ${AGENT} ]]; then
  ch-run -b "${PROJ_DIR}":/app \
    -b "${PROJ_DIR}/agents/codex_config.toml":/root/.codex/config.toml \
    --cd /app \
    "${CH_STORAGE}/${AGENT}" -- /usr/local/bin/codex --model "${MODEL}"
elif [[ "opencode" == ${AGENT} ]]; then
  # ~/.config/opencode/opencode.json and ~/.local/share/opencode/auth.json need to be created for
  # this to work.
  #
  # auth.json can be populated within opencode by using `/connect`
  # 
  # opencode.json needs to be configured for each endpoint
  # available models can be gotten with commands like:
  # Ollama: `curl http://localhost:11434/api/tags`
  # LiteLLM: `curl https://hostname/v1/models -H "Authorization: Bearer TOKEN"`
  #
  # Configuration examples are available here:
  # https://opencode.ai/docs/providers
  #
  # Note: Charliecloud doesn't have the same host.containers.internal feature as Podman
  # You may need to use the host's actual IP address or use --join-ns for networking

  ch-run --write \
    -b "${PROJ_DIR}":/app \
    -v "${HOME}/.config/opencode/opencode.json":/root/.config/opencode/opencode.json \
    -v "${HOME}/.local/share/opencode/auth.json":/root/.local/share/opencode/auth.json \
    --set-env=OPENCODE_ENABLE_EXA=1 \
    --set-env=OPENCODE_EXPERIMENTAL_LSP_TOOL=true \
    --cd /app \
    "${CH_STORAGE}/${AGENT}" -- /usr/local/bin/opencode
fi
