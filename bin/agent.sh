#!/usr/bin/env bash
#
# Launches/updates an AI agent

# The default model to use
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
pushd "${PROJ_DIR}/agents"
if ! podman image exists agent-base; then
  rm -rf certs
  mkdir certs
  cp ${HOME}/.local/share/certs/* certs/
  podman build -t agent-base -f Containerfile.base .
fi
if ! podman image exists "${AGENT}"; then
  podman build -t ${AGENT} -f Containerfile.${AGENT} .
fi
popd

if [[ "claude-code" == ${AGENT} ]]; then
  podman run -it --rm -v ./:/app ${AGENT} /root/.local/bin/claude --model "${MODEL}"
elif [[ "codex" == ${AGENT} ]]; then
  podman run -it --rm -v ./:/app -v "${PROJ_DIR}/agents/codex_config.toml":/root/.codex/config.toml ${AGENT} /usr/local/bin/codex --model "${MODEL}"
elif [[ "opencode" == ${AGENT} ]]; then
  # ~/.config/opencode/opencode.json and ~/.local/share/opencode/auth.json need to be crated for
  # this to work.
  #
  # auth.json can be populated within opencode by using `/connect`
  # 
  # opencode.json needs to be configured for each endpoint
  # avaiable models can be gotten with commands like:
  # Ollama: `curl http://localhost:11434/api/tags`
  # LiteLLM: `curl https://hostname/v1/models -H "Authorization: Bearer TOKEN"`
  #
  # Configuration examples are avaiable here:
  # https://opencode.ai/docs/providers
  #
  # baseurl should be http://host.containers.internal if Ollama is running in another container

  podman run -it --rm -v ./:/app \
    -v "${HOME}/.config/opencode/opencode.json":/root/.config/opencode/opencode.json \
    -v "${HOME}/.local/share/opencode/auth.json":/root/.local/share/opencode/auth.json \
    -e OPENCODE_ENABLE_EXA=1 \
    -e OPENCODE_EXPERIMENTAL_LSP_TOOL=true \
    ${AGENT} /usr/local/bin/opencode
fi
