import os
import sys
import textwrap

from agtbox import core, releases

_SCRIPT = r'''set -euo pipefail
mkdir -p "${AGT_TOOLS}/node" "${AGT_TOOLS}/bin"
WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

@CONSTS@

# One resolver for every tool (modes documented on releases.Release). In redirect
# mode the tag must look like a version, so a landing page that isn't a release
# (e.g. a repo with no releases) fails here rather than as a confusing 404 later.
# JSON mode: rows may be a dict or a list; `where` optionally names a key the
# chosen row must have truthy (node's "lts"). python3 stderr is dropped so a
# blocked host yields our warning, not a traceback.
resolve() {  # api  where_key  field
  [ -z "$1" ] && return 0
  if [ -z "$3" ]; then
    local url tag
    url=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$1") || return 1
    tag=${url##*/}
    case $tag in v[0-9]*|[0-9]*) printf '%s\n' "$tag" ;; *) return 1 ;; esac
    return 0
  fi
  curl -fsSL "$1" | WHERE="$2" FIELD="$3" python3 -c '
import json, os, sys
rows = json.load(sys.stdin)
rows = rows if isinstance(rows, list) else [rows]
w = os.environ["WHERE"]
print(next(r for r in rows if not w or r.get(w))[os.environ["FIELD"]])' 2>/dev/null
}

_subst() {  # template  version
  local s=$1 v=$2
  s=${s//@VER@/$v}
  s=${s//@VERNOV@/${v#v}}
  printf '%s' "$s"
}

_download() {  # url  dest  [extra tar args...]
  local url=$1 dest=$2; shift 2
  local taropt
  case $url in
    *.tar.xz) taropt=-xJ ;;
    *)        taropt=-xz ;;
  esac
  mkdir -p "$dest"
  curl -fsSL "$url" | tar "$taropt" "$@" -C "$dest"
}

_try_tree() {  # api where field url_tmpl dest
  local ver url
  ver=$(resolve "$1" "$2" "$3") || return 1
  url=$(_subst "$4" "$ver") || return 1
  _download "$url" "$5" --strip-components=1
}

_try_bins() {  # api where field url_tmpl dir  src:dst...
  local api=$1 where=$2 field=$3 tmpl=$4 dir=$5; shift 5
  local ver url pair src dst
  ver=$(resolve "$api" "$where" "$field") || return 1
  url=$(_subst "$tmpl" "$ver") || return 1
  _download "$url" "$dir" || return 1
  for pair in "$@"; do
    src=$(_subst "${pair%%:*}" "$ver")
    dst=${pair##*:}
    install -m755 "${dir}/${src}" "${AGT_TOOLS}/bin/${dst}" || return 1
  done
}

# The blocked-host hint is derived from the first URL fetched (the API, else the
# download URL) so it cannot drift from the URL it describes.
_warn_skip() {  # name  url
  local host=${2#https://}; host=${host%%/*}
  echo "Agent Box: WARNING -- $1 install failed (${host} blocked?); skipping." >&2
  skipped="${skipped} $1"
}

_fail() {  # name  url
  local host=${2#https://}; host=${host%%/*}
  echo "Agent Box: ERROR -- $1 install failed (${host} unreachable?)." >&2
}

install_tree() {  # name required api where field url dest
  local name=$1 req=$2; shift 2
  echo "Agent Box: installing ${name}..." >&2
  _try_tree "$@" && return 0
  if [ "$req" = required ]; then _fail "$name" "${1:-$4}"; return 1; fi
  _warn_skip "$name" "${1:-$4}"
  return 0
}

install_bins() {  # name required api where field url src:dst...
  local name=$1 req=$2; shift 2
  local api=$1 where=$2 field=$3 tmpl=$4; shift 4
  echo "Agent Box: installing ${name}..." >&2
  _try_bins "$api" "$where" "$field" "$tmpl" "${WORK}/${name}" "$@" && return 0
  if [ "$req" = required ]; then _fail "$name" "${api:-$tmpl}"; return 1; fi
  _warn_skip "$name" "${api:-$tmpl}"
  return 0
}

if [ "${AGT_DO_CORE}" = "1" ]; then
  skipped=
@CALLS@
  if [ -n "${skipped}" ]; then
    echo "Agent Box: core toolchain ready; skipped:${skipped} (retry later with -u)." >&2
  fi
  # .stamp marks the core toolchain as installed. Written here -- after the core
  # block, before the npm step -- so a failed agent `npm install` can't force a full
  # node re-download next run (the npm step is separately gated by the agent bin's
  # presence). With set -e, a failed core install aborts before this line.
  date > "${AGT_TOOLS}/.stamp"
fi

if [ -n "${AGT_NPM_PKGS}" ]; then
  if [ -n "${AGT_NPM_REMOVE:-}" ]; then
    "${AGT_TOOLS}/node/bin/npm" uninstall -g --prefix "${AGT_TOOLS}" --no-fund --no-audit ${AGT_NPM_REMOVE}
  fi
  echo 'Agent Box: installing the agent CLI...' >&2
  "${AGT_TOOLS}/node/bin/npm" install -g --prefix "${AGT_TOOLS}" --no-fund --no-audit ${AGT_NPM_PKGS}
fi
'''


def install_script(machine):
    """The install script for `machine`, assembled from the releases table. Arch is
    substituted here, so no arch env var crosses into the sandbox; only @VER@ and
    @VERNOV@ are left for bash, since the version is resolved inside the sandbox."""
    consts, calls = releases.render(machine)
    return (_SCRIPT
            .replace("@CONSTS@", consts)
            .replace("@CALLS@", textwrap.indent(calls, "  ")))


def agt_env(agent, do_core):
    return [
        ("AGT_TOOLS", core.AGENT_TOOLS),
        ("AGT_NPM_PKGS", " ".join(agent.packages)),
        ("AGT_NPM_REMOVE", " ".join(agent.obsolete_packages)),
        ("AGT_DO_CORE", "1" if do_core else "0"),
    ]


def install_env(agent, do_core):
    """Resolve the install env once, here (not in the sandbox). The same for every
    sandbox: AGENT_ENV (the npm/pip/uv routing into the mounted toolchain) + the
    generic proxy/locale forwards WHEN set on the host -- so the install's downloads
    work behind a proxy on either sandbox -- plus the AGT_* inputs. `resolve_env([],
    [])` is exactly that: no agent-specific API/config vars, which the install never
    needs (it downloads node + npm packages, it doesn't run the agent). An unset
    forward is omitted, so this is a no-op on a machine with no proxy exported."""
    return list(core.resolve_env([], [])) + agt_env(agent, do_core)


def ensure_tools(agent, sandbox, force):
    """Install on first use (or -u). Core toolchain gated by .stamp; the launched
    agent's packages gated by its bin presence (lexists -- presence only). The
    sandbox supplies the run arch and runs the install; this fn owns the env."""
    stamp = os.path.exists(f"{core.AGENT_TOOLS}/.stamp")
    bin_present = os.path.lexists(agent.bin)
    if not (force or not stamp or not bin_present):
        return
    print(f"Agent Box: setting up the toolchain in {core.AGENT_TOOLS} (one-time)...", file=sys.stderr)
    do_core = force or not stamp
    machine = sandbox.install_machine()
    pairs = install_env(agent, do_core)
    sandbox.install(install_script(machine), pairs)
