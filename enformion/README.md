# Enformion research lane — packet agent

One lane for offender contact research + emergency-contact verification:
booking → Enformion person search → verify (name + arrest city + exact age)
→ up to 4 relatives, up to 3 wireless-first numbers each → packet block.

## Files
- `client.py` — credential loader + adapters (Mock for offline tests;
  Http filled in 2026-10-17 from the account's own API docs — endpoint,
  galaxy- auth headers, request build, response parser).
- `verify.py` — the standing verification doctrine (unchanged from 2026-10-07).
- `run_lookup.py` — CLI: one lookup in, one packet block out. Every run is
  audit-logged (see below). `--source <ref>` ties a lookup to its booking.
- `audit.py` — the audit + records layer (added 2026-10-17, HBK's order).
- `research_log.py` — view the kept track of lookups (metadata only).

## Research log + kept records (audit)
- Records live in `../../hidden_files/enformion-records/` (override with
  `$ENFORMION_RECORDS_DIR`): directories mode 700, files mode 600, local only.
- `research-log.jsonl` — append-only, one line per lookup EVENT: timestamps
  (UTC + ET), lookup id, adapter, booking source ref, the query (name, city,
  state, age), outcome (VERIFIED / UNVERIFIED / NOT_CONFIGURED / ERROR),
  reason, and counts (records / relatives / numbers returned).
- `results/<lookup_id>.json` — the full kept research record, written ONLY
  for VERIFIED lookups (unverified yields no contacts, so none are kept).
  Its sha256 is pinned in the log line, so later edits are detectable.
- Credentials are never logged or stored. Retention: kept until HBK orders
  destruction (shred policy).
- View: `python3 research_log.py [--last N] [--subject "First Last"] [--verified]`

## Credentials
- Name + password pair, read from `~/.config/enformion/keys.env` (chmod 600)
  or `$ENFORMION_KEYS_FILE`. Never printed, logged, committed, or sent in chat.
- The same pair is also saved as a website login in the Secure Vault
  (api.enformion.com) for browser sign-in to the portal/ApiDemo.

## To go live — status 2026-10-17
- ✅ ApiDemo shape CAPTURED from the account's own API docs inside the
  portal (endpoint, galaxy- auth headers, request fields, response
  layout) and the HttpAdapter is filled in; its response parser is
  proven against a docs-shaped dummy response (verify 31 VERIFIED /
  30 UNVERIFIED). Full notes: portal-survey-2026-10-17.md.
- ✅ Keys file placed on the Nexus box (~/.config/enformion/keys.env,
  chmod 600, verified by screenshot 2026-10-17).
- ⏳ Remaining: a copy of this code on the Nexus box for the agents to
  call, then the first live test lookup (one call, audit-logged).
- Known caveat: the base Person Search response returns relatives'
  identities, not their phone numbers — a relative's numbers need a
  follow-up Person Search on that relative.

## Test (mock data)
```
cd files/enformion
python3 run_lookup.py --first Marcus --last Delane --city Orlando --state FL --age 31 --mock   # VERIFIED
python3 run_lookup.py --first Marcus --last Delane --city Orlando --state FL --age 30 --mock   # UNVERIFIED
```
