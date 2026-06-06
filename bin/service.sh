#!/usr/bin/env bash
#
# Controls a compose-based service (ollama or localai): up | down | attach.
set -eo pipefail

# Get the dir of the project
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/.." &> /dev/null && pwd )

usage() {
  echo "Usage: ${0##*/} [-h] SERVICE COMMAND"
  echo "  SERVICE [REQUIRED] ollama|localai"
  echo "  COMMAND [REQUIRED] up|down|attach"
  echo
  echo "  -h      Display this message"
  exit 1
}

while getopts "h" opt; do
  case ${opt} in
    h|?) usage ;;
  esac
done
shift $((OPTIND - 1))

SERVICE="${1:-}"
COMMAND="${2:-}"

case "${SERVICE}" in
  ollama|localai) ;;
  *) echo "Error: invalid or missing SERVICE '${SERVICE}'."; usage ;;
esac
if [[ -z "${COMMAND}" ]]; then
  echo "Error: Missing COMMAND."
  usage
fi

cd "${PROJ_DIR}/${SERVICE}"

# Resolve a Compose provider. The compose files target podman; prefer
# podman-compose, then fall back to podman's built-in `compose` subcommand.
compose() {
  if command -v podman-compose >/dev/null 2>&1; then
    podman-compose "$@"
  elif podman compose version >/dev/null 2>&1; then
    podman compose "$@"
  else
    echo "Error: no Compose provider found. Install podman-compose" \
         "(e.g. 'pipx install podman-compose') or a 'podman compose' provider." >&2
    exit 1
  fi
}

up() {
  compose up -d
}
down() {
  compose down
}
attach() {
  if [[ "$(podman inspect "${SERVICE}" -f '{{.State.Running}}' 2>/dev/null)" != "true" ]]; then
    up
  fi
  podman exec -it "${SERVICE}" /bin/bash
}

case "${COMMAND}" in
  up) up ;;
  down) down ;;
  attach) attach ;;
  *) echo "Unknown command '${COMMAND}'."; usage ;;
esac
