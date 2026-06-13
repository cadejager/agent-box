#!/usr/bin/env bash
# Argv tests for bin/agtbox.sh (Agent Box: bwrap + podman engines).
#
# Stubs `bwrap` AND `podman` on PATH so each launch prints the argv it WOULD exec
# instead of really sandboxing, then asserts the constructed command for both
# engines. Runs the launcher with a throwaway HOME -- with the toolchain bins
# pre-created so the one-time (networked) install is skipped, and the podman stub
# reporting the image already exists so no build runs -- so it stays hermetic and
# offline. Pure bash. Run: ./test/argv.sh
set -uo pipefail
unset AGTBOX_REINSTALL

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
AGTBOX="${HERE}/../bin/agtbox.sh"

STUB=$(mktemp -d)
TMP=$(realpath "$(mktemp -d)")
THOME="${TMP}/home"
mkdir -p "${TMP}/ro" "${TMP}/rw" "${TMP}/app" "${THOME}"
trap 'rm -rf "${STUB}" "${TMP}"' EXIT

# Stub bwrap: echo every arg so we can assert the constructed argv.
cat >"${STUB}/bwrap" <<'SH'
#!/usr/bin/env bash
printf 'ARG:%s\n' "$@"
exit 0
SH
chmod +x "${STUB}/bwrap"

# Stub podman: `image ...` succeeds (so build_image sees the image as present and
# skips the build), `build` is a no-op, `run ... uname -m` answers the arch probe,
# and any other `run` echoes its argv so we can assert it.
cat >"${STUB}/podman" <<'SH'
#!/usr/bin/env bash
case "$1" in
  image) exit 0 ;;
  build) exit 0 ;;
  run)
    case " $* " in
      *" uname -m "*) echo aarch64; exit 0 ;;
      *) printf 'ARG:%s\n' "$@"; exit 0 ;;
    esac ;;
esac
exit 0
SH
chmod +x "${STUB}/podman"
export PATH="${STUB}:${PATH}"

# Pre-create the toolchain so ensure_tools() skips the networked install.
TOOLS="${THOME}/.local/share/agent-box"
mkdir -p "${TOOLS}/bin" "${TOOLS}/node/bin"
for b in claude opencode codex uv; do printf '#!/bin/sh\n' >"${TOOLS}/bin/${b}"; done
printf '#!/bin/sh\n' >"${TOOLS}/node/bin/node"
chmod +x "${TOOLS}/bin/"* "${TOOLS}/node/bin/node"
: >"${TOOLS}/.stamp"   # marks a complete install so ensure_tools skips it

rc=0
OUT=""
run()    { OUT=$(HOME="${THOME}" "${AGTBOX}" "$@" 2>&1) || true; }
has()    { grep -Fq  -- "$1" <<<"${OUT}" || { echo "  FAIL: expected [$1]"; rc=1; }; }
hasx()   { grep -Fxq -- "$1" <<<"${OUT}" || { echo "  FAIL: expected exact line [$1]"; rc=1; }; }
hasnot() { grep -Fq  -- "$1" <<<"${OUT}" && { echo "  FAIL: unexpected [$1]"; rc=1; }; }
exits()  { local want=$1; shift; HOME="${THOME}" "${AGTBOX}" "$@" >/dev/null 2>&1; local got=$?
           [[ "${got}" == "${want}" ]] || { echo "  FAIL: exit ${got} != ${want} for: $*"; rc=1; }; }

# Must hold for every tool: locked-down system binds, tmpfs home, persistent
# toolchain + cache + config binds, env union, shared net.
common() {
  has "ARG:--ro-bind"                                              # system dirs, read-only
  has "ARG:/usr"; has "ARG:/etc"
  has "ARG:--tmpfs"; hasx "ARG:${THOME}"                           # empty ephemeral HOME
  has "ARG:--bind"
  has "ARG:${THOME}/.local/share/agent-box"                       # persistent toolchain (rw)
  has "ARG:${THOME}/.cache/agent-box"                             # caches (rw)
  has "ARG:${THOME}/.config/agent-box/claude"; has "ARG:${THOME}/.claude"            # config dir bind
  has "ARG:${THOME}/.config/agent-box/claude.json"; has "ARG:${THOME}/.claude.json"  # claude.json FILE bind
  has "ARG:${THOME}/.config/agent-box/opencode"; has "ARG:${THOME}/.config/opencode"            # config
  has "ARG:${THOME}/.local/share/agent-box/opencode"; has "ARG:${THOME}/.local/share/opencode"  # data
  has "ARG:${THOME}/.local/state/agent-box/opencode"; has "ARG:${THOME}/.local/state/opencode"  # state
  has "ARG:${THOME}/.cache/agent-box/opencode"; has "ARG:${THOME}/.cache/opencode"              # cache
  has "ARG:${THOME}/.config/gh"; has "ARG:${THOME}/.config/glab-cli"   # gh/glab auth dirs
  has "ARG:${THOME}/.config/git"; hasnot "ARG:GIT_CONFIG_GLOBAL"       # git: dir bind (no file-bind EBUSY), no redirect
  has "ARG:${THOME}/.config/agent-box/ssh"; has "ARG:${THOME}/.ssh"    # ssh keys/known_hosts persist
  has "ARG:--clearenv"                                            # wipe host env; --setenv is the allowlist
  has "ARG:--setenv"
  has "ARG:CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"                 # env UNION, every tool
  has "ARG:OPENCODE_ENABLE_EXA"; has "ARG:OPENCODE_EXPERIMENTAL_LSP_TOOL"
  has "ARG:npm_config_prefix"; has "ARG:npm_config_cache"         # installs + caches persist
  # Isolation primitives -- a refactor must not silently drop the sandbox's teeth:
  has "ARG:--dev"; has "ARG:--proc"; has "ARG:--ro-bind-try"      # min /dev, /proc, optional binds
  has "ARG:/sbin"; has "ARG:/opt"; has "ARG:/cpe"                 # system dirs ro-bound (incl. /opt, /cpe HPC path)
  has "ARG:--unshare-pid"; has "ARG:--unshare-ipc"; has "ARG:--unshare-uts"
  has "ARG:--die-with-parent"
  hasx "ARG:--"                                                   # bwrap arg terminator
  hasnot "ARG:--unshare-net"                                      # network shared (agents need it)
  hasnot "ARG:--symlink"                                          # direct binds, never bwrap --symlink
}

# Must hold for every tool under the podman engine: same-path -v binds + HOME
# override (so the bind tables are reused verbatim), the env union as -e, the image,
# shared net. The image is the rootfs, so there are NO system-dir binds here.
common_podman() {
  has "ARG:run"; has "ARG:--rm"                                   # podman run
  has "ARG:--userns=keep-id"                                      # files owned by you (rootless Linux)
  has "ARG:--security-opt"; has "ARG:label=disable"               # don't let SELinux block the binds
  has "ARG:-e"; has "ARG:HOME=${THOME}"                           # HOME override
  has "ARG:PATH=${TOOLS}/bin:${TOOLS}/node/bin:/usr/bin:/bin"     # toolchain on PATH
  has "ARG:npm_config_prefix=${TOOLS}"                            # installs persist to the toolchain
  has "ARG:CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"              # env union as -e, every tool
  has "ARG:-v"
  has "ARG:${TOOLS}:${TOOLS}"                                     # toolchain, same path (rw)
  has "ARG:${THOME}/.cache/agent-box:${THOME}/.cache/agent-box"   # cache, same path
  has "ARG:${THOME}/.config/agent-box/claude:${THOME}/.claude"            # config dir bind src:dst
  has "ARG:${THOME}/.config/agent-box/claude.json:${THOME}/.claude.json"  # claude.json FILE bind
  has "ARG:${THOME}/.config/agent-box/ssh:${THOME}/.ssh"          # ssh keys/known_hosts
  has "ARG:agent-box"                                            # the image name
  hasx "ARG:--"                                                  # podman arg terminator
  hasnot "ARG:--network"                                         # network shared (agents need it)
  hasnot "ARG:--clearenv"                                        # that's a bwrap flag, not podman
}

echo "[claude] sandbox binds, union env, agent bin, verbatim passthrough, -r"
run -a "${TMP}/app" -r "${TMP}/ro" claude --resume X
common
has "ARG:${TOOLS}/bin/claude"
has "ARG:--resume"; has "ARG:X"
has "ARG:${TMP}/app"                                              # project bound at same path
has "ARG:${TMP}/ro"                                               # -r dir bound

echo "[opencode] same sandbox; opencode bin; verbatim --session"
run -a "${TMP}/app" opencode --session Y
common
has "ARG:${TOOLS}/bin/opencode"
has "ARG:--session"; has "ARG:Y"

echo "[codex] same sandbox; codex bin; verbatim resume subcommand"
run -a "${TMP}/app" codex resume Z
common
has "ARG:${TOOLS}/bin/codex"
has "ARG:resume"; has "ARG:Z"

echo "[no flag-mapping leaks] bare claude injects nothing"
run -a "${TMP}/app" claude
hasnot "ARG:--continue"; hasnot "ARG:--resume"; hasnot "ARG:--fork-session"

echo "[mix] -v rw + -r ro (different paths) both bound"
run -a "${TMP}/app" -v "${TMP}/rw" -r "${TMP}/ro" claude foo
has "ARG:${TMP}/rw"; has "ARG:${TMP}/ro"; has "ARG:foo"

echo "[dedup] same path via -v and -r: rw wins, warning"
run -a "${TMP}/app" -v "${TMP}/ro" -r "${TMP}/ro" claude bar
has "given as both -v (rw) and -r (ro)"
has "ARG:${TMP}/ro"

echo "[errors] no tool / unknown tool / -h all exit 1"
exits 1
exits 1 frobnicate
exits 1 -h

# ---- podman engine (forced with -t podman) ----

echo "[claude/podman] same-path -v binds, -e env, image, agent bin, -r ro"
run -t podman -a "${TMP}/app" -r "${TMP}/ro" claude --resume X
common_podman
has "ARG:${TOOLS}/bin/claude"
has "ARG:--resume"; has "ARG:X"
has "ARG:${TMP}/app:${TMP}/app"                                   # project bound same path
has "ARG:-w"; has "ARG:${TMP}/app"                               # workdir
has "ARG:${TMP}/ro:${TMP}/ro:ro"                                 # -r read-only

echo "[opencode/podman] same sandbox; opencode bin; verbatim --session"
run -t podman -a "${TMP}/app" opencode --session Y
common_podman
has "ARG:${TOOLS}/bin/opencode"
has "ARG:--session"; has "ARG:Y"

echo "[codex/podman] same sandbox; codex bin; verbatim resume subcommand"
run -t podman -a "${TMP}/app" codex resume Z
common_podman
has "ARG:${TOOLS}/bin/codex"
has "ARG:resume"; has "ARG:Z"

echo "[podman mix] -v rw + -r ro (different paths) both bound"
run -t podman -a "${TMP}/app" -v "${TMP}/rw" -r "${TMP}/ro" claude foo
has "ARG:${TMP}/rw:${TMP}/rw"; has "ARG:${TMP}/ro:${TMP}/ro:ro"; has "ARG:foo"

echo "[bad engine] -t bogus exits 1"
exits 1 -t bogus claude

echo
if ((rc)); then echo "RESULT: FAILED"; else echo "RESULT: ALL PASSED"; fi
exit "${rc}"
