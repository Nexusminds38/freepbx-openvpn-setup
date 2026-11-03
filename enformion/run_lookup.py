"""Run one Enformion verification lookup and print the packet block.

Usage:
  python3 run_lookup.py --first Marcus --last Delane --city Orlando \
      --state FL --age 31 [--mock] [--source <booking record ref>]

Without --mock it uses the live adapter, which raises NotConfigured until
the ApiDemo request shape is filled in and the keys file is placed.
Output is a packet block: VERIFIED lines carry contacts; UNVERIFIED lines
carry the reason and never any contact data.

Every run — verified, unverified, not-configured, or error — is written to
the audit research log (see audit.py). VERIFIED runs also store the full
research record (contacts included) locally; its sha256 is in the log.
"""

from __future__ import annotations

import argparse
import sys

import audit
from client import NotConfigured, get_adapter
from verify import verify


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--first", required=True)
    ap.add_argument("--last", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--age", required=True, type=int)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--source", default="",
                    help="booking record reference this lookup belongs to")
    args = ap.parse_args()

    lookup_id = audit.new_lookup_id()
    adapter_name = "mock" if args.mock else "http"
    query = {"first": args.first, "last": args.last, "city": args.city,
             "state": args.state, "age": args.age}

    try:
        adapter = get_adapter(mock=args.mock)
        records = adapter.search_person(args.first, args.last, args.city, args.state)
    except NotConfigured as exc:
        audit.log_event(lookup_id=lookup_id, adapter=adapter_name, query=query,
                        outcome="NOT_CONFIGURED", reason=str(exc),
                        source_ref=args.source)
        print(f"NOT CONFIGURED: {exc}")
        print(f"Audit: logged as {lookup_id} (NOT_CONFIGURED)")
        return 2
    except Exception as exc:  # never lose an attempted lookup from the record
        audit.log_event(lookup_id=lookup_id, adapter=adapter_name, query=query,
                        outcome="ERROR", reason=f"{type(exc).__name__}: {exc}",
                        source_ref=args.source)
        print(f"ERROR: {exc}")
        print(f"Audit: logged as {lookup_id} (ERROR)")
        return 3

    rec, reason = verify(records, first=args.first, last=args.last,
                         city=args.city, age=args.age)
    print(f"{args.first} {args.last} — booking age {args.age}, {args.city}, {args.state}")
    if rec is None:
        audit.log_event(lookup_id=lookup_id, adapter=adapter_name, query=query,
                        outcome="UNVERIFIED", reason=reason,
                        source_ref=args.source,
                        records_returned=len(records))
        print(f"VERDICT: UNVERIFIED — {reason}")
        print(f"Audit: logged as {lookup_id} (UNVERIFIED, no contact data kept)")
        return 1

    relatives = len(rec.relatives)
    phones = sum(len(r.phones) for r in rec.relatives)
    payload = {
        "lookup_id": lookup_id,
        "adapter": adapter_name,
        "source_ref": args.source,
        "query": query,
        "verdict": "VERIFIED",
        "reason": reason,
        "record": audit.record_to_payload(rec),
    }
    rel_path, digest = audit.store_result(lookup_id, payload)
    audit.log_event(lookup_id=lookup_id, adapter=adapter_name, query=query,
                    outcome="VERIFIED", reason=reason, source_ref=args.source,
                    records_returned=len(records), relatives_returned=relatives,
                    phones_returned=phones, result_file=rel_path,
                    result_sha256=digest)
    print(f"VERDICT: VERIFIED — {reason}")
    print(f"Record: {rec.first} {rec.last}, age {rec.age}, {rec.city}, {rec.state}")
    print("Emergency-contact candidates (confirm before packet use):")
    for rel in rec.relatives:
        print(f"  - {rel.name} ({rel.relation or 'contact'})")
        for ph in rel.phones:
            bits = [ph.number]
            if ph.label:
                bits.append(ph.label)
            if ph.carrier:
                bits.append(ph.carrier)
            if ph.first_reported:
                bits.append(f"since {ph.first_reported}")
            print(f"      {' · '.join(bits)}")
    print(f"Audit: logged as {lookup_id} (VERIFIED, record kept at {rel_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
