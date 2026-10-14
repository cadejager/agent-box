# OpenCode NFS Lock Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Address the PR #44 review: no double-delivered terminal signals, the lock lives beside the database it protects, and more network filesystems trigger the lock.

**Architecture:** Three independent edits on branch `opencode-locking`: `Sandbox.wait` (signal handling), `Opencode.run` (lock path + network type set), and the docs/tests that name those.

**Tech Stack:** Python 3 stdlib, `unittest`.

**Spec:** PR #44 review discussion (this session); no separate spec file.

**Follow-up correction:** The SIGHUP handling proposed in Task 1 below is superseded.
Forward SIGHUP: terminal hangup can signal only the launcher as session leader.
Keep the SIGINT no-op to avoid double-delivering Ctrl-C. A real PTY hangup test
now checks child termination and lock cleanup.

## Global Constraints

- Stdlib only. Run tests with `python3 -m unittest discover -s test`; all must pass after every task.
- Do not push; the user pushes.
- Filesystem detection stays type-name based on both Linux and macOS (no macOS `local`-flag path).
- No stale-lock auto-detection and no per-project locking (explicitly out of scope).

## Review Focus

- Ctrl-C in a cooked terminal reaches the sandbox exactly once, and the agent still dies on it (the child must not inherit an ignored SIGINT).
- SIGTERM sent only to the launcher is still forwarded and the lock is still released.
- The lock file is not inside any directory bound into the sandbox.
- `stat -f -c %T` names for Lustre/GPFS/CIFS are matched exactly (lowercased).
- Known local types (`ext2/ext3`, `xfs`, `apfs`, `tmpfs`) still take the unlocked exec path.

---

### Task 1: Don't re-send terminal signals from `Sandbox.wait`

**Files:**
- Modify: `agtbox/sandboxes/base.py` (`Sandbox.wait`)
- Test: `test/test_sandboxes.py` (class `Wait`), `test/test_opencode.py` (class `LaunchLock`)

**Interfaces:**
- Produces: `Sandbox.wait(ctx) -> int` (unchanged signature/return).

Behavior: while waiting, SIGINT and SIGHUP get a **no-op Python handler** in the launcher (the terminal already delivers them to the child's process group). SIGTERM is still forwarded via `child.send_signal` (queued in `pending` if it arrives before `Popen` returns). Use a Python function, **not** `signal.SIG_IGN`: ignored dispositions are inherited across `exec`, so `SIG_IGN` would make bwrap/podman/opencode immune to Ctrl-C; Python handlers reset to default on `exec`. Handlers are restored in `finally` as today.

- [ ] **Step 1: Write the failing tests**

In `Wait`, add `test_terminal_signals_are_not_forwarded`: same mocking pattern as `test_forwards_signals_and_restores_handlers_after_child_exits`, but `wait()` invokes `handlers[signal.SIGINT](signal.SIGINT, None)` and `handlers[signal.SIGHUP](signal.SIGHUP, None)` and returns `0`. Assert `child.send_signal.assert_not_called()`, and that the handlers installed for SIGINT/SIGHUP during the wait were callables, not `signal.SIG_IGN`. Keep the existing SIGTERM test unchanged (it must still pass).

In `LaunchLock`, add `test_ctrl_c_reaches_sandbox_once`: same shape as `test_launcher_forwards_termination_waits_and_cleans_up`, but
- launch the launcher with `start_new_session=True`;
- the worker counts SIGINTs (`signal.signal(SIGINT, handler)` incrementing a counter), prints `ready`, then after the first SIGINT sleeps 0.5 s and `sys.exit(count)`;
- after reading `ready`, send `os.killpg(launcher.pid, signal.SIGINT)` (simulates the terminal);
- assert `launcher.returncode == 1` and the lock file is gone.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest test.test_sandboxes.Wait test.test_opencode.LaunchLock -v`
Expected: both new tests FAIL (send_signal called / exit code 2).

- [ ] **Step 3: Implement in `agtbox/sandboxes/base.py`**

Install `forward` only for `SIGTERM`; install a no-op function for `SIGINT`, `SIGHUP`. Shorten the docstring to one line.

- [ ] **Step 4: Run full suite**

Run: `python3 -m unittest discover -s test`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add agtbox/sandboxes/base.py test/test_sandboxes.py test/test_opencode.py
git commit -m "opencode lock: don't re-send terminal SIGINT/SIGHUP to the sandbox"
```

---

### Task 2: Keep the lock beside the OpenCode database

**Files:**
- Modify: `agtbox/agents/opencode.py` (`Opencode.run`)
- Modify: `README.md`, `CLAUDE.md`, `agtbox/agents/README.md` (lock path mentions)
- Test: `test/test_opencode.py` (`SharedStorage.setUp`), `test/test_agtbox.py` (two `test_opencode_*` tests)

**Interfaces:**
- Produces: lock path `Path(core.AGENT_TOOLS) / "opencode.launch.lock"` — a sibling of the data dir `${AGENT_TOOLS}/opencode`, so it is on the same filesystem but **not** inside the sandbox-bound directory (the agent can't see or delete it).

- [ ] **Step 1: Update tests to the new path**

- `test_opencode.py` `SharedStorage.setUp`: `self.lock = self.home / "tools/opencode.launch.lock"`.
- `test_agtbox.py` `test_opencode_shared_storage_in_both_sandboxes`: assert `(self.tools / "opencode.launch.lock")` does not exist (replace the `state / "launch.lock"` check).
- `test_agtbox.py` `test_opencode_nfs_lock_blocks_another_project_before_setup`: `lock = self.tools / "opencode.launch.lock"`.
- Add to `SharedStorage`: `test_lock_is_not_inside_a_sandbox_bind` — for every `bind` in `Opencode().binds`, assert `self.lock` is not under `Path(bind.src)` (use `Path.is_relative_to`).

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest test.test_opencode test.test_agtbox -v`
Expected: FAIL on the lock-path assertions.

- [ ] **Step 3: Implement**

In `Opencode.run`, set `lock = Path(core.AGENT_TOOLS) / "opencode.launch.lock"`. Update the path in `README.md` (code block `~/.local/share/agent-box/<arch>/opencode.launch.lock`, note it is per-architecture like the database) and `CLAUDE.md` (replace `${AGENT_STATE}/opencode/launch.lock`; the lock now scopes to the same arch-specific data dir, not the shared state dir).

- [ ] **Step 4: Run full suite**

Run: `python3 -m unittest discover -s test`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add agtbox/agents/opencode.py README.md CLAUDE.md agtbox/agents/README.md test/
git commit -m "opencode lock: store the lock beside the database it protects"
```

---

### Task 3: Treat more filesystems as networked

**Files:**
- Modify: `agtbox/agents/opencode.py` (module constant + `Opencode.run`)
- Modify: `README.md`, `CLAUDE.md` (replace "NFS/NFS4" wording with "network filesystem (NFS, Lustre, GPFS, CIFS/SMB, Ceph)")
- Test: `test/test_opencode.py` (`SharedStorage`)

**Interfaces:**
- Produces: `NETWORK_FSTYPES: frozenset[str]` in `agtbox/agents/opencode.py`, values exactly:
  Linux (`stat -f -c %T` names): `nfs`, `nfs4`, `lustre`, `gpfs`, `cifs`, `smb2`, `smb3`, `smbfs`, `ceph`;
  macOS (`/sbin/mount` names): `afpfs`, `webdav` (`nfs`, `smbfs` already present).
  `Opencode.run` locks when `fstype is None or fstype in NETWORK_FSTYPES`.

- [ ] **Step 1: Write the failing test**

Extend `test_nfs_holds_global_lock_through_setup_and_exit` to loop over `sorted(NETWORK_FSTYPES) + [None]` instead of `("nfs", "nfs4", None)`. Extend `test_local_storage_uses_exec_without_lock` to loop over `("ext2/ext3", "xfs", "apfs", "tmpfs")` (fresh `mock.Mock()` sandbox per subtest).

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest test.test_opencode.SharedStorage -v`
Expected: ImportError / FAIL for `lustre`, `gpfs`, etc.

- [ ] **Step 3: Implement** the constant and the membership check; update the stderr/docs wording so they no longer say "NFS" only.

- [ ] **Step 4: Run full suite**

Run: `python3 -m unittest discover -s test`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add agtbox/agents/opencode.py README.md CLAUDE.md test/test_opencode.py
git commit -m "opencode lock: lock on Lustre, GPFS, CIFS/SMB and Ceph too"
```

---

**Manual follow-up (user):** on the cluster, run `stat -f -c %T ~/.local/share/agent-box/$(uname -m)/opencode` and confirm the printed name is in `NETWORK_FSTYPES`.
