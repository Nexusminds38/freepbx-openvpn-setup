"""Synthetic cleanup and offline handoff smoke tests.

Sockets and HTTP are denied. Fixtures are fictional. Reports are checked
for counts only, not for names or contact values.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import cleanup
import client
import retention
import run_lookup

SOURCE = "https://example.org/fictional-roster"
POLICY = {"mode": "explicit_expires_at"}
STORE_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
FIRST_SEEN = datetime(2026, 11, 8, 3, 5, tzinfo=timezone.utc)
CLOCK = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)


def _prepare(directory: str, booking: str, expires_at: str):
    return run_lookup.prepare_offline(
        first="Casey",
        last="Exampleton",
        city="Exampleville",
        state="EX",
        age=40,
        county="Example County",
        source=SOURCE,
        booking_reference=booking,
        observed_at=OBSERVED,
        first_seen_at=FIRST_SEEN,
        now=STORE_NOW,
        policy=POLICY,
        expires_at=expires_at,
        records_directory=directory,
    )


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _snapshot(root: str) -> dict[str, bytes]:
    found = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, root)] = handle.read()
    return found


class SyntheticCleanupTests(unittest.TestCase):
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
        for key in ("ENFORMION_RECORDS_DIR", "ENFORMION_KEYS_FILE", "ENFORMION_RETENTION_POLICY"):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for item in self._patches:
            item.stop()
        self.assertEqual(self.network_calls, [])

    def _deny_network(self, *args, **kwargs):
        self.network_calls.append(repr(args[:1]))
        raise AssertionError("live network is denied in offline tests")

    def test_smoke_handoff_then_explicit_synthetic_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cleanup.mark_synthetic_store(tmp)
            expired = _prepare(tmp, "BK-FICTION-0001", "2026-11-10T00:00:00+00:00")
            retained = _prepare(tmp, "BK-FICTION-0002", "2026-11-20T15:00:00-05:00")
            self.assertEqual(expired["consistency"]["status"], "candidate_consistent")
            self.assertFalse(expired["consistency"]["identity_proof"])
            self.assertFalse(expired["consistency"]["verified_export"])
            self.assertIsNone(expired["handoff"]["production_destination"])
            self.assertEqual(expired["receipt"]["live_linkage"], "blocked")
            self.assertTrue(expired["receipt"]["persisted"])
            self.assertTrue(retained["receipt"]["persisted"])
            with self.assertRaises(client.LiveLookupDisabled):
                client.get_adapter(mock=False)

            expired_id = expired["handoff"]["episode"]["episode_id"]
            retained_id = retained["handoff"]["episode"]["episode_id"]
            self.assertNotEqual(expired_id, retained_id)
            slug = retention.episode_slug(expired_id)
            _write(
                os.path.join(tmp, "backups", "research-log.jsonl.bak"),
                json.dumps({"episode_id": expired_id, "expires_at": "2026-11-10T00:00:00+00:00"}) + "\n"
                + json.dumps({"episode_id": retained_id, "expires_at": "2026-11-20T15:00:00-05:00"}) + "\n",
            )
            _write(os.path.join(tmp, "results", slug + ".sha256"), "synthetic-sidecar\n")
            offset_id = "county=example county|source=https://example.org/fictional-roster|booking=bk-fiction-offset"
            _write(
                os.path.join(tmp, "results", retention.episode_slug(offset_id) + ".json"),
                json.dumps({"episode_id": offset_id, "expires_at": "2026-11-09T19:00:00-05:00"}) + "\n",
            )
            hold_id = "county=example county|source=https://example.org/fictional-roster|booking=bk-fiction-hold"
            _write(
                os.path.join(tmp, "results", retention.episode_slug(hold_id) + ".json"),
                json.dumps({"episode_id": hold_id, "expires_at": "2026-11-10T00:00:01+00:00"}) + "\n",
            )
            naive_id = "county=example county|source=https://example.org/fictional-roster|booking=bk-fiction-naive"
            naive_path = os.path.join(tmp, "results", retention.episode_slug(naive_id) + ".json")
            _write(naive_path, json.dumps({"episode_id": naive_id, "expires_at": "2026-11-01T00:00:00"}) + "\n")
            with open(os.path.join(tmp, "research-log.jsonl"), "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"note": "orphan-line", "expires_at": "2026-11-01T00:00:00+00:00"}) + "\n")
            before = _snapshot(tmp)
            self.assertGreater(len(before), 0)
            dry = cleanup.apply_synthetic_cleanup(tmp, now=CLOCK, policy=POLICY, apply=False)
            self.assertFalse(dry["applied"])
            self.assertEqual(_snapshot(tmp), before)
            self.assertGreater(dry["counts"]["expired_episodes"], 0)
            self.assertGreater(dry["counts"]["retained_episodes"], 0)
            self.assertGreater(dry["counts"]["malformed_deadline_records"], 0)
            self.assertGreater(dry["counts"]["ambiguous_records"], 0)
            self.assertFalse(dry["retention_compliance_claimed"])
            self.assertFalse(dry["production_deletion_proof"])
            self.assertIsNone(dry["production_destination"])
            self.assertEqual(dry["live_linkage"], "blocked")
            report_text = json.dumps(dry)
            self.assertNotIn("Exampleton", report_text)
            self.assertNotIn("Casey", report_text)
            self.assertNotIn("synthetic-sidecar", report_text)

            applied = cleanup.apply_synthetic_cleanup(
                tmp, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertTrue(applied["applied"])
            self.assertFalse(applied["partial_failure"])
            self.assertGreater(applied["counts"]["removed_files"], 0)
            self.assertEqual(applied["counts"]["removed_log_lines"], 0)
            self.assertEqual(applied["counts"]["removed_sidecars"], 0)
            self.assertGreater(applied["counts"]["mixed_files"], 0)
            self.assertLess(applied["counts"]["files_after"], applied["counts"]["files_before"])
            after = _snapshot(tmp)
            self.assertIn("results/" + slug + ".sha256", after)
            self.assertFalse(any(name.endswith(".cleanup-tmp") for name in after))
            self.assertIn("synthetic-store.json", after)
            self.assertIn(os.path.relpath(naive_path, tmp), after)
            combined = "\n".join(text.decode() for text in after.values())
            self.assertIn(retained_id, combined)
            self.assertIn(hold_id, combined)
            self.assertIn("orphan-line", combined)
            self.assertIn(expired_id, combined)
            self.assertNotIn(offset_id, combined)
            self.assertIn("synthetic-sidecar", combined)
            self.assertFalse(applied["retention_compliance_claimed"])
            self.assertIn("Roster-watch", " ".join(applied["out_of_scope_stores"]))

    def test_cleanup_refuses_without_marker_policy_or_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _prepare(tmp, "BK-FICTION-0001", "2026-11-10T00:00:00+00:00")
            before = _snapshot(tmp)
            unmarked = cleanup.apply_synthetic_cleanup(
                tmp, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertFalse(unmarked["applied"])
            self.assertEqual(_snapshot(tmp), before)
            with self.assertRaises(cleanup.CleanupError):
                cleanup.mark_synthetic_store(tmp)
            self.assertNotIn("synthetic-store.json", _snapshot(tmp))
        with tempfile.TemporaryDirectory() as empty:
            cleanup.mark_synthetic_store(empty)
            _write(os.path.join(empty, "results", "old.json"), '{"episode_id":"old","expires_at":"2026-11-01T00:00:00+00:00"}\n')
            marked = _snapshot(empty)
            missing_confirm = cleanup.apply_synthetic_cleanup(empty, now=CLOCK, policy=POLICY, apply=True)
            self.assertFalse(missing_confirm["applied"])
            self.assertEqual(_snapshot(empty), marked)
            missing_policy = cleanup.apply_synthetic_cleanup(
                empty, now=CLOCK, policy=None, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertFalse(missing_policy["applied"])
            self.assertEqual(missing_policy["live_linkage"], "blocked")
            self.assertEqual(_snapshot(empty), marked)
            duration = cleanup.apply_synthetic_cleanup(
                empty,
                now=CLOCK,
                policy={"mode": "explicit_expires_at", "duration": "unverified"},
                apply=True,
                confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertFalse(duration["applied"])
            self.assertEqual(_snapshot(empty), marked)

    def test_symlink_and_ambiguous_episode_block_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as root:
            outside_file = os.path.join(outside, "record.json")
            _write(outside_file, '{"episode_id":"outside","expires_at":"2026-11-01T00:00:00+00:00"}\n')
            cleanup.mark_synthetic_store(root)
            os.symlink(outside_file, os.path.join(root, "escape.json"))
            _write(
                os.path.join(root, "results", "local.json"),
                '{"episode_id":"local","expires_at":"2026-11-01T00:00:00+00:00"}\n',
            )
            before = _snapshot(root)
            applied = cleanup.apply_synthetic_cleanup(
                root, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertFalse(applied["applied"])
            self.assertTrue(applied["containment_blockers"])
            self.assertEqual(_snapshot(root), before)
            with open(outside_file, encoding="utf-8") as handle:
                self.assertIn("outside", handle.read())

        with tempfile.TemporaryDirectory() as tmp:
            cleanup.mark_synthetic_store(tmp)
            mixed = os.path.join(tmp, "research-log.jsonl")
            _write(
                mixed,
                "\n".join([
                    '{"episode_id":"expired-clear","expires_at":"2026-11-01T00:00:00+00:00"}',
                    '{"episode_id":"still-held","expires_at":"2026-12-01T00:00:00+00:00"}',
                    '{"expires_at":"2026-11-01T00:00:00+00:00","note":"no-owner"}',
                    '{"episode_id":"expired-clear","expires_at":"not-a-deadline"}',
                ]) + "\n",
            )
            applied = cleanup.apply_synthetic_cleanup(
                tmp, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertEqual(applied["counts"]["removed_files"], 0)
            self.assertEqual(applied["counts"]["removed_log_lines"], 0)
            self.assertGreater(applied["counts"]["malformed_deadline_records"], 0)
            self.assertGreater(applied["counts"]["blocked_episodes"], 0)
            self.assertFalse(applied["retention_compliance_claimed"])
            with open(mixed, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("still-held", text)
            self.assertIn("expired-clear", text)
            self.assertIn("no-owner", text)

    def test_partial_failure_stops_without_a_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cleanup.mark_synthetic_store(tmp)
            first = os.path.join(tmp, "results", "one.json")
            second = os.path.join(tmp, "results", "two.json")
            _write(first, '{"episode_id":"one","expires_at":"2026-11-01T00:00:00+00:00"}\n')
            _write(second, '{"episode_id":"two","expires_at":"2026-11-01T00:00:00+00:00"}\n')
            real_unlink = os.unlink
            calls = {"n": 0}

            def flaky(path: str) -> None:
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise OSError("injected failure")
                real_unlink(path)

            with patch("cleanup.os.unlink", flaky):
                applied = cleanup.apply_synthetic_cleanup(
                    tmp, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
                )
            self.assertTrue(applied["partial_failure"])
            self.assertEqual(applied["counts"]["removed_files"], 1)
            names = set(os.listdir(os.path.join(tmp, "results")))
            self.assertEqual(len(names), 1)
            self.assertFalse(any(name.endswith(".tombstone") or "sha256" in name for name in os.listdir(tmp)))
            self.assertNotIn("episode_id", json.dumps(applied))
            self.assertNotIn("one.json", json.dumps(applied))
            self.assertNotIn("two.json", json.dumps(applied))

    def test_exact_boundary_uses_instants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cleanup.mark_synthetic_store(tmp)
            _write(
                os.path.join(tmp, "results", "boundary.json"),
                '{"episode_id":"boundary","expires_at":"2026-11-10T00:00:00+00:00"}\n',
            )
            _write(
                os.path.join(tmp, "results", "offset.json"),
                '{"episode_id":"offset","expires_at":"2026-11-09T19:00:00-05:00"}\n',
            )
            _write(
                os.path.join(tmp, "results", "future.json"),
                '{"episode_id":"future","expires_at":"2026-11-09T19:00:01-05:00"}\n',
            )
            applied = cleanup.apply_synthetic_cleanup(
                tmp, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertTrue(applied["applied"])
            remaining = os.listdir(os.path.join(tmp, "results"))
            self.assertEqual(remaining, ["future.json"])
            self.assertEqual(
                retention.try_aware_deadline("2026-11-09T19:00:00-05:00"),
                CLOCK,
            )
            self.assertEqual(
                retention.try_aware_deadline("2026-11-09T19:00:01-05:00"),
                CLOCK + timedelta(seconds=1),
            )

    def test_hardlink_conflict_and_untied_sidecar_block_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as root:
            cleanup.mark_synthetic_store(root)
            inside = os.path.join(root, "results", "linked.json")
            _write(inside, '{"episode_id":"linked","expires_at":"2026-11-01T00:00:00+00:00"}\n')
            outside_name = os.path.join(outside, "linked.json")
            os.link(inside, outside_name)
            before = _snapshot(root)
            with open(outside_name, "rb") as handle:
                outside_before = handle.read()
            applied = cleanup.apply_synthetic_cleanup(
                root, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertFalse(applied["applied"])
            self.assertTrue(applied["containment_blockers"])
            self.assertEqual(applied["counts"]["removed_files"], 0)
            self.assertEqual(applied["counts"]["files_after"], applied["counts"]["files_before"])
            self.assertGreater(applied["counts"]["files_before"], 0)
            self.assertEqual(_snapshot(root), before)
            with open(outside_name, "rb") as handle:
                self.assertEqual(handle.read(), outside_before)

        with tempfile.TemporaryDirectory() as tmp:
            cleanup.mark_synthetic_store(tmp)
            _write(os.path.join(tmp, "results", "early.json"), '{"episode_id":"split","expires_at":"2026-11-01T00:00:00+00:00"}\n')
            _write(os.path.join(tmp, "results", "late.json"), '{"episode_id":"split","expires_at":"2026-12-01T00:00:00+00:00"}\n')
            _write(os.path.join(tmp, "results", "untied.sha256"), "untied-sidecar\n")
            before = _snapshot(tmp)
            early_clock = datetime(2026, 11, 9, tzinfo=timezone.utc)
            dry_early = cleanup.plan_cleanup(tmp, now=early_clock, policy=POLICY)
            dry_late = cleanup.plan_cleanup(tmp, now=CLOCK, policy=POLICY)
            self.assertEqual(_snapshot(tmp), before)
            self.assertGreater(dry_late["counts"]["conflicting_deadline_records"], 0)
            self.assertEqual(dry_early["evaluated_at"], early_clock.isoformat())
            self.assertEqual(dry_late["evaluated_at"], CLOCK.isoformat())
            applied = cleanup.apply_synthetic_cleanup(
                tmp, now=CLOCK, policy=POLICY, apply=True, confirm=cleanup.SYNTHETIC_APPLY_CONFIRMATION,
            )
            self.assertFalse(applied["applied"])
            self.assertEqual(_snapshot(tmp), before)
            self.assertIn("untied-sidecar", before["results/untied.sha256"].decode())

        with tempfile.TemporaryDirectory() as tmp:
            cleanup.mark_synthetic_store(tmp)
            _write(
                os.path.join(tmp, "results", "later.json"),
                '{"episode_id":"later","expires_at":"2026-11-20T00:00:00+00:00"}\n',
            )
            before = _snapshot(tmp)
            early = cleanup.plan_cleanup(tmp, now=CLOCK, policy=POLICY)
            late = cleanup.plan_cleanup(
                tmp, now=datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc), policy=POLICY,
            )
            self.assertEqual(_snapshot(tmp), before)
            self.assertEqual(early["counts"]["eligible_files"], 0)
            self.assertEqual(early["counts"]["retained_episodes"], 1)
            self.assertEqual(late["counts"]["eligible_files"], 1)
            self.assertEqual(late["counts"]["expired_episodes"], 1)
            self.assertEqual(late["counts"]["removed_files"], 0)
