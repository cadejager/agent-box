# UV GitHub Release Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current `uv` bootstrap-script install with a direct GitHub release binary install while preserving the existing best-effort toolchain setup behavior.

**Architecture:** Keep the change tightly scoped to the toolchain install path in `bin/agtbox.py`. Reuse the existing GitHub-release pattern already used for `gh`, extend the Python-side arch mapping only as needed for `uv` asset names, and preserve the current “warn and continue” semantics when `uv` cannot be downloaded.

**Tech Stack:** Python 3 launcher (`bin/agtbox.py`), bash install script string, curl, tar, unittest.

## Global Constraints

- Do not change the Slurm-related shell behavior as part of this work.
- Do not introduce a `pip`-based `uv` install path.
- Do not keep `astral.sh` as the `uv` install transport; use GitHub release assets directly.
- Preserve the current best-effort behavior for `uv` installation: warn and continue on failure.
- Preserve the current support envelope of the launcher unless a test proves otherwise; today the launcher only supports `x86_64` and `aarch64` in `arch_pair()`.
- Keep the install target as Linux-only, because installs run inside bwrap or podman Linux environments.
- Install both `uv` and `uvx` from the release tarball to avoid regressing the current tool surface.
- Do not commit as part of this task; the user will review first.

---

## File Structure

- `bin/agtbox.py`
  - Extend or rename the current arch mapping helper so it can provide the `uv` asset arch name in addition to the existing Node and Go arch names.
  - Replace the `uv` install stanza in `install_script()` with a direct GitHub release download and extraction flow.
  - Keep `gh` and `glab` install behavior unchanged.
- `test/test_agtbox.py`
  - Add or update unit tests for the arch mapping helper.
  - Update the existing `Helpers.test_arch_pair` expectation for the new helper shape.
  - Add install-script assertions that the `uv` path no longer references `astral.sh` and instead references the GitHub API and Linux tarball asset names.
- `README.md`
  - Update the toolchain-install description and locked-down-network note to explain that `uv` now comes from GitHub releases instead of `astral.sh`.
- `CLAUDE.md`
  - Update the architecture/install notes so future sessions know `uv` is installed from GitHub releases, not the Astral bootstrap URL.

## Proposed Implementation Notes

- Use the GitHub API endpoint `https://api.github.com/repos/astral-sh/uv/releases/latest` to resolve the latest tag, matching the existing `gh` install style.
- Download the Linux GNU archive directly from GitHub release assets, not from `releases.astral.sh` and not via `uv-installer.sh`.
- For the currently supported architectures, the expected asset names are:
  - `x86_64` → `uv-x86_64-unknown-linux-gnu.tar.gz`
  - `aarch64` → `uv-aarch64-unknown-linux-gnu.tar.gz`
- Extract the tarball into `/tmp` and install both `uv` and `uvx` into `${AGT_TOOLS}/bin`.
- Keep the user-facing warning text explicit about GitHub being blocked rather than `astral.sh` being blocked.

### Task 1: Add UV asset mapping support in the launcher

**Files:**
- Modify: `bin/agtbox.py:361-370`
- Modify: `test/test_agtbox.py:537-541`
- Test: `test/test_agtbox.py`

**Interfaces:**
- Consumes: current `arch_pair(machine)` helper
- Produces: a helper that returns the Node arch, Go arch, and `uv` Linux asset arch token for a supported machine

- [ ] **Step 1: Write the failing unit tests for arch mapping**

```python
class Helpers(LauncherTest):
    def test_arch_pair(self):
        self.assertEqual(agtbox.arch_pair("aarch64"), ("arm64", "arm64", "aarch64"))
        self.assertEqual(agtbox.arch_pair("x86_64"), ("x64", "amd64", "x86_64"))

class ArchPair(unittest.TestCase):
    def test_x86_64_maps_all_arch_names(self):
        self.assertEqual(agtbox.arch_pair("x86_64"), ("x64", "amd64", "x86_64"))

    def test_aarch64_maps_all_arch_names(self):
        self.assertEqual(agtbox.arch_pair("aarch64"), ("arm64", "arm64", "aarch64"))

    def test_unsupported_arch_exits(self):
        with self.assertRaises(SystemExit):
            agtbox.arch_pair("sparc64")
```

- [ ] **Step 2: Run the targeted test to verify it fails**

Run: `python3 -m unittest test.test_agtbox.ArchPair -v`
Expected: FAIL because `arch_pair()` currently returns only two values and the new test class does not exist yet.

- [ ] **Step 3: Update the helper to return the UV asset arch token too**

```python
def arch_pair(machine):
    """Map a `uname -m` value to the release-tarball arch names used by
    node.js, gh/glab, and uv respectively."""
    if machine == "aarch64":
        return "arm64", "arm64", "aarch64"
    if machine == "x86_64":
        return "x64", "amd64", "x86_64"
    print(f"Error: unsupported architecture '{machine}'.", file=sys.stderr)
    sys.exit(1)
```

- [ ] **Step 4: Update call sites to accept the third return value without changing current node/gh/glab behavior**

```python
narch, goarch, uvarch = arch_pair(os.uname().machine)
```

```python
narch, goarch, uvarch = arch_pair(container_arch)
```

- [ ] **Step 5: Re-run the targeted test to verify it passes**

Run: `python3 -m unittest test.test_agtbox.Helpers.test_arch_pair test.test_agtbox.ArchPair -v`
Expected: PASS

### Task 2: Replace the UV bootstrap script with direct GitHub release download

**Files:**
- Modify: `bin/agtbox.py:300-358`
- Test: `test/test_agtbox.py`

**Interfaces:**
- Consumes: the extended `arch_pair()` output and the existing `AGT_*` env pattern
- Produces: a bash install-script stanza that installs `uv` from a GitHub release tarball into `${AGT_TOOLS}/bin`

- [ ] **Step 1: Write the failing tests that describe the new install script**

```python
class InstallScript(unittest.TestCase):
    def test_uv_install_uses_github_release_api(self):
        script = agtbox.install_script()
        self.assertIn("https://api.github.com/repos/astral-sh/uv/releases/latest", script)

    def test_uv_install_does_not_use_astral_script(self):
        script = agtbox.install_script()
        self.assertNotIn("https://astral.sh/uv/install.sh", script)

    def test_uv_install_uses_linux_gnu_tarball_pattern(self):
        script = agtbox.install_script()
        self.assertIn("uv-${AGT_UVARCH}-unknown-linux-gnu.tar.gz", script)

    def test_uvx_is_installed_with_uv(self):
        script = agtbox.install_script()
        self.assertIn('install -m755 "/tmp/uv-${AGT_UVARCH}-unknown-linux-gnu/uvx" "${AGT_TOOLS}/bin/uvx"', script)

    def test_uv_warning_stays_best_effort(self):
        script = agtbox.install_script()
        self.assertIn("WARNING -- uv install failed", script)
```

- [ ] **Step 2: Run the targeted tests to verify they fail**

Run: `python3 -m unittest test.test_agtbox.InstallScript -v`
Expected: FAIL because the script still references `astral.sh` and has no `AGT_UVARCH` support.

- [ ] **Step 3: Extend the AGT environment helper to pass UV arch into the install script**

```python
def _agt_env(narch, goarch, uvarch):
    return [
        ("AGT_TOOLS", AGENT_TOOLS),
        ("AGT_NARCH", narch),
        ("AGT_GOARCH", goarch),
        ("AGT_UVARCH", uvarch),
        ("AGT_NPM_PKGS", " ".join(NPM_PKGS)),
    ]
```

- [ ] **Step 4: Replace the UV install block in `install_script()`**

```bash
echo 'Agent Box: installing uv...' >&2
{ uvv=$(curl -fsSL https://api.github.com/repos/astral-sh/uv/releases/latest \
       | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])') \
  && curl -fsSL "https://github.com/astral-sh/uv/releases/download/${uvv}/uv-${AGT_UVARCH}-unknown-linux-gnu.tar.gz" \
       | tar -xz -C /tmp \
  && install -m755 "/tmp/uv-${AGT_UVARCH}-unknown-linux-gnu/uv" "${AGT_TOOLS}/bin/uv" \
  && install -m755 "/tmp/uv-${AGT_UVARCH}-unknown-linux-gnu/uvx" "${AGT_TOOLS}/bin/uvx"; } \
  || { echo 'Agent Box: WARNING -- uv install failed (github.com blocked?); skipping.' >&2; skipped="${skipped} uv"; }
```

- [ ] **Step 5: Update the install call sites to pass the new AGT env shape**

```python
narch, goarch, uvarch = arch_pair(os.uname().machine)
bw += _fmt_env("bwrap", _agt_env(narch, goarch, uvarch))
```

```python
narch, goarch, uvarch = arch_pair(container_arch)
pd += _fmt_env("podman", [_kv(e) for e in AGENT_ENV] + _agt_env(narch, goarch, uvarch))
```

Apply these updates at the concrete call sites in `bin/agtbox.py:421-423`, `bin/agtbox.py:437`, and `bin/agtbox.py:451`.

- [ ] **Step 6: Re-run the targeted tests to verify they pass**

Run: `python3 -m unittest test.test_agtbox.InstallScript -v`
Expected: PASS

- [ ] **Step 7: Run a focused install-path regression test**

Run: `python3 -m unittest test.test_agtbox.InstallEnv -v`
Expected: PASS

### Task 3: Update docs to match the new UV install path

**Files:**
- Modify: `README.md:17-18`
- Modify: `README.md:31`
- Modify: `README.md:107`
- Modify: `CLAUDE.md` (the install narrative that currently mentions `astral.sh / github.com / gitlab.com`)

**Interfaces:**
- Consumes: the final implementation from Tasks 1-2
- Produces: consistent human-facing documentation that says `uv` is fetched from GitHub releases instead of `astral.sh`

- [ ] **Step 1: Write the failing doc assertions as a simple grep check**

Run: `python3 -m unittest test.test_agtbox.InstallScript -v`
Expected: PASS from Task 2, while docs still contain stale references that must now be updated manually.

- [ ] **Step 2: Update `README.md` install descriptions**

```markdown
- Everything else — node, `uv`, and the agent CLIs — is installed automatically on first run into `~/.local/share/agent-box`.
```

```markdown
The first launch installs the toolchain (node + `uv` + the three CLIs + `gh`/`glab`) into `~/.local/share/agent-box`.
```

```markdown
- **Locked-down network?** The agent CLIs (node + npm) are required; `uv`, `gh`, and `glab` are auxiliary and installed best-effort. If their download hosts on GitHub or GitLab are blocked while the node/npm registries are allowed, the install warns and continues.
```

- [ ] **Step 3: Update `CLAUDE.md` architecture notes for the UV source**

```markdown
node is downloaded from nodejs.org, the agent CLIs come from npm, `uv` is downloaded from GitHub release assets, and `gh`/`glab` come from their latest GitHub/GitLab release tarballs.
```

- [ ] **Step 4: Run a targeted grep verification**

Run: `rg -n "astral\.sh/uv/install\.sh|uv install failed \(astral\.sh blocked\?\)" README.md CLAUDE.md bin/agtbox.py`
Expected: no matches

### Task 4: Run the full regression suite for the launcher

**Files:**
- Test: `test/test_agtbox.py`

**Interfaces:**
- Consumes: all prior implementation tasks
- Produces: verification that the launcher still constructs the correct bwrap/podman/install argv and docs no longer contradict behavior

- [ ] **Step 1: Run the full unit and integration suite**

Run: `python3 -m unittest discover -s test`
Expected: PASS

- [ ] **Step 2: Run one focused runtime smoke check if host Python supports the launcher**

Run: `AGTBOX_REINSTALL=1 ./bin/agtbox.py -t bwrap bash -- -lc 'command -v uv && uv --version'`
Expected: prints a `uv` path and version inside the sandbox

- [ ] **Step 3: If the smoke check cannot run because host Python is too old, capture that explicitly**

Run: `python3 --version`
Expected: if `< 3.9`, note that runtime smoke verification is blocked by the existing launcher Python floor and rely on the unittest suite plus targeted reproduction commands.

- [ ] **Step 4: Review the final diff before handing back**

Run: `git diff -- bin/agtbox.py test/test_agtbox.py README.md CLAUDE.md`
Expected: only the planned `uv` installer, mapping, tests, and docs changes appear

## Self-Review

- Spec coverage: the plan covers the installer change, arch handling, tests, and docs updates.
- Placeholder scan: no TODO/TBD placeholders remain.
- Type consistency: the plan consistently uses `arch_pair()` returning `(narch, goarch, uvarch)` and `_agt_env(narch, goarch, uvarch)`.

## Notes for This Session

- Do not perform any of the commit steps above during implementation unless the user explicitly asks for a commit.
- The current environment appears to have Python 3.6 on the host, so runtime smoke checks may remain blocked even after the code change.
