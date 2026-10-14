# Install Release Table — Design

**Date:** 2026-09-21
**Branch:** `install-release-table` (PR target: `plugin-architecture`)

## Goal

Replace the four hand-duplicated download blocks in `agtbox/install.py`'s embedded
bash with a declarative Python table, and in doing so land `main`'s change of
installing `uv` from GitHub release assets instead of the `astral.sh` bootstrap
script.

## Background

`main` and `plugin-architecture` diverged at `09cb0ee`. `main` gained four commits,
of which one is substantive: PR #40 replaced the `uv` install transport because
**locked-down hosts can reach GitHub but not `astral.sh`**. That change landed in
`bin/agtbox.py`, which `plugin-architecture` has since reduced to a ~10-line shim,
so it cannot be merged — it has to be reimplemented in the package.

Reimplementing it verbatim would make the underlying problem worse. `uv` was the one
auxiliary tool that was *not* boilerplate (two lines piping a vendor installer to
`sh`); `main` turned it into a third copy of the six-line fetch-tag → download →
extract → install shape. The install script is 44 lines, and four of its blocks
(node, uv, gh, glab) repeat the same five moves. Two defects are visible in that
duplication today:

- `glab` extracts to a bare `/tmp/bin/glab` at the tmp root while `gh` and `uv`
  extract into version-and-arch-named directories. Nothing cleans `/tmp` up.
- The arch-to-asset-name mapping is split across two languages: `core.arch_pair`
  hands bash `narch`/`goarch` while the filename templates live in the bash string.
  This is why `main`'s one-tool change had to touch `arch_pair`, `_agt_env`,
  `install_script`, and tests in two files.

## Non-goals

- **Lint cleanup.** `plugin-architecture` currently fails CI lint (8 errors under the
  existing narrow paths; 32 across the package). Deferred to a follow-up so this
  change can be reviewed on its own.
- **The Python 3.11 floor and `actions/setup-python`.** Still an unported `main`
  change. It belongs with the lint work, because pinning ruff's target version is
  what it is for.
- **`lint.yml` path update.** The workflow still lints `bin/agtbox.py
  test/test_agtbox.py`, stale since the package restructure. Same follow-up.
- **Toolchain locking.** Two cold runs on the same arch both see no `.stamp` and race
  to untar node into `${AGENT_TOOLS}/node`. Pre-existing; filed separately.

## Constraints (behavior that must not change)

- Downloads happen **inside the sandbox**. The launcher makes no network calls. Under
  podman the host may be macOS, so the host's arch, proxy config, and trust store are
  not the ones the toolchain runs under — `Sandbox.install_machine()` exists for this
  reason and resolution must stay on its side of the boundary.
- **Install once per arch, then share.** `AGENT_TOOLS` is `~/.local/share/agent-box/{uname -m}`;
  `ensure_tools` returns early when `.stamp` exists, the agent's bin is present, and
  `-u` was not passed. The toolchain stays a read-write bind so in-session `npm -g`,
  `uv tool install`, and CLI self-updates persist and are shared.
- **node is required; uv/gh/glab are best-effort.** A blocked auxiliary host warns,
  skips, and still writes `.stamp`. A node failure aborts before `.stamp`.
- `.stamp` is written after the core block and before the npm step.
- `Sandbox.install(script, pairs)` is unchanged; neither sandbox is touched.

## Architecture

### `agtbox/releases.py` (new)

Pure data plus one rendering function. No side effects, no imports beyond stdlib and
`agtbox.core`. Imported only by `agtbox/install.py`.

Four entries — node included. The boundary was reconsidered during design: node's
JSON shape differs from `gh`'s, but `gh`'s and `glab`'s already differ from each
other, so shape is a per-tool field rather than a boundary. Compression is derivable
from the URL suffix. What genuinely differs is two ordinary two-valued fields,
extraction mode and `required`, and making `required` explicit is an improvement —
today node being mandatory is encoded by the *absence* of a `|| warn`.

| name | required | api | where | field | mode |
|------|----------|-----|-------|-------|------|
| node | yes | `https://nodejs.org/dist/index.json` | `lts` | `version` | tree |
| uv   | no  | `https://api.github.com/repos/astral-sh/uv/releases/latest` | — | `tag_name` | bins |
| gh   | no  | `https://api.github.com/repos/cli/cli/releases/latest` | — | `tag_name` | bins |
| glab | no  | `https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases` | — | `tag_name` | bins |

URL templates (Python `{}` placeholders are substituted at render time from
`arch_names`; `@VER@` / `@VERNOV@` are left for bash):

- node: `https://nodejs.org/dist/@VER@/node-@VER@-linux-{narch}.tar.xz` → `${AGT_TOOLS}/node`
- uv:   `https://github.com/astral-sh/uv/releases/download/@VER@/uv-{uvarch}-unknown-linux-gnu.tar.gz`
- gh:   `https://github.com/cli/cli/releases/download/@VER@/gh_@VERNOV@_linux_{goarch}.tar.gz`
- glab: `https://gitlab.com/gitlab-org/cli/-/releases/@VER@/downloads/glab_@VERNOV@_linux_{goarch}.tar.gz`

Binary maps (`src-in-tarball: dest-name`), also subject to substitution:

- uv:   `uv-{uvarch}-unknown-linux-gnu/uv: uv`, `uv-{uvarch}-unknown-linux-gnu/uvx: uvx`
- gh:   `gh_@VERNOV@_linux_{goarch}/bin/gh: gh`
- glab: `bin/glab: glab`

`uvx` is listed explicitly because the astral bootstrap installed it implicitly; it
is the tool surface most easily lost in this refactor.

### `core.arch_pair` → `core.arch_names`

Returns a dict, not a tuple:

```python
{"narch": "arm64", "goarch": "arm64", "uvarch": "aarch64"}   # aarch64
{"narch": "x64",   "goarch": "amd64", "uvarch": "x86_64"}    # x86_64
```

A dict so that a future tool with a fourth arch spelling adds a key rather than
renumbering every unpack site — the churn `main` hit. Unsupported arch keeps the
current behavior: message to stderr, `sys.exit(1)`. One caller in `agtbox/`
(`install.py`), plus `test/test_core.py`.

### The generated script

Arch is substituted in Python, so the script carries literals and **no arch env var
crosses the boundary**. `AGT_NARCH` and `AGT_GOARCH` lose their last readers and are
removed; `agt_env` shrinks to `AGT_TOOLS`, `AGT_NPM_PKGS`, `AGT_DO_CORE`. `main`'s
`AGT_UVARCH` is therefore never introduced — its intent survives, its mechanism
dissolves.

Rendering emits a constants block (one `readonly` per tool, arch already
substituted) followed by the call lines, so the long URL templates stay off the call
lines and the generated script is readable when eyeballed or dumped by `bash -n`:

```bash
readonly NODE_API='https://nodejs.org/dist/index.json'
readonly NODE_URL='https://nodejs.org/dist/@VER@/node-@VER@-linux-x64.tar.xz'
readonly UV_API='https://api.github.com/repos/astral-sh/uv/releases/latest'
readonly UV_URL='https://github.com/astral-sh/uv/releases/download/@VER@/uv-x86_64-unknown-linux-gnu.tar.gz'
# ... gh, glab
```

Emitted call lines (x86_64 shown):

```bash
install_tree node required "$NODE_API" lts     version  "$NODE_URL" "${AGT_TOOLS}/node"
install_bins uv   optional "$UV_API"   ""      tag_name "$UV_URL" \
  "uv-x86_64-unknown-linux-gnu/uv:uv" "uv-x86_64-unknown-linux-gnu/uvx:uvx"
install_bins gh   optional "$GH_API"   ""      tag_name "$GH_URL" \
  "gh_@VERNOV@_linux_amd64/bin/gh:gh"
install_bins glab optional "$GLAB_API" ""      tag_name "$GLAB_URL" "bin/glab:glab"
```

Table order is execution order; node first, and `required` + `set -e` aborts before
`.stamp`.

**One `resolve()` replaces three bespoke `python3 -c` expressions.** Two plain string
params cover all three JSON shapes (dict / list / list-filtered-on-a-key), so no
Python expression is carried as data and no `eval` is needed:

```bash
resolve() {  # api  where_key  field
  curl -fsSL "$1" | WHERE="$2" FIELD="$3" python3 -c '
import json, os, sys
rows = json.load(sys.stdin)
rows = rows if isinstance(rows, list) else [rows]
w = os.environ["WHERE"]
print(next(r for r in rows if not w or r.get(w))[os.environ["FIELD"]])'
}
```

`install_tree` and `install_bins` share one `_download` helper and differ only in
disposition: `--strip-components=1` into a destination tree, versus `install -m755`
of `src:dst` pairs into `${AGT_TOOLS}/bin`.

### Substitution rules

- Python `{narch}` / `{goarch}` / `{uvarch}` — rendered in Python via
  `str.format(**arch_names(machine))`.
- `@VER@` — the resolved version, substituted in bash: `${s//@VER@/$ver}`.
- `@VERNOV@` — the same with a leading `v` stripped: `${s//@VERNOV@/${ver#v}}`.

Substitution runs over **every argument**, not only the URL: `gh`'s binary lives at
`gh_${ver#v}_linux_{goarch}/bin/gh`, with the version in the path.

Compression is taken from the URL suffix (`.tar.xz` → `-xJ`, `.tar.gz` → `-xz`), not
from a table field.

### Temp directories

One `WORK=$(mktemp -d)` at script top with `trap 'rm -rf "$WORK"' EXIT`; each tool
extracts into `$WORK/<name>`. This fixes both current defects at once — the
`/tmp/bin/glab` collision at the tmp root, and the absent cleanup.

### Error handling

`optional` wraps the tool in `|| { warn; skipped="$skipped <name>"; return 0; }`. The
`return 0` is load-bearing: without it `set -e` takes the script down. `required` is
not wrapped, so `set -e` aborts before `.stamp`.

The blocked-host hint is derived from the API URL rather than hand-written per tool,
so it cannot drift from the URL it describes (`main`'s change had to remember to edit
`astral.sh` → `github.com` by hand):

```bash
host=${api#https://}; host=${host%%/*}
echo "Agent Box: WARNING -- ${name} install failed (${host} blocked?); skipping." >&2
```

This preserves the `WARNING -- <tool> install failed` substring and makes uv's
message name `api.github.com` — the host that must be reachable for resolution even
when `github.com` itself is fine.

The skipped summary is unchanged:
`Agent Box: core toolchain ready; skipped:${skipped} (retry later with -u).`

## Files touched

| File | Change |
|------|--------|
| `agtbox/releases.py` | New: the four-entry table + render-to-call-lines |
| `agtbox/core.py` | `arch_pair` → `arch_names` (dict) |
| `agtbox/install.py` | `install_script` assembles from the table; `agt_env` drops the arch vars |
| `test/test_core.py` | `arch_names` tests replace `arch_pair` |
| `test/test_install.py` | Two rewrites (below) + new table/render tests |
| `CLAUDE.md` | "Auto-install on first run" paragraph |
| `README.md` | Requirements bullet, quick-start sentence, locked-down-network bullet |

## Testing

**New:**

- `arch_names` returns the right dict per arch; `SystemExit` on unsupported.
- Arch substitution is real: rendering for `x86_64` puts
  `uv-x86_64-unknown-linux-gnu.tar.gz` in uv's line, `aarch64` puts `uv-aarch64-…`;
  likewise `gh`'s `amd64`/`arm64` and node's `x64`/`arm64`.
- **No unsubstituted Python placeholders survive rendering** (whitelisting `@VER@` /
  `@VERNOV@`). Catches `{uarch}`-for-`{uvarch}` typos that would otherwise surface as
  a 404 on a user's first run.
- Table intent: node `required`, the rest `optional`; uv installs both `uv` and
  `uvx`; nothing references `astral.sh`; uv resolves via
  `api.github.com/repos/astral-sh/uv`.
- **Shell-level skip-path test:** a stub `curl` on `PATH` exiting non-zero, running
  the generated script with `AGT_DO_CORE=1`, asserting exit 0 plus a warning on
  stderr. The `install_bins` skip path is the riskiest new code and is untested
  today — it is exactly what regresses silently.

**Rewritten (asserting the same intent against the table, not against literal text):**

- `test_aux_tools_best_effort_and_stamp_last` greps three literal
  `WARNING -- <tool> install failed` strings that become one template inside
  `install_bins`. Becomes: uv/gh/glab optional, node required, all `install_*` lines
  emitted before the `.stamp` line.
- `AgtEnv.test_carries_packages_and_do_core` asserts `pairs["AGT_NARCH"] == "arm64"`.
  Becomes: the three surviving vars present, arch vars absent.

**Unchanged and still passing:** `test_is_syntactically_valid_bash` (`bash -n`, more
valuable now the script is assembled), `test_core_block_is_gated_and_npm_always_runs`,
`InstallEnvContents`.

## Docs

- `CLAUDE.md` — rewrite the "Auto-install on first run" paragraph for the table and
  `install_tree`/`install_bins`; `astral.sh` → GitHub release assets; `arch_pair` →
  `arch_names`.
- `README.md` — requirements bullet (uv from GitHub release assets), quick-start
  install sentence, locked-down-network bullet (drop `astral.sh`, name the API hosts
  that must be reachable).

`main`'s factual deltas are folded in but not its wording: that text still says
`-v`/`-t`/`AGTBOX_REINSTALL=1`, all renamed on this branch.

## Known follow-up

This branch reimplements `main`'s change; it does not merge `main`. Merging
`main` into `plugin-architecture` afterwards will still conflict in `bin/agtbox.py`,
`test/test_agtbox.py`, `CLAUDE.md`, and `README.md` — resolve by taking
`plugin-architecture`'s side, since the work lives in its new home.
`docs/superpowers/plans/2026-08-20-uv-github-install.md` merges clean.
