# Install Release Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the four hand-duplicated download blocks in `agtbox/install.py`'s embedded bash with a declarative Python table, landing `main`'s uv-from-GitHub change as a reimplementation rather than a port.

**Architecture:** A new pure-data module `agtbox/releases.py` holds one `Release` entry per downloadable tool (node, uv, gh, glab). `install.py` renders those entries into a bash constants block plus one `install_tree`/`install_bins` call line each, with CPU-arch spellings substituted in Python so no arch env var crosses into the sandbox. All network access stays inside the sandbox; only the version placeholder (`@VER@`/`@VERNOV@`) is resolved in bash.

**Tech Stack:** Python 3 stdlib only (`dataclasses`, `textwrap`), bash 4+, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-21-install-release-table-design.md`

## Global Constraints

- **Stdlib only.** No third-party Python packages anywhere in `agtbox/`.
- **The launcher makes no network calls.** All `curl` happens inside the sandbox. Do not add `urllib`/`requests` to `agtbox/`.
- **node is required; uv/gh/glab are best-effort.** A blocked auxiliary host warns, skips, and still writes `.stamp`. A node failure aborts before `.stamp`.
- **`.stamp` is written after the core block and before the npm step.**
- **`Sandbox.install(script, pairs)` is unchanged.** Do not modify `agtbox/sandboxes/`.
- **Two placeholder dialects, never mixed:** `{narch}`/`{goarch}`/`{uvarch}` are Python `str.format` and are substituted at render time; `@VER@`/`@VERNOV@` are left for bash and substituted in the sandbox (`@VERNOV@` = version with a leading `v` stripped).
- **Out of scope:** lint cleanup, the Python 3.11 floor + `actions/setup-python`, `lint.yml` path updates, toolchain locking.
- Run the full suite with `python3 -m unittest discover -s test`.

---

### Task 1: `core.arch_names` returns a dict

**Files:**
- Modify: `agtbox/core.py:51-57`
- Modify: `agtbox/install.py:53-61` (keep callers working; arch vars are removed in Task 3)
- Test: `test/test_core.py:56-61`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `core.arch_names(machine: str) -> dict[str, str]` with keys `narch`, `goarch`, `uvarch`. Exits via `sys.exit(1)` on an unsupported machine. Tasks 2 and 3 depend on these exact key names.

- [ ] **Step 1: Write the failing test**

Replace `test_arch_pair` in `test/test_core.py` (lines 56-61) with:

```python
    def test_arch_names(self):
        from agtbox import core
        self.assertEqual(core.arch_names("aarch64"),
                         {"narch": "arm64", "goarch": "arm64", "uvarch": "aarch64"})
        self.assertEqual(core.arch_names("x86_64"),
                         {"narch": "x64", "goarch": "amd64", "uvarch": "x86_64"})

    def test_arch_names_unsupported_exits(self):
        from agtbox import core
        with self.assertRaises(SystemExit):
            core.arch_names("riscv64")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test.test_core -v`
Expected: FAIL — `AttributeError: module 'agtbox.core' has no attribute 'arch_names'`

- [ ] **Step 3: Write minimal implementation**

Replace `arch_pair` in `agtbox/core.py` (lines 51-57) with:

```python
def arch_names(machine):
    """The per-tool spellings of the CPU arch, keyed by the placeholder that uses
    them. A dict rather than a tuple so a future tool with a fourth spelling adds a
    key instead of renumbering every unpack site."""
    if machine == "aarch64":
        return {"narch": "arm64", "goarch": "arm64", "uvarch": "aarch64"}
    if machine == "x86_64":
        return {"narch": "x64", "goarch": "amd64", "uvarch": "x86_64"}
    print(f"Error: unsupported architecture '{machine}'.", file=sys.stderr)
    sys.exit(1)
```

Update the one caller in `agtbox/install.py` (lines 53-61) so the suite stays green. The arch vars themselves are removed in Task 3:

```python
def agt_env(agent, do_core, machine):
    names = core.arch_names(machine)
    return [
        ("AGT_TOOLS", core.AGENT_TOOLS),
        ("AGT_NARCH", names["narch"]),
        ("AGT_GOARCH", names["goarch"]),
        ("AGT_NPM_PKGS", " ".join(agent.packages)),
        ("AGT_DO_CORE", "1" if do_core else "0"),
    ]
```

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `python3 -m unittest discover -s test`
Expected: OK, 83 tests (82 before, `test_arch_pair` became two tests)

- [ ] **Step 5: Commit**

```bash
git add agtbox/core.py agtbox/install.py test/test_core.py
git commit -m "refactor(core): arch_pair -> arch_names returning a dict"
```

---

### Task 2: The `agtbox/releases.py` table

**Files:**
- Create: `agtbox/releases.py`
- Test: `test/test_releases.py` (new)

**Interfaces:**
- Consumes: `core.arch_names(machine) -> dict` (Task 1).
- Produces:
  - `Release` — frozen dataclass with fields `name, api, tag_field, url, where="", required=False, dest="", bins=()`.
  - `TOOLS` — a 4-tuple of `Release` in execution order: node, uv, gh, glab.
  - `render(machine) -> (consts: str, calls: str)` — two newline-joined blocks with arch already substituted. Task 3 consumes this exact signature and return shape.

- [ ] **Step 1: Write the failing test**

Create `test/test_releases.py`:

```python
import re
import unittest

from agtbox import releases


class Table(unittest.TestCase):
    def test_four_tools_in_execution_order(self):
        self.assertEqual([t.name for t in releases.TOOLS],
                         ["node", "uv", "gh", "glab"])

    def test_node_required_and_others_optional(self):
        by_name = {t.name: t for t in releases.TOOLS}
        self.assertTrue(by_name["node"].required)
        for name in ("uv", "gh", "glab"):
            self.assertFalse(by_name[name].required, name)

    def test_uv_installs_both_uv_and_uvx(self):
        # The astral bootstrap installed uvx implicitly; the tarball path must not
        # silently drop it.
        uv = next(t for t in releases.TOOLS if t.name == "uv")
        self.assertEqual(sorted(b.split(":")[1] for b in uv.bins), ["uv", "uvx"])

    def test_uv_resolves_from_the_github_api(self):
        uv = next(t for t in releases.TOOLS if t.name == "uv")
        self.assertEqual(uv.api,
                         "https://api.github.com/repos/astral-sh/uv/releases/latest")

    def test_nothing_references_the_astral_bootstrap_host(self):
        # The whole point of the change: locked-down hosts reach GitHub, not astral.sh.
        blob = repr(releases.TOOLS)
        self.assertNotIn("astral.sh", blob)


class Render(unittest.TestCase):
    def test_substitutes_uv_arch(self):
        self.assertIn("uv-x86_64-unknown-linux-gnu.tar.gz",
                      releases.render("x86_64")[0])
        self.assertIn("uv-aarch64-unknown-linux-gnu.tar.gz",
                      releases.render("aarch64")[0])

    def test_substitutes_go_arch_for_gh(self):
        self.assertIn("gh_@VERNOV@_linux_amd64.tar.gz", releases.render("x86_64")[0])
        self.assertIn("gh_@VERNOV@_linux_arm64.tar.gz", releases.render("aarch64")[0])

    def test_substitutes_node_arch(self):
        self.assertIn("node-@VER@-linux-x64.tar.xz", releases.render("x86_64")[0])
        self.assertIn("node-@VER@-linux-arm64.tar.xz", releases.render("aarch64")[0])

    def test_no_python_placeholders_survive(self):
        # Guards against a typo like {uarch} for {uvarch}, which would otherwise only
        # surface as a 404 on a user's first cold install. Bash's own ${VAR} braces are
        # stripped first: those are MEANT to survive into the script, where bash expands
        # them. Only a leftover Python format placeholder is a defect here.
        for machine in ("x86_64", "aarch64"):
            consts, calls = releases.render(machine)
            blob = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", "", consts + calls)
            self.assertNotIn("{", blob, machine)
            self.assertNotIn("}", blob, machine)

    def test_node_dest_is_a_bash_variable_reference(self):
        # ${AGT_TOOLS} must reach the script intact -- nothing substitutes @AGT_TOOLS@,
        # so a literal would extract node into a directory of that name and leave
        # ${AGT_TOOLS}/node/bin/npm missing.
        node = next(t for t in releases.TOOLS if t.name == "node")
        self.assertEqual(node.dest, "${AGT_TOOLS}/node")
        self.assertIn('"${AGT_TOOLS}/node"', releases.render("x86_64")[1])

    def test_calls_use_the_right_mode_per_tool(self):
        calls = releases.render("x86_64")[1]
        self.assertIn("install_tree node required", calls)
        for tool in ("uv", "gh", "glab"):
            self.assertIn(f"install_bins {tool} optional", calls)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest test.test_releases -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agtbox.releases'`

- [ ] **Step 3: Write minimal implementation**

Create `agtbox/releases.py`:

```python
"""The toolchain's downloadable tools, as data.

One entry per tool installed from a release tarball. `install.py` renders these into
the bash install script; nothing in this module runs a command, opens a socket, or
touches the disk.

Two placeholder dialects meet here and must not be confused:

  {narch} {goarch} {uvarch}  Python str.format -- substituted HERE, from
                             core.arch_names(), before the script is built.
  @VER@ @VERNOV@             Left for bash -- substituted inside the sandbox once
                             the version has been resolved. @VERNOV@ is the version
                             with a leading 'v' stripped (gh and glab put the bare
                             number in their asset names and paths).
"""
from dataclasses import dataclass

from agtbox import core


@dataclass(frozen=True)
class Release:
    """One downloadable tool.

    `where` names a key a JSON row must have truthy to be chosen (node's "lts");
    empty means take the first row. `dest` selects tree mode (extract in place,
    --strip-components=1); `bins` selects bins mode ("src-in-tarball:dest-name").
    Exactly one of the two is set.
    """
    name: str
    api: str
    tag_field: str
    url: str
    where: str = ""
    required: bool = False
    dest: str = ""
    bins: tuple = ()


TOOLS = (
    Release(
        name="node",
        required=True,
        api="https://nodejs.org/dist/index.json",
        where="lts",
        tag_field="version",
        url="https://nodejs.org/dist/@VER@/node-@VER@-linux-{narch}.tar.xz",
        dest="${AGT_TOOLS}/node",
    ),
    Release(
        name="uv",
        api="https://api.github.com/repos/astral-sh/uv/releases/latest",
        tag_field="tag_name",
        url="https://github.com/astral-sh/uv/releases/download/@VER@/"
            "uv-{uvarch}-unknown-linux-gnu.tar.gz",
        bins=("uv-{uvarch}-unknown-linux-gnu/uv:uv",
              "uv-{uvarch}-unknown-linux-gnu/uvx:uvx"),
    ),
    Release(
        name="gh",
        api="https://api.github.com/repos/cli/cli/releases/latest",
        tag_field="tag_name",
        url="https://github.com/cli/cli/releases/download/@VER@/"
            "gh_@VERNOV@_linux_{goarch}.tar.gz",
        bins=("gh_@VERNOV@_linux_{goarch}/bin/gh:gh",),
    ),
    Release(
        name="glab",
        api="https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases",
        tag_field="tag_name",
        url="https://gitlab.com/gitlab-org/cli/-/releases/@VER@/downloads/"
            "glab_@VERNOV@_linux_{goarch}.tar.gz",
        bins=("bin/glab:glab",),
    ),
)


def render(machine):
    """(constants block, call lines) for `machine`, arch already substituted.

    Constants keep the long URL templates off the call lines so the generated
    script stays readable when dumped or eyeballed.
    """
    names = core.arch_names(machine)
    consts, calls = [], []
    for t in TOOLS:
        up = t.name.upper()
        consts.append(f"readonly {up}_API='{t.api}'")
        consts.append(f"readonly {up}_URL='{t.url.format(**names)}'")
        req = "required" if t.required else "optional"
        head = (f'{t.name} {req} "${up}_API" "{t.where}" {t.tag_field} "${up}_URL"')
        if t.dest:
            calls.append(f'install_tree {head} "{t.dest}"')
        else:
            bins = " ".join(f'"{b.format(**names)}"' for b in t.bins)
            calls.append(f"install_bins {head} {bins}")
    return "\n".join(consts), "\n".join(calls)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest test.test_releases -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Run the full suite (nothing else should have moved)**

Run: `python3 -m unittest discover -s test`
Expected: OK, 94 tests

- [ ] **Step 6: Commit**

```bash
git add agtbox/releases.py test/test_releases.py
git commit -m "feat(releases): declarative table of downloadable toolchain tools"
```

---

### Task 3: Assemble `install_script` from the table

**Files:**
- Modify: `agtbox/install.py:6-50` (`install_script`), `:53-61` (`agt_env`), `:64-72` (`install_env`), `:75-86` (`ensure_tools`)
- Test: `test/test_install.py` (all four existing test classes touched)

**Interfaces:**
- Consumes: `releases.render(machine)` (Task 2), `core.arch_names` (Task 1).
- Produces: `install.install_script(machine) -> str` (now takes the machine), `install.agt_env(agent, do_core) -> list[tuple]` and `install.install_env(agent, do_core) -> list[tuple]` (both lose the `machine` parameter — arch no longer crosses into the sandbox).

- [ ] **Step 1: Write the failing tests**

In `test/test_install.py`, update all three `install_script()` call sites to pass a machine, rewrite the two tests that assert on removed text, and drop `machine` from the `agt_env`/`install_env` calls:

```python
class InstallScript(unittest.TestCase):
    def test_is_syntactically_valid_bash(self):
        # install_script() is an assembled shell string; a stray quote would break
        # first-run install for every user and no other test would notice.
        # `bash -n` parses without executing (no network/downloads).
        bash = shutil.which("bash") or "/bin/bash"
        r = subprocess.run([bash, "-n", "-c", install.install_script("x86_64")],
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_aux_tools_best_effort_and_stamp_last(self):
        s = install.install_script("x86_64")
        for tool in ("uv", "gh", "glab"):
            self.assertIn(f"install_bins {tool} optional", s)
        self.assertIn("install_tree node required", s)
        self.assertLess(s.index("install_bins glab"),
                        s.index('date > "${AGT_TOOLS}/.stamp"'))

    def test_core_block_is_gated_and_npm_always_runs(self):
        s = install.install_script("x86_64")
        self.assertIn('if [ "${AGT_DO_CORE}" = "1" ]', s)        # core gated
        self.assertIn("npm", s)
        # npm install references AGT_NPM_PKGS unconditionally (outside the core gate)
        self.assertIn("${AGT_NPM_PKGS}", s)


class AgtEnv(unittest.TestCase):
    def test_carries_packages_and_do_core(self):
        from agtbox.agents.claude import Claude
        pairs = dict(install.agt_env(Claude(), do_core=True))
        self.assertEqual(pairs["AGT_NPM_PKGS"], "@anthropic-ai/claude-code")
        self.assertEqual(pairs["AGT_DO_CORE"], "1")

    def test_arch_vars_no_longer_cross_the_boundary(self):
        # Arch is substituted in Python and baked into the script literals.
        from agtbox.agents.claude import Claude
        pairs = dict(install.agt_env(Claude(), do_core=True))
        self.assertNotIn("AGT_NARCH", pairs)
        self.assertNotIn("AGT_GOARCH", pairs)
```

In `InstallEnvContents`, change both `install.install_env(Claude(), True, "aarch64")` calls to `install.install_env(Claude(), True)`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest test.test_install -v`
Expected: FAIL — `install_script() takes 0 positional arguments but 1 was given`

- [ ] **Step 3: Write the implementation**

> **Pre-validated:** the shell below was assembled with stub `render()` output and
> checked before this plan was written — `bash -n` parses it, and running it with a
> stub `curl`/`tar` gives exit 0 + three warnings + `.stamp` when the auxiliary hosts
> fail, and exit 1 + no `.stamp` when node fails. If you see different behavior, you
> have transcribed something incorrectly; diff against this block rather than
> redesigning it.

Replace the top of `agtbox/install.py` (imports through `install_env`) with:

```python
import os
import sys
import textwrap

from agtbox import core, releases

_SCRIPT = r'''set -euo pipefail
mkdir -p "${AGT_TOOLS}/node" "${AGT_TOOLS}/bin"
WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

@CONSTS@

# One resolver for every tool: rows may be a dict (gh) or a list (glab, node), and
# `where` optionally names a key the chosen row must have truthy (node's "lts").
# python3 stderr is dropped so a blocked host yields our warning, not a traceback.
resolve() {  # api  where_key  field
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

# The blocked-host hint is derived from the API URL so it cannot drift from the URL
# it describes. Note this names the *resolution* host (api.github.com), which is the
# one that must be reachable even when the download host is fine.
_warn_skip() {  # name  api
  local host=${2#https://}; host=${host%%/*}
  echo "Agent Box: WARNING -- $1 install failed (${host} blocked?); skipping." >&2
  skipped="${skipped} $1"
}

install_tree() {  # name required api where field url dest
  local name=$1 req=$2; shift 2
  _try_tree "$@" && return 0
  [ "$req" = required ] && return 1
  _warn_skip "$name" "$1"
  return 0
}

install_bins() {  # name required api where field url src:dst...
  local name=$1 req=$2; shift 2
  local api=$1 where=$2 field=$3 tmpl=$4; shift 4
  _try_bins "$api" "$where" "$field" "$tmpl" "${WORK}/${name}" "$@" && return 0
  [ "$req" = required ] && return 1
  _warn_skip "$name" "$api"
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
  echo 'Agent Box: installing the agent CLI...' >&2
  "${AGT_TOOLS}/node/bin/npm" install -g --prefix "${AGT_TOOLS}" --no-fund --no-audit ${AGT_NPM_PKGS}
fi
'''


def install_script(machine):
    """The install script for `machine`, assembled from the releases table. Arch is
    substituted here, so no arch env var crosses into the sandbox; only @VER@ is
    left for bash, since the version is resolved inside the sandbox."""
    consts, calls = releases.render(machine)
    return (_SCRIPT
            .replace("@CONSTS@", consts)
            .replace("@CALLS@", textwrap.indent(calls, "  ")))


def agt_env(agent, do_core):
    return [
        ("AGT_TOOLS", core.AGENT_TOOLS),
        ("AGT_NPM_PKGS", " ".join(agent.packages)),
        ("AGT_DO_CORE", "1" if do_core else "0"),
    ]
```

Then update `install_env` to drop `machine` (keep its existing docstring):

```python
def install_env(agent, do_core):
    return list(core.resolve_env([], [])) + agt_env(agent, do_core)
```

And the tail of `ensure_tools`, which must now resolve the machine once — under podman `install_machine()` spawns a container, so calling it twice would cost a second one:

```python
    machine = sandbox.install_machine()
    pairs = install_env(agent, do_core)
    sandbox.install(install_script(machine), pairs)
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m unittest discover -s test`
Expected: OK, 94 tests (`AgtEnv` gained a test)

- [ ] **Step 5: Eyeball the generated script once**

Run: `python3 -c "from agtbox import install; print(install.install_script('x86_64'))"`
Expected: four `readonly *_API`/`*_URL` pairs with `x86_64`/`amd64`/`x64` substituted, and four indented `install_tree`/`install_bins` calls inside the `AGT_DO_CORE` gate. (Do not expect the script to be brace-free — it is full of `${AGT_TOOLS}` and bash function bodies. The no-Python-placeholder guard lives in Task 2's `test_no_python_placeholders_survive`, which checks `render()` output only.)

- [ ] **Step 6: Commit**

```bash
git add agtbox/install.py test/test_install.py
git commit -m "refactor(install): assemble the install script from the releases table"
```

---

### Task 4: End-to-end skip-path test with stubbed `curl`/`tar`

**Files:**
- Test: `test/test_install.py` (append one class)

**Interfaces:**
- Consumes: `install.install_script(machine)` (Task 3).
- Produces: nothing other tasks depend on.

This is the highest-value new test: `install_bins`' warn-and-continue path is what silently regresses, and `set -e` makes it easy to get wrong. Stubs let the whole script run with no network.

- [ ] **Step 1: Write the failing test**

Append to `test/test_install.py` (add `os`, `tempfile`, and `from pathlib import Path` to the imports if absent):

```python
class SkipPathEndToEnd(unittest.TestCase):
    """Run the real generated script with curl/tar stubbed: nodejs.org answers, the
    auxiliary hosts fail. The script must warn, skip all three, still write .stamp,
    and exit 0 -- otherwise a blocked host stops the core toolchain completing."""

    def _stub_dir(self, tmp):
        stub = tmp / "stub"
        stub.mkdir()
        # curl: serve node's index.json, fail (7) for anything else.
        (stub / "curl").write_text(
            '#!/bin/sh\n'
            'for a in "$@"; do\n'
            '  case "$a" in\n'
            '    *nodejs.org*) echo \'[{"version":"v20.0.0","lts":"Iron"}]\'; exit 0 ;;\n'
            '  esac\n'
            'done\n'
            'exit 7\n')
        # tar: accept whatever it is handed, so node's "download" succeeds.
        (stub / "tar").write_text('#!/bin/sh\nexit 0\n')
        for f in ("curl", "tar"):
            (stub / f).chmod(0o755)
        return stub

    def test_blocked_aux_hosts_warn_and_still_stamp(self):
        bash = shutil.which("bash") or "/bin/bash"
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        stub = self._stub_dir(tmp)
        tools = tmp / "tools"
        (tools / "bin").mkdir(parents=True)
        env = {**os.environ,
               "PATH": f"{stub}:{os.environ['PATH']}",
               "AGT_TOOLS": str(tools),
               "AGT_NPM_PKGS": "",
               "AGT_DO_CORE": "1"}
        r = subprocess.run([bash, "-c", install.install_script("x86_64")],
                           capture_output=True, text=True, env=env, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        for tool in ("uv", "gh", "glab"):
            self.assertIn(f"WARNING -- {tool} install failed", r.stderr)
        self.assertNotIn("WARNING -- node", r.stderr)         # node succeeded
        self.assertIn("skipped: uv gh glab", r.stderr)
        self.assertTrue((tools / ".stamp").exists(), r.stderr)

    def test_warning_names_the_resolution_host(self):
        # Derived from the API URL, so it cannot drift: uv must blame api.github.com,
        # not astral.sh and not github.com.
        bash = shutil.which("bash") or "/bin/bash"
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        stub = self._stub_dir(tmp)
        tools = tmp / "tools"
        (tools / "bin").mkdir(parents=True)
        env = {**os.environ,
               "PATH": f"{stub}:{os.environ['PATH']}",
               "AGT_TOOLS": str(tools),
               "AGT_NPM_PKGS": "",
               "AGT_DO_CORE": "1"}
        r = subprocess.run([bash, "-c", install.install_script("x86_64")],
                           capture_output=True, text=True, env=env, check=False)
        self.assertIn("uv install failed (api.github.com blocked?)", r.stderr)
        self.assertIn("glab install failed (gitlab.com blocked?)", r.stderr)
        self.assertNotIn("astral.sh", r.stderr)
```

- [ ] **Step 2: Run the tests**

Run: `python3 -m unittest test.test_install.SkipPathEndToEnd -v`
Expected: PASS, 2 tests. If they fail on exit status, read stderr — the usual cause is a `return 0` missing from a skip path, which is exactly what this test exists to catch.

- [ ] **Step 3: Run the full suite**

Run: `python3 -m unittest discover -s test`
Expected: OK, 96 tests

- [ ] **Step 4: Commit**

```bash
git add test/test_install.py
git commit -m "test(install): end-to-end skip path with stubbed curl/tar"
```

---

### Task 5: Documentation

**Files:**
- Modify: `CLAUDE.md` (the "Auto-install on first run" paragraph)
- Modify: `README.md:18`, `README.md:127`

**Interfaces:**
- Consumes: the finished behavior from Tasks 1-4.
- Produces: nothing code depends on.

Fold in `main`'s factual deltas but **not** `main`'s wording — that text still says `-v`/`-t`/`AGTBOX_REINSTALL=1`, all renamed on this branch.

- [ ] **Step 1: Rewrite the CLAUDE.md auto-install paragraph**

Replace the sentence beginning "The shared `install_script` (in `agtbox/install.py`) resolves the **current LTS** node…" through "…Reads inputs from `AGT_*` env vars." with:

```markdown
The install script is **assembled from a table** (`agtbox/releases.py`): one `Release` entry per downloadable tool — node, `uv`, `gh`, `glab` — carrying its JSON API, the key holding the version, a URL template, and either a destination tree (node) or `src:dest` binary pairs (the rest). `install_script(machine)` renders that table into a constants block plus one `install_tree`/`install_bins` call per tool. **CPU-arch spellings are substituted in Python** (`core.arch_names` → `{narch}`/`{goarch}`/`{uvarch}`), so no arch env var crosses into the sandbox; only `@VER@`/`@VERNOV@` survive, resolved in bash after the version lookup. A single `resolve()` handles all three JSON shapes (dict, list, list-filtered-on-a-key), and one `mktemp -d` + `EXIT` trap covers every extraction. `uv` comes from its **GitHub release tarball** (`uv` and `uvx` both), not the `astral.sh` bootstrap — locked-down hosts commonly reach GitHub but not `astral.sh`. **node is `required` (fail hard); `uv`/`gh`/`glab` are `optional`** — a failure warns, skips, and still writes `.stamp` (so the core toolchain isn't re-downloaded every run; `-u` retries the skipped ones). The blocked-host hint in that warning is derived from the tool's API URL, so it names the host that actually has to be reachable (e.g. `api.github.com`) and can't drift. The script always targets **linux**; the **arch** comes from `Sandbox.install_machine()` — the host's `uname -m` under bwrap, the *container's* under podman. Reads inputs from `AGT_TOOLS`/`AGT_NPM_PKGS`/`AGT_DO_CORE`. Idempotent (re-run upgrades).
```

- [ ] **Step 2: Update the two README spots**

(`README.md:31`, the quick-start sentence, deliberately stays as-is: the uv
source belongs in the requirements bullet and the locked-down note, and
repeating it in the quick start would just be noise.)

`README.md:18` — append the uv source to the sentence:

```markdown
- Everything else — node, `uv`, and the agent CLIs — is **installed automatically on first run** into `~/.local/share/agent-box` (the same toolchain for both sandboxes), with `uv`, `gh`, and `glab` fetched from their GitHub/GitLab release tarballs. Local-model *serving* is out of scope (run e.g. [LM Studio](https://lmstudio.ai) separately and point an agent at it; see below).
```

`README.md:127` — drop `astral.sh`, name the resolution hosts:

```markdown
- **Locked-down network?** The agent CLIs (node + npm) are **required**; `uv`, `gh`, and `glab` are **auxiliary** and installed best-effort. Each resolves its latest version from an API (`api.github.com` for `uv` and `gh`, `gitlab.com` for `glab`) before downloading, so that host has to be reachable too — the warning names whichever one failed. If they're blocked while the node/npm registries are allowed, the install **warns and continues** — the core toolchain still completes (so the agents run) and isn't re-downloaded every launch. Re-run with `-u` to retry the skipped tools once they're reachable (e.g. via a proxy or once a host is allowlisted).
```

- [ ] **Step 3: Verify no stale references remain**

Run: `grep -rn 'astral\.sh\|arch_pair\|AGT_NARCH\|AGT_GOARCH\|AGT_UVARCH' README.md CLAUDE.md agtbox/ test/`
Expected: no output.

- [ ] **Step 4: Run the full suite one more time**

Run: `python3 -m unittest discover -s test`
Expected: OK, 96 tests

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: describe the releases table and the GitHub-sourced uv install"
```

---

## Done criteria

- `python3 -m unittest discover -s test` passes (96 tests).
- `python3 -c "from agtbox import releases; print(releases.render('aarch64'))"` shows `arm64`/`aarch64` substituted and contains no `{`/`}`.
- `grep -rn 'astral\.sh' .` returns nothing outside `docs/` and `.git/`.
- The branch is ready for a PR against `plugin-architecture`.
