#!/usr/bin/env bash
# Single source of truth for Agent Box's consolidated layout.
#
# Every agent's config AND the shared pip/npm caches live under one root: on the
# host that's ~/.config/agent-box, which the launcher bind-mounts to
# /root/.config/agent-box inside the container. This script (a) creates the
# target subdirs + seed files under <root>, and (b) with --symlinks, points each
# tool's expected ~/ path at them. It is idempotent and never clobbers anything
# that already exists.
#
#   config-layout.sh <root>             # host side: just create the sources
#   config-layout.sh <root> --symlinks  # + the in-container symlinks (image build)
#
# Because the launcher bind-mounts ONLY this one dir, every per-tool path is a
# symlink INTO it -- including ~/.claude.json and the caches.
#
# FRAGILITY NOTE (~/.claude.json): a *file* symlink only persists because Claude
# currently writes that file IN PLACE (it follows the symlink to the mounted
# host file). An earlier Claude wrote it via atomic temp+rename, which REPLACES
# the symlink with a throwaway file in the container's ephemeral /root and
# silently stops persisting config. If that regresses, the fix is to stop
# symlinking claude.json here and bind-mount it in the launcher instead
# (~/.config/agent-box/claude.json:/root/.claude.json). Seeded JSON files use
# "{}" (a valid empty object), because opencode rejects a present-but-invalid
# JSON file -- never seed a config file empty.
set -eo pipefail

root=${1:?usage: config-layout.sh <root> [--symlinks]}
mode=${2:-}

# Directories -- "<subdir under root>|<the ~ path the tool looks for>".
dirs=(
  "claude|${HOME}/.claude"
  "codex|${HOME}/.codex"
  "opencode|${HOME}/.config/opencode"
  "opencode/share|${HOME}/.local/share/opencode"
  "opencode/state|${HOME}/.local/state/opencode"
  "opencode/cache|${HOME}/.cache/opencode"
  "cache/pip|${HOME}/.cache/pip"
  "cache/npm|${HOME}/.npm"
)

# Files -- "<path under root>|<the ~ path the tool looks for>|<seed if absent>".
files=(
  "claude.json|${HOME}/.claude.json|{}"
)

# In --symlinks mode, point $2 (a ~/ path) at $1 (a path under <root>), unless
# something is already there. No-op on the host side.
link_in() {
  [[ "${mode}" == "--symlinks" ]] || return 0
  mkdir -p "$(dirname "$2")"
  [[ -e "$2" || -L "$2" ]] || ln -s "$1" "$2"
}

for entry in "${dirs[@]}"; do
  sub=${entry%%|*}
  link=${entry#*|}
  mkdir -p "${root}/${sub}"
  link_in "${root}/${sub}" "${link}"
done

for entry in "${files[@]}"; do
  sub=${entry%%|*}
  rest=${entry#*|}
  link=${rest%%|*}
  seed=${rest#*|}
  mkdir -p "$(dirname "${root}/${sub}")"
  [[ -e "${root}/${sub}" || -L "${root}/${sub}" ]] || printf '%s' "${seed}" > "${root}/${sub}"
  link_in "${root}/${sub}" "${link}"
done
