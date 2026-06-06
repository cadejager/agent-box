#!/usr/bin/env bash
#
# Launches Claude Desktop (GUI) in a podman container
#
# The GUI is shown by bind-mounting the host Wayland socket straight into the
# container (native Wayland, no SSH/X forwarding). Claude Desktop's own inner
# sandboxes are disabled because the container itself is the access-control
# boundary:
#   - Electron is run with --no-sandbox (Chromium's setuid/userns sandbox
#     cannot nest inside a rootless container, and refuses to run as root).
#   - Cowork is pinned to COWORK_VM_BACKEND=host so it never tries to spin up
#     a bubblewrap/KVM sandbox inside this container.
#
# Native Wayland trade-off: the global hotkey (Ctrl+Alt+Space) does not work
# in native Wayland mode (a Chromium GlobalShortcuts-portal limitation).

set -eo pipefail

# Get the dir of the project
PROJ_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )/.." &> /dev/null && pwd )

# Defaults
APP_DIR=$(pwd)
REBUILD=false
NO_GPU=false
VOLUMES=()

usage() {
  echo "Usage: ${0} [-a APP_DIR] [-v VOLUME] [-g] [-r] [-h]"
  echo "  Container args:"
  echo "    -a       The application directory (default: current dir)"
  echo "             Will be mounted at the same path inside the container"
  echo "    -v       Additional volume to mount (can be specified multiple times)"
  echo "             Path will be mounted at the same location inside the container"
  echo "    -r       Rebuild images"
  echo "  Claude Desktop args:"
  echo "    -g       Disable GPU acceleration (software rendering; CLAUDE_DISABLE_GPU=1)"
  echo ""
  echo "  -h       Display this message"
  exit 1
}

while getopts "a:v:grh" opt; do
  case ${opt} in
    a) APP_DIR=$OPTARG ;;
    v) VOLUMES+=("$OPTARG") ;;
    g) NO_GPU=true ;;
    r) REBUILD=true ;;
    h|?) usage ;;
  esac
done

if ! command -v podman >/dev/null 2>&1; then
  echo "Error: podman not found."
  exit 1
fi

# The GUI passthrough requires a Wayland session
if [[ -z "${WAYLAND_DISPLAY}" || -z "${XDG_RUNTIME_DIR}" ]]; then
  echo "Error: WAYLAND_DISPLAY / XDG_RUNTIME_DIR not set - not a Wayland session?"
  echo "This launcher displays the GUI by mounting the host Wayland socket."
  exit 1
fi

WL_SOCK="${XDG_RUNTIME_DIR}/${WAYLAND_DISPLAY}"
if [[ ! -S "${WL_SOCK}" ]]; then
  echo "Error: Wayland socket not found at ${WL_SOCK}"
  exit 1
fi

# Ensure APP_DIR is absolute
APP_DIR=$(realpath "$APP_DIR")

# Convert all volume paths to absolute paths
for i in "${!VOLUMES[@]}"; do
  VOLUMES[i]=$(realpath "${VOLUMES[i]}")
done

# Persisted Claude Desktop config (login/OAuth token, MCP config, logs)
mkdir -p "${HOME}/.config/Claude"

# Runtime dir inside the container that holds the Wayland (and dbus) sockets
CTR_RUNTIME="/tmp/wl"

if [[ "true" == "${REBUILD}" ]]; then
  podman image rm agent-base 2>/dev/null || true
  podman image rm claude-desktop 2>/dev/null || true
fi

pushd "${PROJ_DIR}/agents" > /dev/null || exit
if ! podman image exists agent-base; then
  podman build -t agent-base -f Containerfile.base .
fi
if ! podman image exists claude-desktop; then
  rm -rf certs
  mkdir certs
  cp "${HOME}"/.local/share/certs/* certs/ 2>/dev/null || true
  podman build -t claude-desktop -f Containerfile.claude-desktop .
fi
popd > /dev/null || exit

CMD="podman run --rm -v '${APP_DIR}':'${APP_DIR}' -w '${APP_DIR}'"
# Add additional volumes
for vol in "${VOLUMES[@]}"; do
  CMD="${CMD} -v '${vol}':'${vol}'"
done

# GPU acceleration vs software rendering
if [[ "true" == "${NO_GPU}" ]]; then
  GPU_ARGS="-e CLAUDE_DISABLE_GPU=1"
else
  GPU_ARGS="--device /dev/dri"
fi

# Pass the session D-Bus socket if present (file dialogs, portals, tray)
DBUS_ARGS=""
if [[ -S "${XDG_RUNTIME_DIR}/bus" ]]; then
  DBUS_ARGS="-v '${XDG_RUNTIME_DIR}/bus':'${CTR_RUNTIME}/bus' \
    -e DBUS_SESSION_BUS_ADDRESS=unix:path=${CTR_RUNTIME}/bus"
fi

CMD="${CMD} \
  -v '${HOME}/.config/Claude/':'/root/.config/Claude/' \
  -v '${WL_SOCK}':'${CTR_RUNTIME}/${WAYLAND_DISPLAY}' \
  -e XDG_RUNTIME_DIR='${CTR_RUNTIME}' \
  -e WAYLAND_DISPLAY='${WAYLAND_DISPLAY}' \
  -e CLAUDE_USE_WAYLAND=1 \
  -e COWORK_VM_BACKEND=host \
  --shm-size=2g \
  ${GPU_ARGS} \
  ${DBUS_ARGS} \
  claude-desktop claude-desktop --no-sandbox"

eval "${CMD}"