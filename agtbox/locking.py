"""Filesystem detection and shared launch ownership; no host-local PID checks."""
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from contextlib import contextmanager


def filesystem_type(path):
    """Return the backing filesystem type, or None when detection fails.

    The caller creates the data directory first, including triggering automounts.
    stat follows its backing mount on Linux; macOS mount lists filesystem types
    (BSD stat's format options differ). Resolve symlinks before matching mounts.
    """
    path = os.path.realpath(path)
    if sys.platform.startswith("linux"):
        command = ["stat", "-f", "-c", "%T", "--", path]
    elif sys.platform == "darwin":
        command = ["/sbin/mount"]
    else:
        return None
    try:
        output = subprocess.run(command, check=True, capture_output=True,
                                text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if sys.platform.startswith("linux"):
        output = output.lower()
        return output if output and not output.startswith("unknown") else None

    best, fstype = -1, None
    for line in output.splitlines():
        match = re.match(r"^.+? on (/.*) \(([^,)]+)[,)]", line)
        if not match:
            continue
        mount, kind = match.groups()
        if (path == mount or path.startswith(mount.rstrip("/") + "/")) and len(mount) > best:
            best, fstype = len(mount), kind
    return fstype


@contextmanager
def launch_lock(path, project):
    """An exclusive-create lock file, usable on NFSv3+ as well as local disks.

    Never guess that another host is dead. A crashed launcher leaves a lock for
    manual recovery. Keep the inode open so cleanup can recognize a replacement
    lock after somebody explicitly removes ours.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            owner = json.loads(path.read_text())
            details = (f"host {owner['host']}, launcher PID {owner['pid']}, "
                       f"since {owner['started']}, project {owner['project']}")
        except (OSError, ValueError, KeyError, TypeError):
            details = "owner information unavailable"
        raise SystemExit(
            "Agent Box: OpenCode is already locked by another launch.\n"
            f"Owner: {details}\nLock file: {path}\n"
            "If the original run has stopped or its node is down, remove the lock "
            "and retry:\n"
            f"  rm -- {shlex.quote(str(path))}") from None

    with os.fdopen(fd, "w") as owner:
        try:
            json.dump({"host": socket.gethostname(), "pid": os.getpid(),
                       "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "project": project}, owner)
            owner.write("\n")
            owner.flush()
            yield
        finally:
            try:
                current = path.stat()
                held = os.fstat(owner.fileno())
                if (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass
