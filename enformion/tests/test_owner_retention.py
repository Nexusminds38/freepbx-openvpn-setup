"""Owner 23-hour retention and cleanup recovery tests.

The duration is the owner's explicit rule, not a caller-selected number.
Fixtures are fictional. Network is denied.
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import cleanup
import handoff
import retention
import run_lookup

POLICY = {"mode": "owner_first_collection_plus_23h"}
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
COLLECTED = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
FIRST_SEEN = datetime(2026, 11, 8, 3, 5, tzinfo=timezone.utc)
CAP = "2026-11-08T23:00:00+00:00"


def _run(directory, **overrides):
    values = dict(
        first="Casey",
        last="Exampleton",
        city="Exampleville",
        state="EX",
        age=40,
        county="Example County",
        source="https://example.org/fictional-roster",
        booking_reference="BK-FICTION-0001",
        observed_at=OBSERVED,
        first_seen_at=FIRST_SEEN,
        first_collected_at=COLLECTED,
        now=NOW,
        policy=POLICY,
        records_directory=directory,
        source_timestamp_raw="2026-04-01T08:00:00-04:00",
    )
    values.update(overrides)
    return run_lookup.prepare_offline(**values)


def _snapshot(root):
    found = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, root)] = handle.read()
    return found


class OwnerRetentionTests(unittest.TestCase):
    def setUp(self):
        self.network_calls = []
        self._patches = [
            patch.object(socket, "socket", self._deny),
            patch.object(socket, "create_connection", self._deny),
            patch.object(urllib.request, "urlopen", self._deny),
            patch.object(urllib.request, "Request", self._deny),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in self._patches:
            item.stop()
        self.assertEqual(self.network_calls, [])

    def _deny(self, *args, **kwargs):
        self.network_calls.append(repr(args[:1]))
        raise AssertionError("live network is denied")

    def test_deadline_is_first_collection_plus_23h_and_does_not_reset(self):
        self.assertEqual(retention.OWNER_RETENTION, timedelta(hours=23))
        with tempfile.TemporaryDirectory() as tmp:
            first = _run(tmp)
            self.assertTrue(first["receipt"]["persisted"])
            self.assertEqual(first["receipt"]["expires_at"], CAP)
            self.assertEqual(first["handoff"]["provenance"]["first_collected_at"], COLLECTED.isoformat())
            self.assertEqual(
                first["handoff"]["provenance"]["source_timestamp_raw"],
                "2026-04-01T08:00:00-04:00",
            )
            self.assertIsNone(first["handoff"]["provenance"]["source_timezone"])
            self.assertNotEqual(
                first["handoff"]["provenance"]["first_collected_at"],
                first["handoff"]["provenance"]["source_timestamp_raw"],
            )
            shorter = _run(
                tmp,
                booking_reference="BK-FICTION-0002",
                expires_at="2026-11-08T18:00:00+00:00",
            )
            self.assertTrue(shorter["receipt"]["persisted"])
            self.assertEqual(shorter["receipt"]["expires_at"], "2026-11-08T18:00:00+00:00")
            self.assertIsNone(first["handoff"]["production_destination"])
            before = _snapshot(tmp)
            later_sighting = _run(
                tmp,
                observed_at=datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc),
                first_seen_at=datetime(2026, 11, 8, 18, 5, tzinfo=timezone.utc),
                expires_at="2026-11-09T17:00:00+00:00",
            )
            self.assertFalse(later_sighting["receipt"]["persisted"])
            self.assertIn("23 hours", later_sighting["receipt"]["reason"])
            self.assertEqual(_snapshot(tmp), before)
            reset = _run(
                tmp,
                first_collected_at=COLLECTED + timedelta(hours=1),
            )
            self.assertFalse(reset["receipt"]["persisted"])
            self.assertIn("not reset", reset["receipt"]["reason"])
            self.assertEqual(_snapshot(tmp), before)
            combined = b"\n".join(before.values())
            self.assertIn(CAP.encode(), combined)
            self.assertNotIn(b"2026-11-09T17:00:00+00:00", combined)

    def test_bad_first_collection_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = _run(tmp, first_collected_at=None)
            self.assertFalse(missing["receipt"]["persisted"])
            self.assertIn("not inferred", missing["receipt"]["reason"])
            self.assertEqual(os.listdir(tmp), [])
            with self.assertRaises(handoff.HandoffError):
                _run(tmp, first_collected_at=datetime(2026, 11, 8, 0, 0))
            future = _run(
                tmp,
                first_collected_at=NOW + timedelta(hours=1),
                now=NOW,
            )
            self.assertFalse(future["receipt"]["persisted"])
            self.assertIn("future", future["receipt"]["reason"])
            self.assertFalse(retention.policy_is_owner_23h(
                {"mode": "owner_first_collection_plus_23h", "hours": 24}
            ))

    def test_cleanup_recovers_temp_and_refuses_a_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cleanup.mark_synthetic_store(tmp)
            log = os.path.join(tmp, "research-log.jsonl")
            with open(log, "w", encoding="utf-8") as handle:
                handle.write('{"episode_id":"keep","expires_at":"2026-12-01T00:00:00+00:00"}\n')
            leftover = log + ".cleanup-tmp"
            with open(leftover, "w", encoding="utf-8") as handle:
                handle.write("TEMP-SECRET\n")
            dry = cleanup.plan_cleanup(tmp, now=NOW, policy={"mode": "explicit_expires_at"})
            self.assertEqual(dry["counts"]["transaction_artifacts"], 1)
            self.assertEqual(dry["counts"]["removed_files"], 0)
            self.assertTrue(os.path.exists(leftover))
            self.assertNotIn("TEMP-SECRET", __import__("json").dumps(dry))
            applied = cleanup.apply_synthetic_cleanup(
                tmp,
                now=NOW,
                policy={"mode": "explicit_expires_at"},
                apply=True,
                confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertFalse(os.path.exists(leftover))
            self.assertNotIn("TEMP-SECRET", __import__("json").dumps(applied))
            with open(log, encoding="utf-8") as handle:
                self.assertIn("keep", handle.read())

        with tempfile.TemporaryDirectory() as tmp:
            cleanup.mark_synthetic_store(tmp)
            log = os.path.join(tmp, "research-log.jsonl")
            with open(log, "w", encoding="utf-8") as handle:
                handle.write('{"episode_id":"old","expires_at":"2026-11-01T00:00:00+00:00"}\n')
            real = cleanup._read_text_nofollow
            state = {"n": 0}

            def change_after_decision_read(path):
                text = real(path)
                if path == log:
                    state["n"] += 1
                    if state["n"] == 2:
                        with open(path, "a", encoding="utf-8") as handle:
                            handle.write(
                                '{"episode_id":"added-later","expires_at":"2026-12-01T00:00:00+00:00"}\n'
                            )
                return text

            with patch("cleanup._read_text_nofollow", change_after_decision_read):
                applied = cleanup.apply_synthetic_cleanup(
                    tmp,
                    now=NOW,
                    policy={"mode": "explicit_expires_at"},
                    apply=True,
                    confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
                )
            self.assertTrue(applied["partial_failure"])
            with open(log, encoding="utf-8") as handle:
                body = handle.read()
            self.assertIn("old", body)
            self.assertIn("added-later", body)
            self.assertFalse(os.path.exists(log + ".cleanup-tmp"))
