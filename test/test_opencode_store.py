import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agtbox import core
from agtbox import opencode_store as store


def cred(cid, integration="openai", active=False, key="sk", **extra):
    return {"id": cid, "integrationID": integration, "label": "API key",
            "active": active, "value": {"type": "key", "key": key}, **extra}


class ProjectId(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_basename_is_sanitized_and_hash_is_sha256_of_realpath(self):
        project = self.root / "my proj!"
        project.mkdir()
        digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:12]
        self.assertEqual(store.project_id(str(project)), f"my_proj_-{digest}")

    def test_filesystem_root(self):
        self.assertRegex(store.project_id("/"), r"^root-[0-9a-f]{12}$")

    def test_symlink_resolves_to_the_same_id(self):
        project = self.root / "project"
        project.mkdir()
        link = self.root / "link"
        link.symlink_to(project, target_is_directory=True)
        self.assertEqual(store.project_id(str(link)), store.project_id(str(project)))

    def test_same_basename_in_different_dirs_differs(self):
        a, b = self.root / "a" / "app", self.root / "b" / "app"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        self.assertNotEqual(store.project_id(str(a)), store.project_id(str(b)))
        self.assertTrue(store.project_id(str(a)).startswith("app-"))

    def test_id_is_stable(self):
        self.assertEqual(store.project_id(str(self.root)), store.project_id(str(self.root)))


class ParseCredentials(unittest.TestCase):
    def test_accepts_an_array_of_credentials(self):
        self.assertEqual(store.parse_credentials(json.dumps([cred("a")])), [cred("a")])
        self.assertEqual(store.parse_credentials("[]"), [])

    def test_rejects_malformed_input(self):
        for text in ("not json", '{"id": "a"}', '[1]', '[{"integrationID": "openai"}]',
                     '[{"id": 5, "integrationID": "openai"}]', '[{"id": "a"}]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                store.parse_credentials(text)


class MasterFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patch = mock.patch.object(core, "AGENT_TOOLS", str(self.home / "agent-box" / "x86_64"))
        patch.start()
        self.addCleanup(patch.stop)
        self.dir = self.home / "agent-box" / "opencode"

    def test_paths_are_outside_the_arch_toolchain(self):
        self.assertEqual(store.master_dir(), self.dir)
        self.assertEqual(store.master_path(), self.dir / "credentials.json")
        self.assertEqual(store.master_lock_path(), self.dir / "credentials.lock")

    def test_absent_master_reads_as_none(self):
        self.assertIsNone(store.read_master())

    def test_corrupt_master_raises(self):
        self.dir.mkdir(parents=True)
        store.master_path().write_text("garbage")
        with self.assertRaises(ValueError):
            store.read_master()

    def test_write_round_trips_with_private_modes(self):
        self.dir.mkdir(parents=True, mode=0o755)
        self.dir.chmod(0o755)
        store.write_master([cred("a", active=True)])
        self.assertEqual(store.read_master(), [cred("a", active=True)])
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(store.master_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["credentials.json"])

    def test_failed_write_keeps_previous_master_and_no_temp_file(self):
        store.write_master([cred("a")])
        with mock.patch("agtbox.opencode_store.os.replace", side_effect=OSError("disk full")), \
             self.assertRaisesRegex(OSError, "disk full"):
            store.write_master([cred("b")])
        self.assertEqual(store.read_master(), [cred("a")])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["credentials.json"])


class Merge(unittest.TestCase):
    def test_new_credentials_are_inserted_inactive(self):
        merged = store.merge_credentials([], [cred("a")])
        self.assertEqual(merged, [cred("a", active=False)])

    def test_replace_updates_value_but_not_active(self):
        master = [cred("a", active=True, key="old")]
        merged = store.merge_credentials(master, [cred("a", active=False, key="new")])
        self.assertEqual(merged, [cred("a", active=True, key="new")])

    def test_master_only_credentials_are_kept(self):
        master = [cred("a"), cred("b", "anthropic")]
        self.assertEqual(store.merge_credentials(master, [cred("a", key="new")]),
                         [cred("a", key="new"), cred("b", "anthropic")])

    def test_first_active_export_credential_wins_per_integration(self):
        master = [cred("a", active=True), cred("x", "anthropic", active=True)]
        exported = [cred("b", active=True), cred("c", active=True)]
        merged = store.merge_credentials(master, exported)
        self.assertEqual({c["id"]: c["active"] for c in merged},
                         {"a": False, "x": True, "b": True, "c": False})

    def test_integration_without_active_export_keeps_master_flags(self):
        master = [cred("a", active=True)]
        merged = store.merge_credentials(master, [cred("b", active=False)])
        self.assertEqual({c["id"]: c["active"] for c in merged}, {"a": True, "b": False})

    def test_empty_export_is_a_no_op(self):
        master = [cred("a", active=True)]
        self.assertEqual(store.merge_credentials(master, []), master)

    def test_unknown_fields_round_trip(self):
        oauth = {"id": "o", "integrationID": "openai", "label": "ChatGPT", "active": True,
                 "value": {"type": "oauth", "refresh": "r", "access": "t", "expires": 1},
                 "metadata": {"plan": "plus"}}
        merged = store.merge_credentials([], [oauth])
        self.assertEqual(merged, [oauth])

    def test_inputs_are_not_mutated(self):
        master, exported = [cred("a", active=True)], [cred("a", key="new")]
        snapshot = json.dumps([master, exported])
        store.merge_credentials(master, exported)
        self.assertEqual(json.dumps([master, exported]), snapshot)


if __name__ == "__main__":
    unittest.main()
