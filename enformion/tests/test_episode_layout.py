"""Per-episode files, crash recovery, and the synthetic mixed-log migration.

Fixtures are fictional. Sockets and HTTP are denied. The lock test checks
only writers that take the store lock. A writer that does not take it is
outside the guarantee.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.request
from datetime import datetime, timezone
from unittest.mock import patch

import audit
import cleanup
import publish
import retention

SOURCE = "https://example.org/fictional-roster"
POLICY = {"mode": "explicit_expires_at"}
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
CLOCK = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
EXPIRED = "expired-episode"
RETAINED = "retained-episode"


def _line(episode_id: str, expires_at: str) -> str:
    return json.dumps({"episode_id": episode_id, "expires_at": expires_at}, sort_keys=True) + "\n"


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


class EpisodeLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.network_calls: list = []
        self._patches = [
            patch.object(socket, "socket", self._deny_network),
            patch.object(socket, "create_connection", self._deny_network),
            patch.object(urllib.request, "urlopen", self._deny_network),
            patch.object(urllib.request, "Request", self._deny_network),
        ]
        for item in self._patches:
            item.start()
        publish.FAULT_AFTER = None
        cleanup.FAULT_AFTER = None

    def tearDown(self) -> None:
        publish.FAULT_AFTER = None
        cleanup.FAULT_AFTER = None
        for item in self._patches:
            item.stop()
        self.assertEqual(self.network_calls, [])

    def _deny_network(self, *args, **kwargs):
        self.network_calls.append(repr(args[:1]))
        raise AssertionError("live network is denied in offline tests")

    def _marker(self, root: str) -> None:
        cleanup.mark_synthetic_store(root)

    def _mixed(self, root: str) -> str:
        path = os.path.join(root, "research-log.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_line(EXPIRED, "2026-11-01T00:00:00+00:00"))
            handle.write(_line(RETAINED, "2026-12-01T00:00:00+00:00"))
        return path

    def test_new_logs_are_per_episode_and_cleanup_unlinks_only_the_expired_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._marker(tmp)
            first = audit.persist_bundle(
                tmp,
                {"episode": {"episode_id": EXPIRED}, "consistency": {"status": "candidate_consistent"}, "lane": "disconnected_mock"},
                datetime(2026, 11, 1, tzinfo=timezone.utc),
                now=NOW,
                policy=POLICY,
            )
            self.assertFalse(first["persisted"])
            later = audit.persist_bundle(
                tmp,
                {"episode": {"episode_id": RETAINED}, "consistency": {"status": "candidate_consistent"}, "lane": "disconnected_mock"},
                datetime(2026, 12, 1, tzinfo=timezone.utc),
                now=NOW,
                policy=POLICY,
            )
            self.assertTrue(later["persisted"])
            expired = audit.persist_bundle(
                tmp,
                {"episode": {"episode_id": EXPIRED}, "consistency": {"status": "candidate_consistent"}, "lane": "disconnected_mock"},
                datetime(2026, 11, 9, tzinfo=timezone.utc),
                now=NOW,
                policy=POLICY,
            )
            self.assertTrue(expired["persisted"])
            self.assertFalse(os.path.exists(os.path.join(tmp, "research-log.jsonl")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "query-log.jsonl")))
            expired_log = os.path.join(tmp, "episodes", retention.episode_slug(EXPIRED), "research.jsonl")
            retained_log = os.path.join(tmp, "episodes", retention.episode_slug(RETAINED), "research.jsonl")
            self.assertIn(EXPIRED, _read(expired_log).decode())
            self.assertIn(RETAINED, _read(retained_log).decode())
            applied = cleanup.apply_synthetic_cleanup(
                tmp, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertTrue(applied["applied"])
            self.assertFalse(applied["retention_compliance_claimed"])
            self.assertFalse(os.path.exists(expired_log))
            self.assertTrue(os.path.exists(retained_log))
            self.assertFalse(any(name.endswith(".replacing") for _dir, _names, files in os.walk(tmp) for name in files))

    def test_cleanup_keeps_a_mixed_log_until_an_authorized_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._marker(tmp)
            path = self._mixed(tmp)
            before = _read(path)
            dry = cleanup.migrate_mixed_logs(tmp, confirm="")
            self.assertFalse(dry["migration_complete"])
            self.assertEqual(_read(path), before)
            refused = cleanup.apply_synthetic_cleanup(
                tmp, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertEqual(_read(path), before)
            self.assertFalse(refused["retention_compliance_claimed"])
            self.assertIn(EXPIRED, _read(path).decode())
            migrated = cleanup.migrate_mixed_logs(tmp, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION)
            self.assertTrue(migrated["migration_complete"])
            self.assertFalse(migrated["retention_compliance_claimed"])
            self.assertFalse(os.path.exists(path))
            expired_copy = os.path.join(tmp, "episodes", retention.episode_slug(EXPIRED), "research.jsonl")
            retained_copy = os.path.join(tmp, "episodes", retention.episode_slug(RETAINED), "research.jsonl")
            self.assertEqual(_read(expired_copy), _line(EXPIRED, "2026-11-01T00:00:00+00:00").encode())
            self.assertEqual(_read(retained_copy), _line(RETAINED, "2026-12-01T00:00:00+00:00").encode())
            applied = cleanup.apply_synthetic_cleanup(
                tmp, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertFalse(os.path.exists(expired_copy))
            self.assertTrue(os.path.exists(retained_copy))
            self.assertFalse(applied["retention_compliance_claimed"])

    def test_migration_does_not_overwrite_a_different_episode_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._marker(tmp)
            slug = retention.episode_slug(EXPIRED)
            directory = os.path.join(tmp, "episodes", slug)
            os.makedirs(directory)
            current = _line(EXPIRED, "2026-11-09T00:00:00+00:00").encode()
            publish.create_complete_file(directory, "research.jsonl", current)
            self._mixed(tmp)
            report = cleanup.migrate_mixed_logs(tmp, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION)
            self.assertTrue(report["migration_complete"])
            self.assertEqual(_read(os.path.join(directory, "research.jsonl")), current)
            copied = _read(os.path.join(directory, "research.2.jsonl"))
            self.assertEqual(copied, _line(EXPIRED, "2026-11-01T00:00:00+00:00").encode())
            self.assertIn("research_log", retention.classify_artifact("episodes/expired-episode/research.2.jsonl"))
            again = cleanup.migrate_mixed_logs(tmp, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION)
            self.assertTrue(again["migration_complete"])
            self.assertFalse(os.path.exists(os.path.join(directory, "research.3.jsonl")))
            applied = cleanup.apply_synthetic_cleanup(
                tmp, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertFalse(os.path.exists(directory))
            retained = os.path.join(tmp, "episodes", retention.episode_slug(RETAINED), "research.jsonl")
            self.assertEqual(_read(retained), _line(RETAINED, "2026-12-01T00:00:00+00:00").encode())
            self.assertFalse(applied["retention_compliance_claimed"])

    def test_ownerless_line_blocks_migration_and_does_not_claim_compliance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._marker(tmp)
            path = os.path.join(tmp, "research-log.jsonl")
            body = _line(EXPIRED, "2026-11-01T00:00:00+00:00") + '{"note":"no-owner","expires_at":"2026-11-01T00:00:00+00:00"}\n'
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            report = cleanup.migrate_mixed_logs(tmp, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION)
            self.assertFalse(report["migration_complete"])
            self.assertFalse(report["retention_compliance_claimed"])
            self.assertEqual(_read(path).decode(), body)
            self.assertEqual(report["files_unlinked"], [])

    def test_kill_after_each_migration_step_leaves_complete_files_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._marker(tmp)
            path = self._mixed(tmp)
            original = _read(path)
            cleanup.FAULT_AFTER = "published:" + retention.episode_slug(EXPIRED)
            stopped = cleanup.migrate_mixed_logs(tmp, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION)
            self.assertFalse(stopped["migration_complete"])
            self.assertEqual(_read(path), original)
            published = os.path.join(tmp, "episodes", retention.episode_slug(EXPIRED), "research.jsonl")
            self.assertEqual(_read(published), _line(EXPIRED, "2026-11-01T00:00:00+00:00").encode())
            self.assertFalse(os.path.exists(os.path.join(tmp, "episodes", retention.episode_slug(RETAINED), "research.jsonl")))
            names = [name for _dir, _dirs, files in os.walk(tmp) for name in files]
            self.assertFalse(any(name.endswith(".replacing") or name.endswith(".cleanup-tmp") for name in names))
            cleanup.FAULT_AFTER = None
            again = cleanup.migrate_mixed_logs(tmp, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION)
            self.assertTrue(again["migration_complete"])
            self.assertFalse(os.path.exists(path))
            copies = [
                os.path.join(dirpath, name)
                for dirpath, _dirs, files in os.walk(tmp)
                for name in files
                if name.startswith("research")
            ]
            self.assertEqual(len(copies), 2)

    def test_kill_during_replace_keeps_the_earlier_instant_not_the_later_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = os.path.join(tmp, "episodes", "one")
            os.makedirs(directory)
            name = "research.jsonl"
            original = _line(EXPIRED, "2026-11-08T23:00:00+00:00").encode()
            publish.create_complete_file(directory, name, original)
            later_text = _line(EXPIRED, "2026-11-08T22:00:00-05:00")
            self.assertLess("2026-11-08T22:00:00-05:00", "2026-11-08T23:00:00+00:00")
            self.assertGreater(
                retention.try_aware_deadline("2026-11-08T22:00:00-05:00"),
                retention.try_aware_deadline("2026-11-08T23:00:00+00:00"),
            )
            publish.FAULT_AFTER = "staged:" + name
            with self.assertRaises(RuntimeError):
                publish.replace_exclusive_file(directory, name, later_text.encode(), original)
            self.assertEqual(_read(os.path.join(directory, name)), original)
            self.assertTrue(os.path.exists(os.path.join(directory, name + ".replacing")))
            publish.FAULT_AFTER = None
            self.assertEqual(publish.recover_staged_replacements(tmp), 1)
            self.assertEqual(_read(os.path.join(directory, name)), original)
            self.assertFalse(os.path.exists(os.path.join(directory, name + ".replacing")))

            earlier = _line(EXPIRED, "2026-11-08T18:00:00+00:00").encode()
            publish.create_complete_file(directory, name + ".replacing", earlier)
            self.assertEqual(publish.recover_staged_replacements(tmp), 1)
            self.assertEqual(_read(os.path.join(directory, name)), earlier)

    def test_shortening_refuses_to_rewrite_a_mixed_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "research-log.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_line(EXPIRED, "2026-11-20T00:00:00+00:00"))
                handle.write(_line(RETAINED, "2026-11-25T00:00:00+00:00"))
            before = _read(path)
            receipt = audit.persist_bundle(
                tmp,
                {"episode": {"episode_id": EXPIRED}, "consistency": {"status": "candidate_consistent"}, "lane": "disconnected_mock"},
                datetime(2026, 11, 15, tzinfo=timezone.utc),
                now=NOW,
                policy=POLICY,
            )
            self.assertFalse(receipt["persisted"])
            self.assertIn("shared", receipt["reason"])
            self.assertEqual(_read(path), before)

    def test_cooperating_writer_waits_for_the_directory_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._marker(tmp)
            path = self._mixed(tmp)
            code = textwrap.dedent(
                """
                import sys
                import cleanup
                report = cleanup.migrate_mixed_logs(sys.argv[1], confirm="synthetic-test-store")
                print("complete" if report["migration_complete"] else "blocked")
                """
            )
            with audit._store_lock(tmp):
                child = subprocess.Popen(
                    [sys.executable, "-c", code, tmp],
                    cwd=os.path.dirname(os.path.dirname(__file__)) or ".",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    child.wait(timeout=0.4)
                except subprocess.TimeoutExpired:
                    waiting = True
                else:
                    waiting = False
                self.assertTrue(waiting)
                self.assertTrue(os.path.exists(path))
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "complete")
            self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
