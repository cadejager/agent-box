#!/usr/bin/env bash
# Argv tests for bin/agtbox.sh.
#
# Stubs the container engine (podman / ch-run + the ch-image/ch-convert build
# helpers) on PATH so each launch prints the argv it WOULD exec instead of
# running a real container, then asserts the constructed command. Pure bash, no
# external test framework. Run: ./test/argv.sh
set -uo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
AGTBOX="${HERE}/../bin/agtbox.sh"

# --- engine stubs on PATH -------------------------------------------------
STUB=$(mktemp -d)
TMP=$(realpath "$(mktemp -d)")
mkdir -p "${TMP}/ro" "${TMP}/rw"
trap 'rm -rf "${STUB}" "${TMP}"' EXIT

cat >"${STUB}/podman" <<'SH'
#!/usr/bin/env bash
# `run` -> echo argv; anything else (image exists / build) -> silent success.
case "${1:-}" in run) shift; printf 'ARG:%s\n' "$@" ;; esac
exit 0
SH
cat >"${STUB}/ch-run" <<'SH'
#!/usr/bin/env bash
printf 'ARG:%s\n' "$@"
exit 0
SH
# Build helpers just need to succeed so launch reaches the run step.
printf '#!/usr/bin/env bash\nexit 0\n' >"${STUB}/ch-image"
printf '#!/usr/bin/env bash\nexit 0\n' >"${STUB}/ch-convert"
chmod +x "${STUB}"/*
export PATH="${STUB}:${PATH}"

# --- assertion helpers ----------------------------------------------------
rc=0
OUT=""
run()    { OUT=$("${AGTBOX}" "$@" 2>&1) || true; }
has()    { grep -Fq  -- "$1" <<<"${OUT}" || { echo "  FAIL: expected [$1]"; rc=1; }; }
hasx()   { grep -Fxq -- "$1" <<<"${OUT}" || { echo "  FAIL: expected exact line [$1]"; rc=1; }; }
hasnot() { grep -Fq  -- "$1" <<<"${OUT}" && { echo "  FAIL: unexpected [$1]"; rc=1; }; }
exits()  { local want=$1; shift; "${AGTBOX}" "$@" >/dev/null 2>&1; local got=$?
           [[ "${got}" == "${want}" ]] || { echo "  FAIL: exit ${got} != ${want} for: $*"; rc=1; }; }

echo "[claude] -r read-only, image/binary/env, verbatim passthrough"
run -t podman -r "${TMP}/ro" claude --resume X
has "ARG:${TMP}/ro:${TMP}/ro:ro"
has "ARG:claude-code"
has "ARG:/usr/local/bin/claude"
has "ARG:--resume"
has "ARG:X"
has "ARG:CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"
hasx "ARG:--"            # exact-line: the defensive standalone -- before the image
hasnot "ARG:opencode"

echo "[claude] plain launch: no session-flag remapping leaks in; shared caches; defensive --"
run -t podman claude
hasnot "ARG:--continue"
hasnot "ARG:--resume"
has "podman-ai-agents/pip/:/root/.cache/pip/"
has "podman-ai-agents/npm/:/root/.npm/"
hasx "ARG:--"            # the only -- here is the defensive one (no passthrough args)

echo "[opencode] image + four config mounts + verbatim --session"
run -t podman opencode --session Y
has "ARG:opencode"
has "ARG:/usr/local/bin/opencode"
has "ARG:OPENCODE_ENABLE_EXA=1"
has "ARG:${HOME}/.local/state/opencode/:/root/.local/state/opencode/"
has "ARG:--session"
has "ARG:Y"

echo "[codex] image + config mount + verbatim resume subcommand"
run -t podman codex resume Z
has "ARG:codex"
has "ARG:/usr/local/bin/codex"
has "ARG:${HOME}/.codex/:/root/.codex/"
has "ARG:resume"
has "ARG:Z"

echo "[mix] -v rw + -r ro (different paths) both mounted at the same path"
run -t podman -v "${TMP}/rw" -r "${TMP}/ro" claude foo
has "ARG:${TMP}/rw:${TMP}/rw"
has "ARG:${TMP}/ro:${TMP}/ro:ro"
has "ARG:foo"

echo "[dedup] same path via -v and -r: rw wins, ro mount dropped, warning"
run -t podman -v "${TMP}/ro" -r "${TMP}/ro" claude foo
has "given as both -v (rw) and -r (ro)"
hasnot "ARG:${TMP}/ro:${TMP}/ro:ro"
has "ARG:${TMP}/ro:${TMP}/ro"

echo "[charliecloud] -b binds + --set-env + -- before the command"
run -t charliecloud claude --resume X
has "ARG:-b"
has "ARG:--set-env=CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"
hasx "ARG:--"

echo "[errors] no tool / unknown tool / -h all exit 1"
exits 1 -t podman
exits 1 -t podman frobnicate
exits 1 -h

echo
if ((rc)); then echo "RESULT: FAILED"; else echo "RESULT: ALL PASSED"; fi
exit "${rc}"
