#!/usr/bin/env bash
# Single source of truth for Agent Box's consolidated config layout.
#
# Every agent's config lives under one root: on the host that's
# ~/.config/agent-box, which the launcher bind-mounts to /root/.config/agent-box
# inside the container. This script creates the per-tool target subdirs under
# <root>, idempotently -- it never clobbers anything that already exists.
#
# With --symlinks (used INSIDE the image at build time, before the CLIs are
# installed) it also points each tool's expected ~/ path at its subdir, so the
# tools find their config there and the launcher needs no per-tool knowledge.
#
#   config-layout.sh <root>             # create target subdirs only (host side)
#   config-layout.sh <root> --symlinks  # + create the in-container symlinks
#
# (~/.claude.json is a single FILE; the launcher handles it as a file bind.)
set -eo pipefail

root=${1:?usage: config-layout.sh <root> [--symlinks]}
mode=${2:-}

# "<subdir under root>|<the ~ path the tool looks for>"
layout=(
  "claude|${HOME}/.claude"
  "codex|${HOME}/.codex"
  "opencode/config|${HOME}/.config/opencode"
  "opencode/share|${HOME}/.local/share/opencode"
  "opencode/state|${HOME}/.local/state/opencode"
  "opencode/cache|${HOME}/.cache/opencode"
)

for entry in "${layout[@]}"; do
  sub=${entry%%|*}
  link=${entry#*|}
  mkdir -p "${root}/${sub}"
  if [[ "${mode}" == "--symlinks" ]]; then
    mkdir -p "$(dirname "${link}")"
    # Don't clobber anything already at the link path.
    [[ -e "${link}" || -L "${link}" ]] || ln -s "${root}/${sub}" "${link}"
  fi
done
