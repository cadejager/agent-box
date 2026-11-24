"""OpenCode per-project storage: project IDs and the master credential file.

The master lives beside the per-arch toolchain dirs (one login serves every
arch) and outside every sandbox bind (agents cannot read or change it).
"""
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from agtbox import core


def project_id(app_dir):
    """`<basename>-<12 hex of sha256(realpath)>`: readable, unique per path."""
    real = os.path.realpath(app_dir)
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(real)) or "root"
    return f"{base}-{hashlib.sha256(os.fsencode(real)).hexdigest()[:12]}"


def master_dir():
    return Path(core.AGENT_TOOLS).parent / "opencode"


def master_path():
    return master_dir() / "credentials.json"


def master_lock_path():
    return master_dir() / "credentials.lock"


def ensure_master_dir():
    """Create the master dir and make it private even if it already existed
    (mkdir honours umask and leaves an existing dir's mode alone)."""
    path = master_dir()
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def parse_credentials(text):
    """Parse `opencode auth export` output; ValueError unless it is a list of
    objects with string `id` and `integrationID`."""
    data = json.loads(text)
    if not isinstance(data, list) or not all(
            isinstance(c, dict) and isinstance(c.get("id"), str)
            and isinstance(c.get("integrationID"), str) for c in data):
        raise ValueError("expected a JSON array of credentials with string "
                         "id and integrationID")
    return data


def read_master():
    """The master credentials, or None when there is no master yet."""
    try:
        text = master_path().read_text()
    except FileNotFoundError:
        return None
    return parse_credentials(text)


def write_master(credentials):
    """Replace the master atomically: a private temp file, fsync, then rename."""
    directory = ensure_master_dir()
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".credentials-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(credentials, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, master_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def merge_credentials(master, exported):
    """Fold one project's export into the master (see the design spec).

    By `id`: insert new credentials inactive; replace existing ones' fields but
    keep their `active`. Then, per integration that has an active credential in
    the export, the first such credential becomes the only active one. Master
    credentials missing from the export are kept (logouts don't propagate).
    """
    merged = copy.deepcopy(master)
    index = {c["id"]: i for i, c in enumerate(merged)}
    for cred in exported:
        if cred["id"] in index:
            entry = merged[index[cred["id"]]]
            active = entry.get("active", False)
            entry.clear()
            entry.update(copy.deepcopy(cred))
            entry["active"] = active
        else:
            index[cred["id"]] = len(merged)
            merged.append({**copy.deepcopy(cred), "active": False})
    chosen = {}
    for cred in exported:
        if cred.get("active") is True:
            chosen.setdefault(cred["integrationID"], cred["id"])
    for entry in merged:
        if entry["integrationID"] in chosen:
            entry["active"] = entry["id"] == chosen[entry["integrationID"]]
    return merged
