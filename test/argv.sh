#!/usr/bin/env bash
# Argv tests for bin/agtbox.sh (Agent Box: one image, consolidated config).
#
# Stubs the container engine on PATH so each launch prints the argv it WOULD exec
# instead of running a real container, then asserts the constructed command. Runs
# the launcher with a throwaway HOME so its config-dir setup stays hermetic. Pure
# bash, no external test framework. Run: ./test/argv.sh
set -uo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
AGTBOX="${HERE}/../bin/agtbox.sh"

STUB=$(mktemp -d)
TMP=$(realpath "$(mktemp -d)")
THOME="${TMP}/home"
mkdir -p "${TMP}/ro" "${TMP}/rw" "${THOME}"
trap 'rm -rf "${STUB}" "${TMP}"' EXIT

cat >"${STUB}/podman" <<'SH'
#!/usr/bin/env bash
case "${1:-}" in run) shift; printf 'ARG:%s\n' "$@" ;; esac
exit 0
SH
cat >"${STUB}/ch-run" <<'SH'
#!/usr/bin/env bash
printf 'ARG:%s\n' "$@"
exit 0
SH
printf '#!/usr/bin/env bash\nexit 0\n' >"${STUB}/ch-image"
printf '#!/usr/bin/env bash\nexit 0\n' >"${STUB}/ch-convert"
chmod +x "${STUB}"/*
export PATH="${STUB}:${PATH}"

rc=0
OUT=""
run()    { OUT=$(HOME="${THOME}" "${AGTBOX}" "$@" 2>&1) || true; }
has()    { grep -Fq  -- "$1" <<<"${OUT}" || { echo "  FAIL: expected [$1]"; rc=1; }; }
hasx()   { grep -Fxq -- "$1" <<<"${OUT}" || { echo "  FAIL: expected exact line [$1]"; rc=1; }; }
hasnot() { grep -Fq  -- "$1" <<<"${OUT}" && { echo "  FAIL: unexpected [$1]"; rc=1; }; }
exits()  { local want=$1; shift; HOME="${THOME}" "${AGTBOX}" "$@" >/dev/null 2>&1; local got=$?
           [[ "${got}" == "${want}" ]] || { echo "  FAIL: exit ${got} != ${want} for: $*"; rc=1; }; }

# Things that MUST hold for every tool (one image, union env, one config mount).
common() {
  has "ARG:agent-box"                                                  # one image, every tool
  has "ARG:${THOME}/.config/agent-box/:/root/.config/agent-box/"       # the ONE bind mount
  has "ARG:CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"                    # env UNION, present regardless of tool
  has "ARG:OPENCODE_ENABLE_EXA=1"
  has "ARG:OPENCODE_EXPERIMENTAL_LSP_TOOL=true"
  hasx "ARG:--"                                                        # defensive standalone -- before image
  hasnot "ARG:claude-code"                                             # no per-tool image tag any more
  # Everything else is consolidated INTO the single mount via in-image symlinks,
  # so none of these appear as separate mounts any more:
  hasnot ":/root/.claude.json"                                         # claude.json: symlink, not a file bind
  hasnot ":/root/.cache/pip/"                                          # pip cache: symlink, not a mount
  hasnot ":/root/.npm/"                                                # npm cache: symlink, not a mount
  hasnot "ARG:${THOME}/.claude/:/root/.claude/"                        # old per-tool config mounts gone
  hasnot "ARG:${THOME}/.config/opencode/:/root/.config/opencode/"
  hasnot "ARG:${THOME}/.codex/:/root/.codex/"
}

echo "[claude] one image, union env, single config mount, binary, verbatim passthrough, -r"
run -t podman -r "${TMP}/ro" claude --resume X
common
has "ARG:/usr/local/bin/claude"
has "ARG:--resume"; has "ARG:X"
has "ARG:${TMP}/ro:${TMP}/ro:ro"

echo "[opencode] same image/env/mounts; opencode binary; verbatim --session"
run -t podman opencode --session Y
common
has "ARG:/usr/local/bin/opencode"
has "ARG:--session"; has "ARG:Y"

echo "[codex] same image/env/mounts; codex binary; verbatim resume subcommand"
run -t podman codex resume Z
common
has "ARG:/usr/local/bin/codex"
has "ARG:resume"; has "ARG:Z"

echo "[no flag-mapping leaks] bare claude injects no --continue/--resume/--fork"
run -t podman claude
hasnot "ARG:--continue"
hasnot "ARG:--resume"
hasnot "ARG:--fork-session"

echo "[mix] -v rw + -r ro (different paths) both mounted"
run -t podman -v "${TMP}/rw" -r "${TMP}/ro" claude foo
has "ARG:${TMP}/rw:${TMP}/rw"
has "ARG:${TMP}/ro:${TMP}/ro:ro"
has "ARG:foo"

echo "[dedup] same path via -v and -r: rw wins, ro dropped, warning"
run -t podman -v "${TMP}/ro" -r "${TMP}/ro" claude bar
has "given as both -v (rw) and -r (ro)"
hasnot "ARG:${TMP}/ro:${TMP}/ro:ro"

echo "[charliecloud] -b binds + --set-env union + -- before the command"
run -t charliecloud claude --resume X
has "ARG:-b"
has "ARG:--set-env=CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"
has "ARG:--set-env=OPENCODE_ENABLE_EXA=1"
hasx "ARG:--"

echo "[errors] no tool / unknown tool / -h all exit 1"
exits 1 -t podman
exits 1 -t podman frobnicate
exits 1 -h

echo
if ((rc)); then echo "RESULT: FAILED"; else echo "RESULT: ALL PASSED"; fi
exit "${rc}"
