# Agent Box Synthetic Passwd/Group Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bwrap sandbox resolve the invoking LDAP-backed user and groups by binding launcher-generated passwd/group files into the sandbox instead of relying on live NSS/SSSD plumbing.

**Architecture:** Extend `bin/agtbox.py` to generate synthetic passwd and group files under agent-box state before bwrap launch, then overlay those files onto `/etc/passwd` and `/etc/group` after the existing read-only `/etc` bind. Keep the podman path unchanged and keep the implementation simple by only appending missing current-user and current-group entries based on name/group-name checks.

**Tech Stack:** Python 3 stdlib only, bubblewrap bind mounts, existing `agtbox.py` launcher, `unittest` suite in `test/test_agtbox.py`

## Global Constraints

- Change only the agent-box project under `/users/dejager/.opt/agent-box`.
- Do not expose live SSSD or NSS sockets into the sandbox.
- Keep the podman engine behavior unchanged.
- Keep the implementation simple: check for the current username and group names before appending, rather than handling impossible conflict cases.
- Synthetic identity files must be created under agent-box-managed state, not in arbitrary temp locations.
- The bwrap sandbox must continue binding host `/etc` read-only, with synthetic passwd/group files overlaid afterward.
- No commits or pushes.

---

### Task 1: Add synthetic passwd/group generation

**Files:**
- Modify: `bin/agtbox.py`
- Test: `test/test_agtbox.py`

**Interfaces:**
- Consumes: existing `HOME`, `AGENT_STATE`, and bwrap launch preparation flow
- Produces: helper(s) that materialize sandbox passwd/group files and return their paths

- [ ] **Step 1: Write a failing test for passwd/group synthesis**

Add a unit test in `test/test_agtbox.py` that exercises a new helper using temporary host passwd/group source files and verifies:
- the original contents are copied through
- a missing current user is appended once to passwd
- missing current group names are appended once to group
- already-present username/group names are not duplicated

- [ ] **Step 2: Run the targeted test to verify it fails**

Run:

```bash
python3 -m unittest test.test_agtbox.TestSyntheticIdentityFiles -v
```

Expected: FAIL because the helper does not exist yet.

- [ ] **Step 3: Implement the minimal helper code in `bin/agtbox.py`**

Add stdlib-only helpers to:
- determine the runtime-state destination paths under `AGENT_STATE`
- copy `/etc/passwd` and `/etc/group`
- append the current username if not already present in passwd
- append the current user's groups if their names are not already present in group

Use simple name-based presence checks as agreed.

- [ ] **Step 4: Re-run the targeted test to verify it passes**

Run:

```bash
python3 -m unittest test.test_agtbox.TestSyntheticIdentityFiles -v
```

Expected: PASS.

### Task 2: Wire synthetic files into the bwrap path

**Files:**
- Modify: `bin/agtbox.py`
- Test: `test/test_agtbox.py`

**Interfaces:**
- Consumes: synthetic passwd/group helper paths from Task 1 and existing `bwrap_common()` output
- Produces: bwrap argv that overlays generated passwd/group files onto `/etc/passwd` and `/etc/group`

- [ ] **Step 1: Write a failing test for the bwrap argv**

Add a unit test that verifies `bwrap_common()` or the relevant launcher path includes read-only binds for the generated synthetic passwd/group files onto `/etc/passwd` and `/etc/group`.

- [ ] **Step 2: Run the targeted test to verify it fails**

Run:

```bash
python3 -m unittest test.test_agtbox.TestBwrapIdentityOverlay -v
```

Expected: FAIL because the overlay bind args are not present yet.

- [ ] **Step 3: Implement the minimal bwrap integration**

Update `bin/agtbox.py` so that:
- synthetic files are created during the existing prelaunch preparation flow
- `bwrap_common()` adds `--ro-bind` or equivalent overlay binds for those files onto `/etc/passwd` and `/etc/group`
- the podman path remains unchanged

- [ ] **Step 4: Re-run the targeted test to verify it passes**

Run:

```bash
python3 -m unittest test.test_agtbox.TestBwrapIdentityOverlay -v
```

Expected: PASS.

### Task 3: Verify the launcher behavior

**Files:**
- Modify: none
- Test: `test/test_agtbox.py`, `bin/agtbox.py`

**Interfaces:**
- Consumes: updated helper and bwrap integration
- Produces: evidence that the launcher stays valid and the synthetic identity behavior is wired in correctly

- [ ] **Step 1: Run the full unit test suite**

Run:

```bash
python3 -m unittest discover -s test -v
```

Expected: PASS.

- [ ] **Step 2: Run a syntax check on the launcher**

Run:

```bash
python3 -m py_compile bin/agtbox.py
```

Expected: PASS with no output.

- [ ] **Step 3: Inspect the diff and status**

Run:

```bash
git diff -- bin/agtbox.py test/test_agtbox.py
git status --short
```

Expected: only the intended launcher and test changes are present, plus any plan doc created for this work.
