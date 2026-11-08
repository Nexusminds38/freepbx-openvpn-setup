# Enformion research lane — offline review

This directory is a disconnected, mock-only research preparation lane.
Bookings supervision, the 5-county monitor, and any live research receiver
stay outside this change. A public booking observation is classified as
`public_observation_not_authorized_referral`. It is not an authorized
referral and it is not an eligibility determination.

No production destination is configured. Synthetic input cannot enter a
live lane. This code does not start monitors, transfer production records,
or call Enformion.

## What the previous code did

Read from `run_lookup.py`, `client.py`, `verify.py`, `relative_phones.py`,
`audit.py`, and `research_log.py` before this branch:

- `verify.py` returned the first row whose first name, last name, city, and
  exact age matched, and it attached up to four relatives with phone
  numbers. A second equal row was ignored. State was collected and not
  compared.
- A missing age stopped at the first same-name, same-city row. Other
  incomplete or conflicting rows did not become a review state.
- The default adapter was HTTP. `get_adapter(mock=False)` built a live
  Person Search. Relative phone lookup always posted a follow-up search
  and wrote `results/relative-phones.json`.
- Audit records and the research log were append-only under a default
  `hidden_files` directory, with a sha256 of the stored result. Retention
  was "until destruction is ordered", with no `expires_at`.

Those behaviors are the reason this lane now fails closed.

## Consistency

`verify.check_consistency` compares supplied identifiers only.

| Situation | Status | Export |
| --- | --- | --- |
| No plausible row | `no_candidate` | not verified |
| Missing first, last, city, state, age, or a supplied contact that is not already on the subject row | `missing_identifier` | not verified |
| Supplied values disagree, or the only same-city rows conflict on age, state, or supplied contact | `conflicting_identifier` | not verified |
| Two or more equal plausible rows, or more than one plausible row | `needs_human_review` | not verified; no chosen candidate |
| One consistent row, and no same-city row is missing a required identifier | `candidate_consistent` | candidate consistency only |

`identity_proof` stays false. `verified_export` stays false. Relative rows
and phone numbers are not read for discovery and are not copied into the
result. A supplied phone can confirm or contradict a number that is already
on the subject row. A number that exists only on a relative does not count.

Rows that clearly conflict on a present age or state are not plausible.
They do not, by themselves, block a different row that matches every
supplied identifier. A same-city row that is missing age or state blocks a
consistency result, because that row cannot be excluded.

## Default workflow

`run_lookup.py` calls `prepare_offline`. The adapter is `MockAdapter`.
`get_adapter(mock=False)`, `HttpAdapter`, and `relative_phones` refuse
before credentials, sockets, or output files. `dormant_discovery.py` is not
imported by the workflow and its entry points refuse. There is no flag that
turns live lookup back on.

```bash
cd enformion
python3 run_lookup.py \
  --first Casey --last Exampleton --city Exampleville --state EX --age 40 \
  --county "Example County" \
  --source https://example.org/fictional-roster \
  --booking-ref BK-FICTION-0001 \
  --observed-at 2026-11-08T03:00:00+00:00 \
  --first-seen-at 2026-11-08T03:05:00+00:00
```

The sample person, county, and roster URL are fictional. Exit `0` means
candidate consistency. Exit `2` means human review. Exit `1` means no
verified status. Exit `3` means the input or an explicit retention request
was refused. None of these codes mean identity was proved or a referral was
authorized.

## Handoff contract

`handoff.py` builds schema `enformion.offline_handoff.v1`.

- `synthetic` is true and `synthetic_marker` is `synthetic_fixture_not_a_person`.
- `lane` is `disconnected_mock` and `live_lane_permitted` is false.
- `production_destination` is null.
- `classification` is `public_observation_not_authorized_referral`.
- `eligibility` is `not_performed`.
- `referral_authorization` is omitted unless the caller supplies it. Supplying
  it does not perform eligibility.
- `episode_id` is the normalized county, source, and booking reference.
  The person's name is not part of the key.
- Provenance keeps `source_timestamp_raw` separate from `observed_at` and
  `first_seen_at`. A missing source timezone stays null. The raw value is
  not parsed to fill one.

`receiver.py` accepts that packet in memory. It does not forward it.

## Retention

This lane does not choose a retention duration. An owner duration was not
verified here, so none is configured, including any numeric proposal made
elsewhere.

Real persistence requires all of the following:

1. Policy `{"mode": "explicit_expires_at"}` with no duration field.
2. A timezone-aware `expires_at` that is still in the future. Naive, invalid,
   and expired values are rejected. A `Z` or numeric offset is accepted
   because it is part of the supplied value.
3. An explicit records directory (`ENFORMION_RECORDS_DIR` or the receiver
   `records_directory` argument).

Missing policy blocks new persistence and live linkage. It does not delete
existing files and it does not open the old `hidden_files` default.

An extension, a reset to a later deadline, or any derivative deadline later
than the original `expires_at` is rejected. When deadlines differ inside the
original window, every derivative keeps the earliest instant. The offline
copy stamps `log`, `queue`, `output`, and `packet` with that instant.
Saved JSON, the research log, and the query log receive the same value.
No identifying hash is written.

Live linkage remains `blocked` after an authorized offline copy.

Re-ingestion of the same episode cannot move that deadline later, including when a new timestamp is an earlier calendar string but a later instant. A shorter deadline is written onto the packet, the result, and every stored log or backup line for that episode. A naive or invalid deadline already on disk blocks the write; a timezone is not invented for it, and the bytes are not deleted. The records directory, a path that resolves through a symlink, and any symlink inside the directory are not followed. An identifying-hash sidecar blocks a new write and is left in place for the dry-run inventory.

### Artifact inventory

`retention.dry_run_deletion` lists files and deletes nothing. The catalog is:

| Kind | Location |
| --- | --- |
| `query_log` | `query-log.jsonl` |
| `audit_log` | `audit-events.jsonl` and audit lines in `research-log.jsonl` |
| `research_log` | `research-log.jsonl` |
| `saved_result_json` | `results/*.json` |
| `relative_output` | relative phone JSON |
| `handoff_packet` | `packets/` and `handoff*.json` |
| `backup` | `backups/` and `.bak` / `.backup` copies |

The inventory reports paths, kinds, sizes, and raw `expires_at` values. It
does not copy record bodies into the report. `would_delete` is false.

### Synthetic cleanup

`cleanup.plan_cleanup` counts expired, retained, missing, malformed, and
ambiguous records and deletes nothing. `cleanup.apply_synthetic_cleanup`
also deletes nothing unless `apply=True` and `confirm="synthetic-test-store"`.
Apply additionally requires an explicit `explicit_expires_at` policy with no
duration field and a `synthetic-store.json` marker. `mark_synthetic_store`
writes that marker only into an empty directory, so an existing store cannot
be labeled after the fact. The marker has no episode, name, or deadline.

Records for one episode are deleted only when every deadline on that episode
is the same timezone-aware instant and that instant is at or before the
supplied clock. Differing deadlines block the episode. Hardlinks and symlinks
block the whole cleanup. An untied identifying sidecar or an unclassified
file also blocks the cleanup instead of being skipped.

A record is removable only when every copy of its episode has one
timezone-aware deadline and the earliest instant is less than or equal to
the supplied clock. The same instant expressed with a numeric offset is the
same deadline. A missing, naive, or unreadable deadline blocks that episode
and is counted without deleting it. A log line with no episode id is left
in place. Lines for a different, still-unexpired episode in the same log or
backup are left in place. An identifying sidecar is removed only when its
filename matches exactly one expired episode; an untied sidecar is counted
and kept. Symlinks and paths that leave the store block the whole cleanup.
No identifying tombstone or hash is written. A partial failure stops further
deletes.

Unknown or malformed deadlines block live linkage. The cleanup report sets
`retention_compliance_claimed` false. Fixture cleanup does not prove that
any production store was deleted. Out of scope: unmarked directories, the
historical hidden records path, `/home/box` clients, Bookings, the 5-county
monitor, Roster-watch, provider retention, chat transcripts, and platform
or disk snapshots.

## Deployment readiness

Code in this lane:

- Default workflow stays mock-only. Live lookup, relative search, and
  credential reads refuse.
- `production_destination` stays unset. `live_linkage` stays blocked.
- Supplied-identifier consistency does not claim identity proof.
- Synthetic cleanup can remove expired fixture artifacts under an explicit
  deadline when the caller opts in.

Still required before any operational use:

- An owner-confirmed retention duration. None is configured here.
- A decision on the production destination. None is wired.
- A real-store inventory and an authorized deletion run against that store,
  including backups and copies outside this repository.
- Confirmation that chat, platform, and provider copies are in scope or
  explicitly excluded. This code does not reach them.

### What is still required to prove operational deletion

A passing mock test does not prove deletion. Proving it requires an
owner-verified duration recorded outside this lane, a before-inventory of
the real store (including backups and copies outside the repo), an
authorized deletion of only those artifacts whose earliest deadline has
passed, an after-inventory of the same classes, and a check that no
identifying-hash sidecar remains. This branch does not run that procedure.

## Tests

From `enformion/`:

```bash
python3 -W error::ResourceWarning -m unittest tests.test_offline_acceptance tests.test_synthetic_cleanup -v
```

The suite uses fictional identifiers, denies sockets and HTTP, and does not
read `~/.config/enformion` or a default records directory. No package install
is required.

## Files

- `verify.py` — fail-closed supplied-identifier consistency.
- `client.py` — synthetic mock adapter; live adapter and credential reads refuse.
- `run_lookup.py` — disconnected CLI and `prepare_offline`.
- `handoff.py` — synthetic handoff contract and episode key.
- `receiver.py` — offline receiver.
- `retention.py` — deadline rules, authorization, dry-run inventory.
- `cleanup.py` — dry-run by default; explicit synthetic-store cleanup.
- `audit.py` — optional offline copy when authorization succeeds.
- `research_log.py` — read a configured directory; do not create the default store.
- `relative_phones.py` — disabled entry points.
- `dormant_discovery.py` — refusal boundary, not imported by the default path.
- `tests/test_offline_acceptance.py` — offline acceptance tests.
- `tests/test_synthetic_cleanup.py` — synthetic cleanup and handoff smoke test.

## Still disconnected

- Bookings is not supervised from this branch and is not activated here.
- Live Enformion research is not activated. No paid lookup is made.
- No production receiver or destination was wired.
- The existing 5-county monitor is unchanged.
- Roster-watch is a separate repository and is unchanged.
- No database migration is included.
- An offline copy in a temp directory during tests is not a deployment.
