#!/usr/bin/env bash
#
# Launches/updates an AI agent

MODEL="gpt-oss-64k:20b"

# This gets the dir of the project
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/.." &> /dev/null && pwd )

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
  podman image rm agent-base
  podman image rm ${AGENT}
fi
if ! podman image exists agent-base; then
  podman build -t agent-base -f Containerfile.base "${PROJ_DIR}/agents"
fi
if ! podman image exists "$IMAGE_NAME"; then
  podman build -t ${AGENT} -f Containerfile.${AGENT} "${PROJ_DIR}/agents"
fi

#podman run -it --rm -v ./:/app ${IMAGE_NAME} /root/.local/bin/claude --model "${MODEL}"
if [[ "claude-code" == ${AGENT} ]]; then
  podman run -it --rm -v ./:/app ${AGENT} /root/.local/bin/claude --model "${MODEL}"
elif [[ "codex" == ${AGENT} ]]; then
  #podman run -it --rm -v ./:/app -v "${PROJ_DIR}/agents/codex_config.toml":/root/.codex/config.toml ${AGENT} /usr/local/bin/codex
  podman run -it --rm -v ./:/app -v "${PROJ_DIR}/agents/codex_config.toml":/root/.codex/config.toml ${AGENT} /bin/bash
elif [[ "opencode" == ${AGENT} ]]; then
  mkdir -p "${PROJ_DIR}/agents/opencode"
  curl -s http://localhost:11434/api/tags | jq '{
    "$schema": "https://opencode.ai/config.json",
    "provider": {
      "ollama": {
        "npm": "@ai-sdk/openai-compatible",
        "options": { "baseURL": "http://host.containers.internal:11434/v1" },
        "models": ([.models[] | {key: .name, value: {name: .name, tools: true}}] | from_entries)
      }
    }
  }' > "${PROJ_DIR}/agents/opencode.json"
  podman run -it --rm -v ./:/app -v "${PROJ_DIR}/agents/opencode.json":/root/.config/opencode/opencode.json ${AGENT} /root/.opencode/bin/opencode
fi
