"""Offline acceptance tests for the disconnected Enformion research lane.

Synthetic fixtures only. Sockets and HTTP are denied for the whole module.
The suite does not read home-directory credentials or a default records store.
"""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import audit
import client
import handoff
import receiver
import relative_phones
import retention
import run_lookup
import verify

SOURCE = "https://example.org/fictional-roster"
COUNTY = "Example County"
BOOKING = "BK-FICTION-0001"
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)
FIRST_SEEN = datetime(2026, 11, 8, 3, 5, tzinfo=timezone.utc)
EXPIRES = "2026-11-20T00:00:00+00:00"
POLICY = {"mode": "explicit_expires_at"}
HIDDEN = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "hidden_files", "enformion-records")
)


def person(**overrides):
    values = {
        "first": "Casey",
        "last": "Exampleton",
        "age": 40,
        "city": "Exampleville",
        "state": "EX",
        "relatives": [],
        "subject_phones": [],
    }
    values.update(overrides)
    return client.PersonRecord(**values)


def prepare(**overrides):
    values = {
        "first": "Casey",
        "last": "Exampleton",
        "city": "Exampleville",
        "state": "EX",
        "age": 40,
        "county": COUNTY,
        "source": SOURCE,
        "booking_reference": BOOKING,
        "observed_at": OBSERVED,
        "first_seen_at": FIRST_SEEN,
        "now": NOW,
        "candidates": [person()],
    }
    values.update(overrides)
    return run_lookup.prepare_offline(**values)


class OfflineAcceptanceTests(unittest.TestCase):
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
        for key in (
            "ENFORMION_RECORDS_DIR",
            "ENFORMION_KEYS_FILE",
            "ENFORMION_RETENTION_POLICY",
        ):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for item in getattr(self, "_patches", []):
            item.stop()
        self.assertEqual(self.network_calls, [])

    def _deny_network(self, *args, **kwargs):
        self.network_calls.append(repr(args[:1]))
        raise AssertionError("live network is denied in offline tests")

    def test_duplicate_equal_candidates_require_review_and_do_not_export_verified(self) -> None:
        first = person(relatives=[client.Relative("Riley Exampleton", "relative", [client.Phone("555-0100")])])
        second = person(relatives=[client.Relative("Rowan Exampleton", "relative")])
        report = prepare(candidates=[first, second])
        decision = report["consistency"]
        self.assertEqual(decision["status"], "needs_human_review")
        self.assertTrue(decision["review_required"])
        self.assertEqual(decision["disposition"], "needs_human_review")
        self.assertFalse(decision["verified_export"])
        self.assertFalse(decision["identity_proof"])
        self.assertNotIn("candidate", decision)
        self.assertIn("duplicate equal candidates", decision["reason"])
        blob = json.dumps(report)
        self.assertNotIn('"status": "VERIFIED"', blob)
        self.assertNotIn('"status": "verified"', blob)
        self.assertNotIn('"verified_export": true', blob)
        self.assertNotIn("Riley", blob)
        self.assertNotIn("Rowan", blob)
        self.assertNotIn("555-0100", blob)
        self.assertFalse(report["receipt"]["persisted"])
        self.assertEqual(report["receipt"]["live_linkage"], "blocked")

    def test_zero_candidates_missing_age_and_conflicting_state_are_not_verified(self) -> None:
        zero = prepare(candidates=[])
        self.assertEqual(zero["consistency"]["status"], "no_candidate")
        self.assertFalse(zero["consistency"]["verified_export"])
        self.assertNotIn("candidate", zero["consistency"])

        unknown = prepare(candidates=[person(first="Other", last="Fiction")])
        self.assertEqual(unknown["consistency"]["status"], "no_candidate")
        self.assertFalse(unknown["consistency"]["verified_export"])

        missing_query_age = prepare(age=None, candidates=[person()])
        self.assertEqual(missing_query_age["consistency"]["status"], "missing_identifier")
        self.assertIn("age", missing_query_age["consistency"]["reason"])
        self.assertFalse(missing_query_age["consistency"]["verified_export"])

        missing_record_age = prepare(candidates=[person(age=None)])
        self.assertEqual(missing_record_age["consistency"]["status"], "missing_identifier")
        self.assertIn("age", missing_record_age["consistency"]["reason"])
        self.assertFalse(missing_record_age["consistency"]["verified_export"])

        missing_state = prepare(candidates=[person(state="")])
        self.assertEqual(missing_state["consistency"]["status"], "missing_identifier")
        self.assertIn("state", missing_state["consistency"]["reason"])

        blocked_by_incomplete = prepare(candidates=[person(), person(age=None)])
        self.assertNotEqual(blocked_by_incomplete["consistency"]["status"], "candidate_consistent")
        self.assertFalse(blocked_by_incomplete["consistency"]["verified_export"])

        conflict = prepare(candidates=[person(state="ZZ")])
        self.assertEqual(conflict["consistency"]["status"], "conflicting_identifier")
        self.assertIn("state", conflict["consistency"]["reason"])
        self.assertFalse(conflict["consistency"]["verified_export"])
        self.assertFalse(conflict["consistency"]["identity_proof"])

        supplied_conflict = prepare(
            candidates=[person()],
            supplied_states=("EX", "ZZ"),
        )
        self.assertEqual(supplied_conflict["consistency"]["status"], "conflicting_identifier")
        self.assertFalse(supplied_conflict["consistency"]["verified_export"])

    def test_unique_consistent_candidate_is_not_identity_proof(self) -> None:
        report = prepare(candidates=[person()])
        decision = report["consistency"]
        self.assertEqual(decision["status"], "candidate_consistent")
        self.assertEqual(decision["plausible_count"], 1)
        self.assertFalse(decision["identity_proof"])
        self.assertFalse(decision["verified_export"])
        self.assertFalse(decision["review_required"])
        self.assertEqual(
            decision["consistency_meaning"],
            "candidate_consistency_not_identity_proof",
        )
        self.assertEqual(
            decision["candidate"],
            {
                "first": "Casey",
                "last": "Exampleton",
                "city": "Exampleville",
                "state": "EX",
                "age": 40,
            },
        )
        packet = report["handoff"]
        self.assertEqual(packet["schema_version"], handoff.SCHEMA_VERSION)
        self.assertTrue(packet["synthetic"])
        self.assertEqual(packet["synthetic_marker"], "synthetic_fixture_not_a_person")
        self.assertEqual(packet["classification"], "public_observation_not_authorized_referral")
        self.assertEqual(packet["eligibility"], "not_performed")
        self.assertIsNone(packet["production_destination"])
        self.assertFalse(packet["live_lane_permitted"])
        self.assertEqual(packet["lane"], "disconnected_mock")
        self.assertNotIn("referral_authorization", packet)
        self.assertEqual(packet["provenance"]["source"], SOURCE)

    def test_explicit_referral_authorization_does_not_perform_eligibility(self) -> None:
        report = prepare(referral_authorization={"supplied": True})
        self.assertEqual(report["handoff"]["referral_authorization"], {"supplied": True})
        self.assertEqual(report["handoff"]["eligibility"], "not_performed")
        self.assertEqual(
            report["handoff"]["classification"],
            "public_observation_not_authorized_referral",
        )

    def test_default_path_omits_relative_calls_and_output(self) -> None:
        calls: list[str] = []

        def boom(*_args, **_kwargs):
            calls.append("called")
            raise AssertionError("relative lookup was invoked")

        with patch.object(relative_phones, "lookup", boom), patch.object(relative_phones, "raw_search", boom):
            report = prepare(candidates=None)
        self.assertEqual(calls, [])
        blob = json.dumps(report)
        self.assertNotIn("Riley", blob)
        self.assertNotIn("555-0100", blob)
        self.assertNotIn("Example Wireless", blob)
        self.assertNotIn("relatives", blob)
        self.assertNotIn("phones", blob)
        self.assertFalse(report["consistency"]["relative_output_present"])
        self.assertEqual(report["consistency"]["relative_search"], "disabled")
        self.assertEqual(report["consistency"]["status"], "candidate_consistent")
        self.assertEqual(report["workflow"], "disconnected_mock")

    def test_supplied_contact_mismatch_is_not_verified_and_is_not_discovered(self) -> None:
        on_relative_only = person(
            relatives=[client.Relative("Riley Exampleton", "relative", [client.Phone("555-0100")])]
        )
        missing = prepare(candidates=[on_relative_only], phone="555-0100")
        self.assertEqual(missing["consistency"]["status"], "missing_identifier")
        self.assertIn("contact", missing["consistency"]["reason"])
        self.assertNotIn("555-0100", json.dumps(missing))

        mismatched = prepare(
            candidates=[person(subject_phones=["555-0199"])],
            phone="555-0101",
        )
        self.assertEqual(mismatched["consistency"]["status"], "conflicting_identifier")
        self.assertFalse(mismatched["consistency"]["verified_export"])
        self.assertNotIn("555-0199", json.dumps(mismatched))
        self.assertNotIn("555-0101", json.dumps(mismatched))

        matched = prepare(
            candidates=[person(subject_phones=["555-0102"])],
            phone="555-0102",
        )
        self.assertEqual(matched["consistency"]["status"], "candidate_consistent")
        self.assertNotIn("555-0102", json.dumps(matched))
        self.assertFalse(matched["consistency"]["identity_proof"])

    def test_mocked_transport_makes_zero_live_requests(self) -> None:
        report = prepare(candidates=None)
        self.assertEqual(report["workflow"], "disconnected_mock")
        with self.assertRaises(client.LiveLookupDisabled):
            client.get_adapter(mock=False)
        with self.assertRaises(client.LiveLookupDisabled):
            client.HttpAdapter()
        bare = client.HttpAdapter.__new__(client.HttpAdapter)
        with self.assertRaises(client.LiveLookupDisabled):
            bare.search_person("Casey", "Exampleton", "Exampleville", "EX")
        with self.assertRaises(relative_phones.RelativeSearchDisabled):
            relative_phones.raw_search(None, "Casey", "Exampleton", "Exampleville", "EX")
        with self.assertRaises(relative_phones.RelativeSearchDisabled):
            relative_phones.lookup(None, "Riley Exampleton", "Exampleville", "EX", 3)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = relative_phones.main(["--name", "Riley Exampleton"])
        self.assertEqual(code, 2)
        self.assertIn("RELATIVE SEARCH DISABLED", stdout.getvalue())
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(__file__), "..", "results")))
        import dormant_discovery
        with self.assertRaises(dormant_discovery.DiscoveryDisabled):
            dormant_discovery.discover({"persons": []})
        with self.assertRaises(dormant_discovery.DiscoveryDisabled):
            dormant_discovery.search_relatives("Riley Exampleton")
        self.assertEqual(self.network_calls, [])

    def test_cli_is_mock_only_and_rejects_a_live_switch(self) -> None:
        argv = [
            "--first", "Casey",
            "--last", "Exampleton",
            "--city", "Exampleville",
            "--state", "EX",
            "--age", "40",
            "--county", COUNTY,
            "--source", SOURCE,
            "--booking-ref", BOOKING,
            "--observed-at", "2026-11-08T03:00:00+00:00",
            "--first-seen-at", "2026-11-08T03:05:00+00:00",
        ]
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = run_lookup.main(argv)
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["consistency"]["status"], "candidate_consistent")
        self.assertFalse(report["consistency"]["identity_proof"])
        self.assertNotIn("Riley", stdout.getvalue())
        self.assertNotIn("VERIFIED", stdout.getvalue())
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            with self.assertRaises(SystemExit) as caught:
                run_lookup.main(argv + ["--live"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("unrecognized arguments: --live", stderr.getvalue())

    def test_absent_retention_policy_blocks_real_linkage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = prepare(policy=None, records_directory=tmp, expires_at=EXPIRES)
            self.assertFalse(report["receipt"]["persisted"])
            self.assertEqual(report["receipt"]["live_linkage"], "blocked")
            self.assertIn("retention policy absent", report["receipt"]["reason"])
            self.assertEqual(os.listdir(tmp), [])
            blocked = audit.log_event(
                policy=None,
                expires_at=EXPIRES,
                directory=tmp,
                now=NOW,
                episode_id="episode",
                outcome="candidate_consistent",
            )
            self.assertFalse(blocked["persisted"])
            self.assertEqual(blocked["live_linkage"], "blocked")
            stored = audit.store_result(
                directory=tmp,
                payload={"name": "Casey Exampleton"},
                policy=None,
                expires_at=EXPIRES,
                now=NOW,
            )
            self.assertFalse(stored["persisted"])
            self.assertEqual(os.listdir(tmp), [])
            duration = audit.log_event(
                policy={"mode": "explicit_expires_at", "hours": 23},
                expires_at=EXPIRES,
                directory=tmp,
                now=NOW,
            )
            self.assertFalse(duration["persisted"])
            self.assertEqual(duration["live_linkage"], "blocked")
            self.assertIn("duration", duration["reason"])
            self.assertEqual(os.listdir(tmp), [])
        self.assertFalse(retention.policy_is_explicit({"mode": "explicit_expires_at", "hours": 23}))
        self.assertFalse(retention.policy_is_explicit(None))
        self.assertFalse(os.path.exists(HIDDEN))

    def test_deadline_validation_offset_invalid_expired_extension_and_earliest(self) -> None:
        aware = retention.parse_expires_at("2026-11-20T15:00:00-05:00", now=NOW)
        self.assertEqual(aware.utcoffset(), timedelta(hours=-5))
        for bad in (None, "", "   ", "not-a-timestamp", "2026-11-20T15:00:00", "2026-11-01T00:00:00+00:00"):
            with self.assertRaises(retention.RetentionRejected):
                retention.parse_expires_at(bad, now=NOW)

        original = "2026-11-20T00:00:00+00:00"
        with self.assertRaises(retention.RetentionRejected):
            retention.propose_deadline(original, "2026-11-21T00:00:00+00:00", now=NOW)
        earlier = retention.propose_deadline(original, "2026-11-19T18:00:00-05:00", now=NOW)
        self.assertEqual(earlier, datetime(2026, 11, 19, 23, 0, tzinfo=timezone.utc))
        with self.assertRaises(retention.RetentionRejected):
            retention.propose_deadline(original, "2026-11-19T20:00:00-05:00", now=NOW)

        ledger = retention.DeadlineLedger(original, now=NOW)
        ledger.propose("2026-11-18T00:00:00+00:00", now=NOW)
        with self.assertRaises(retention.RetentionRejected):
            ledger.propose(original, now=NOW)
        self.assertEqual(ledger.earliest, datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc))
        stamped = ledger.stamp_derivatives()
        for name in ("log", "queue", "output", "packet"):
            self.assertEqual(stamped[name]["expires_at"], "2026-11-18T00:00:00+00:00")
            self.assertNotIn("sha256", stamped[name])

        with self.assertRaises(retention.RetentionRejected):
            retention.bind_derivatives(
                original,
                {
                    "log": original,
                    "queue": original,
                    "output": original,
                    "packet": "2026-11-25T00:00:00+00:00",
                },
                now=NOW,
            )
        bound = retention.bind_derivatives(
            original,
            {
                "log": original,
                "queue": "2026-11-19T00:00:00+00:00",
                "output": "2026-11-19T12:00:00+00:00",
                "packet": "2026-11-19T18:00:00+00:00",
            },
            now=NOW,
        )
        for name in ("log", "queue", "output", "packet"):
            self.assertEqual(bound["derivatives"][name]["expires_at"], "2026-11-19T00:00:00+00:00")
        self.assertNotIn("sha256", json.dumps(bound))

        with tempfile.TemporaryDirectory() as tmp:
            base = prepare(expires_at=original, policy=POLICY, records_directory=None)
            lengthened = json.loads(json.dumps(base["handoff"]))
            lengthened["audit_copy"] = {"expires_at": "2026-11-25T00:00:00+00:00"}
            refused = receiver.receive(lengthened, policy=POLICY, now=NOW, records_directory=tmp)
            self.assertFalse(refused["persisted"])
            self.assertEqual(refused["live_linkage"], "blocked")
            self.assertEqual(os.listdir(tmp), [])

            shortened = json.loads(json.dumps(base["handoff"]))
            shortened["queue_copy"] = {"expires_at": "2026-11-19T00:00:00+00:00"}
            accepted = receiver.receive(shortened, policy=POLICY, now=NOW, records_directory=tmp)
            self.assertTrue(accepted["persisted"])
            self.assertEqual(accepted["live_linkage"], "blocked")
            self.assertIsNone(accepted["production_destination"])
            for name in ("log", "queue", "output", "packet"):
                self.assertEqual(
                    accepted["derivatives"][name]["expires_at"],
                    "2026-11-19T00:00:00+00:00",
                )
            for dirpath, _dirs, files in os.walk(tmp):
                for name in files:
                    with open(os.path.join(dirpath, name), encoding="utf-8") as handle:
                        text = handle.read()
                    self.assertIn("2026-11-19T00:00:00+00:00", text)
                    self.assertNotIn("2026-11-25", text)
                    self.assertNotIn("sha256", text)
                    self.assertNotIn("Riley", text)

    def test_source_timezone_is_not_inferred(self) -> None:
        raw_with_offset = "2026-04-01T08:00:00-04:00"
        report = prepare(source_timestamp_raw=raw_with_offset, source_timezone=None)
        provenance = report["handoff"]["provenance"]
        self.assertEqual(provenance["source_timestamp_raw"], raw_with_offset)
        self.assertIsNone(provenance["source_timezone"])
        self.assertFalse(provenance["source_timezone_inferred"])
        self.assertEqual(provenance["observed_at"], OBSERVED.isoformat())
        self.assertEqual(provenance["first_seen_at"], FIRST_SEEN.isoformat())
        self.assertNotEqual(provenance["observed_at"], provenance["first_seen_at"])
        self.assertNotEqual(provenance["observed_at"], raw_with_offset)
        self.assertNotIn("source_timestamp_utc", provenance)
        self.assertNotIn("-04:00", provenance["source_timezone"] or "")

        explicit = prepare(
            source_timestamp_raw="2026-04-01T08:00:00",
            source_timezone="America/Chicago",
        )
        explicit_provenance = explicit["handoff"]["provenance"]
        self.assertEqual(explicit_provenance["source_timestamp_raw"], "2026-04-01T08:00:00")
        self.assertEqual(explicit_provenance["source_timezone"], "America/Chicago")
        self.assertFalse(explicit_provenance["source_timestamp_raw"].endswith("America/Chicago"))
        self.assertFalse(explicit_provenance["source_timezone_inferred"])

        with self.assertRaises(handoff.HandoffError):
            prepare(observed_at=datetime(2026, 11, 8, 3, 0))

    def test_episode_key_uses_county_source_and_booking_reference(self) -> None:
        stable = handoff.episode_id(COUNTY, SOURCE, BOOKING)
        self.assertEqual(stable, handoff.episode_id("  example   county ", SOURCE, "bk-fiction-0001"))
        self.assertNotEqual(stable, handoff.episode_id(COUNTY, SOURCE, "BK-FICTION-0002"))
        self.assertNotEqual(stable, handoff.episode_id("Other County", SOURCE, BOOKING))
        self.assertNotIn("casey", stable)
        self.assertNotIn("exampleton", stable)

        casey = prepare(first="Casey", last="Exampleton")
        other = prepare(first="Other", last="Fiction", candidates=[person(first="Other", last="Fiction")])
        self.assertEqual(casey["handoff"]["episode"]["episode_id"], other["handoff"]["episode"]["episode_id"])
        self.assertEqual(casey["handoff"]["episode"]["episode_id"], stable)
        self.assertEqual(casey["handoff"]["episode"]["county"], COUNTY)
        self.assertEqual(casey["handoff"]["episode"]["source"], SOURCE)
        self.assertEqual(casey["handoff"]["episode"]["booking_reference"], BOOKING)

    def test_synthetic_input_cannot_enter_live_lane(self) -> None:
        report = prepare()
        packet = report["handoff"]
        self.assertTrue(packet["synthetic"])
        self.assertIsNone(packet["production_destination"])
        self.assertFalse(packet["live_lane_permitted"])
        with self.assertRaises(handoff.SyntheticLiveForbidden):
            handoff.refuse_live_lane(packet)
        with self.assertRaises(handoff.HandoffError):
            handoff.build_packet(
                consistency=report["consistency"],
                county=COUNTY,
                source=SOURCE,
                booking_reference=BOOKING,
                observed_at=OBSERVED,
                first_seen_at=FIRST_SEEN,
                supplied_identifiers={"first": "Casey", "last": "Exampleton", "city": "Exampleville", "state": "EX", "age": 40},
                synthetic=False,
            )
        with self.assertRaises(handoff.HandoffError):
            handoff.build_packet(
                consistency=report["consistency"],
                county=COUNTY,
                source=SOURCE,
                booking_reference=BOOKING,
                observed_at=OBSERVED,
                first_seen_at=FIRST_SEEN,
                supplied_identifiers={"first": "Casey", "last": "Exampleton", "city": "Exampleville", "state": "EX", "age": 40},
                production_destination="https://research.example/live",
            )
        self.assertEqual(report["receipt"]["live_linkage"], "blocked")
        self.assertIsNone(report["receipt"]["production_destination"])

    def test_artifact_inventory_is_dry_run_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._write(os.path.join(tmp, "query-log.jsonl"), '{"episode_id":"ep","expires_at":"2026-11-19T00:00:00+00:00","note":"CANARY-PERSON-FIELD"}\n')
            self._write(os.path.join(tmp, "research-log.jsonl"), '{"episode_id":"ep","expires_at":"2026-11-19T00:00:00+00:00","outcome":"candidate_consistent"}\n')
            self._write(os.path.join(tmp, "audit-events.jsonl"), '{"episode_id":"ep","expires_at":"2026-11-19T00:00:00+00:00"}\n')
            self._write(os.path.join(tmp, "results", "synthetic.json"), '{"expires_at":"2026-11-19T00:00:00+00:00","note":"CANARY-PERSON-FIELD"}\n')
            self._write(os.path.join(tmp, "results", "relative-phones.json"), '{"expires_at":"2026-11-10T00:00:00+00:00","note":"CANARY-PERSON-FIELD"}\n')
            self._write(os.path.join(tmp, "packets", "handoff-synthetic.json"), '{"expires_at":"2026-11-19T00:00:00+00:00","synthetic":true}\n')
            self._write(os.path.join(tmp, "backups", "research-log.jsonl.bak"), '{"expires_at":"2026-11-19T00:00:00+00:00"}\n')
            before = self._snapshot(tmp)
            report = retention.dry_run_deletion(tmp, now=NOW, policy=None)
            after = self._snapshot(tmp)
            self.assertEqual(before, after)
            self.assertEqual(report["deleted"], [])
            self.assertFalse(report["would_delete"])
            self.assertFalse(report["real_deletion_performed"])
            self.assertEqual(report["mode"], "inventory_dry_run")
            self.assertFalse(report["operational_deletion_proof"]["provided_by_this_run"])
            self.assertTrue(report["operational_deletion_proof"]["remaining_requirements"])
            blob = json.dumps(report)
            self.assertNotIn("CANARY-PERSON-FIELD", blob)
            for kind in (
                "query_log",
                "audit_log",
                "research_log",
                "saved_result_json",
                "relative_output",
                "handoff_packet",
                "backup",
            ):
                self.assertIn(kind, report["kinds_found"])
            for artifact in report["artifacts"]:
                self.assertFalse(artifact["would_delete"])

            stored = prepare(
                policy=POLICY,
                expires_at=EXPIRES,
                records_directory=tmp,
                candidates=[person(relatives=[client.Relative("Riley Exampleton", "relative", [client.Phone("555-0100")])])],
            )
            self.assertTrue(stored["receipt"]["persisted"])
            self.assertEqual(stored["receipt"]["live_linkage"], "blocked")
            persisted = self._snapshot(tmp)
            second = retention.dry_run_deletion(tmp, now=NOW, policy=POLICY)
            self.assertEqual(persisted, self._snapshot(tmp))
            self.assertEqual(second["deleted"], [])
            self.assertFalse(second["real_deletion_performed"])
            combined = "\n".join(text.decode() for text in persisted.values())
            self.assertNotIn("sha256", combined)
            self.assertNotIn("Riley", combined)
            self.assertNotIn("555-0100", combined)
            self.assertIn("results/relative-phones.json", persisted)

    def test_credentials_and_user_config_are_not_read(self) -> None:
        with patch("os.path.expanduser", side_effect=AssertionError("expanduser")), patch("builtins.open", side_effect=AssertionError("open")):
            with self.assertRaises(client.LiveLookupDisabled):
                client.load_credentials()
        self.assertFalse(os.path.exists(HIDDEN))
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = __import__("research_log").main([])
        self.assertEqual(code, 0)
        self.assertIn("No default store was opened", stdout.getvalue())
        self.assertFalse(os.path.exists(HIDDEN))

    @staticmethod
    def _write(path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    @staticmethod
    def _snapshot(root: str) -> dict[str, bytes]:
        found = {}
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                with open(path, "rb") as handle:
                    found[os.path.relpath(path, root)] = handle.read()
        return found


if __name__ == "__main__":
    unittest.main()
