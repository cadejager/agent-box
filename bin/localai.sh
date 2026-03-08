#!/usr/bin/env bash
#
# Controls the ollama instance

# This gets the dir of the project
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/.." &> /dev/null && pwd )
cd "${PROJ_DIR}/ollama"

# Get Args
usage() {
  echo "Usage: ${0} [-h] COMMAND"
  echo "  COMMAND [REQUIRED] up|down|attach"
  echo
  echo "  -h      Display this message"
  exit 1
}
while getopts "f:vh" opt; do
  case ${opt} in
    f) file="$OPTARG" ;; # -f requires an argument (due to the colon)
    v) verbose=true ;;   # -v is a simple flag
    h|?) usage ;;
  esac
done
shift $((OPTIND-1)) # Shift away the options processed by getopts
if [[ -z "${1}" ]]; then
  echo "Error: Missing COMMAND."
  usage
fi
COMMAND="${1}"

up() {
  podman-compose up -d
}
down() {
  podman-compose down
}
attach() {
  if [[ "$(podman inspect ollama -f '{{.State.Running}}' 2>/dev/null)"  != "true" ]]; then
    up
  fi
  podman exec -it ollama /bin/bash
}

case "${COMMAND}" in
  down) down ;;
  up) up ;;
  attach) attach ;;
  *) echo "Unkown Command '${COMMAND}'"; usage ;;
esac

