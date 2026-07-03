import os
import sys
from agtbox import core


def install_script():
    return r'''set -euo pipefail
mkdir -p "${AGT_TOOLS}/node" "${AGT_TOOLS}/bin"

if [ "${AGT_DO_CORE}" = "1" ]; then
  echo 'Agent Box: installing node (latest LTS)...' >&2
  nv=$(curl -fsSL https://nodejs.org/dist/index.json \
    | python3 -c 'import json,sys; print(next(r["version"] for r in json.load(sys.stdin) if r["lts"]))')
  curl -fsSL "https://nodejs.org/dist/${nv}/node-${nv}-linux-${AGT_NARCH}.tar.xz" \
    | tar -xJ --strip-components=1 -C "${AGT_TOOLS}/node"

  skipped=
  echo 'Agent Box: installing uv...' >&2
  { curl -LsSf https://astral.sh/uv/install.sh \
      | env UV_INSTALL_DIR="${AGT_TOOLS}/bin" UV_NO_MODIFY_PATH=1 sh; } \
    || { echo 'Agent Box: WARNING -- uv install failed (astral.sh blocked?); skipping.' >&2; skipped="${skipped} uv"; }
  echo 'Agent Box: installing gh (GitHub CLI)...' >&2
  { gv=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest \
         | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])') \
    && curl -fsSL "https://github.com/cli/cli/releases/download/${gv}/gh_${gv#v}_linux_${AGT_GOARCH}.tar.gz" \
         | tar -xz -C /tmp \
    && install -m755 "/tmp/gh_${gv#v}_linux_${AGT_GOARCH}/bin/gh" "${AGT_TOOLS}/bin/gh"; } \
    || { echo 'Agent Box: WARNING -- gh install failed (github.com blocked?); skipping.' >&2; skipped="${skipped} gh"; }
  echo 'Agent Box: installing glab (GitLab CLI)...' >&2
  { lv=$(curl -fsSL https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases \
         | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["tag_name"])') \
    && curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/${lv}/downloads/glab_${lv#v}_linux_${AGT_GOARCH}.tar.gz" \
         | tar -xz -C /tmp \
    && install -m755 /tmp/bin/glab "${AGT_TOOLS}/bin/glab"; } \
    || { echo 'Agent Box: WARNING -- glab install failed (gitlab.com blocked?); skipping.' >&2; skipped="${skipped} glab"; }
  if [ -n "${skipped}" ]; then
    echo "Agent Box: core toolchain ready; skipped:${skipped} (retry later with -u)." >&2
  fi
fi

if [ -n "${AGT_NPM_PKGS}" ]; then
  echo 'Agent Box: installing the agent CLI...' >&2
  "${AGT_TOOLS}/node/bin/npm" install -g --prefix "${AGT_TOOLS}" --no-fund --no-audit ${AGT_NPM_PKGS}
fi

date > "${AGT_TOOLS}/.stamp"
'''


def agt_env(agent, do_core, machine):
    narch, goarch = core.arch_pair(machine)
    return [
        ("AGT_TOOLS", core.AGENT_TOOLS),
        ("AGT_NARCH", narch),
        ("AGT_GOARCH", goarch),
        ("AGT_NPM_PKGS", " ".join(agent.packages)),
        ("AGT_DO_CORE", "1" if do_core else "0"),
    ]


def install_env(agent, do_core, machine):
    """Resolve the install env once, here (not in the sandbox). The same for every
    sandbox: AGENT_ENV (the npm/pip/uv routing into the mounted toolchain) + the
    generic proxy/locale forwards WHEN set on the host -- so the install's downloads
    work behind a proxy on either sandbox -- plus the AGT_* inputs. `resolve_env([],
    [])` is exactly that: no agent-specific API/config vars, which the install never
    needs (it downloads node + npm packages, it doesn't run the agent). An unset
    forward is omitted, so this is a no-op on a machine with no proxy exported."""
    return list(core.resolve_env([], [])) + agt_env(agent, do_core, machine)


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
    pairs = install_env(agent, do_core, sandbox.install_machine())
    sandbox.install(install_script(), pairs)
