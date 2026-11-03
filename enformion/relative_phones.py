"""Follow-up phone lookup for verified relatives — Nexus packet-agent lane.

The packet contract keeps phone numbers on RELATIVES, not on the offender
record, and a relative's numbers only appear as the subject of their own
Person Search (client.parse_response deliberately does not map a subject's
phoneNumbers onto the record). This tool runs that follow-up search for one
or more named relatives and extracts, for the best-matching person:

  - full name, age, best address (most recently reported)
  - phone numbers, best first: connected + wireless ahead of landlines,
    then most recently seen; capped per relative

Usage:
  python3 relative_phones.py --city Minneapolis --state MN \
      --name "Aaron J Mccoy Smith" --name "Evelyn Elizabeth Smith" \
      [--max-phones 5] [--out results/relative-phones.json]

Match rule: exact first + last name; among exact matches prefer one located
in the search city, then the search state, then the first returned. The
number of same-name candidates is reported so a human can judge ambiguity.
Results are written to a local JSON file (default under results/) — contact
data lives in files, never in the audit log.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import uuid

from client import HttpAdapter, _live_full_name


def raw_search(adapter: HttpAdapter, first: str, last: str,
               city: str, state: str) -> dict:
    """POST one Person Search and return the RAW payload (unlike
    client.search_person, which parses and drops subject phoneNumbers)."""
    body = json.dumps(
        adapter.build_request(first, last, city, state)).encode()
    req = urllib.request.Request(
        adapter.BASE_URL + adapter.SEARCH_PATH, data=body, method="POST",
        headers={
            "galaxy-ap-name": adapter._name,
            "galaxy-ap-password": adapter._password,
            "galaxy-search-type": adapter.SEARCH_TYPE,
            "galaxy-client-session-id": uuid.uuid4().hex,
            "content-type": "application/json",
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"Enformion API HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Enformion API unreachable: {exc.reason}") from None


def _name_matches(person: dict, first: str, last: str) -> bool:
    name = person.get("name") or {}
    return (str(name.get("firstName", "")).lower() == first.lower()
            and str(name.get("lastName", "")).lower() == last.lower())


def _person_cities(person: dict) -> list[tuple[str, str]]:
    out = []
    for loc in person.get("locations") or []:
        if isinstance(loc, dict):
            out.append((str(loc.get("city", "")), str(loc.get("state", ""))))
    if not out:
        for addr in person.get("addresses") or []:
            if isinstance(addr, dict) and addr.get("city"):
                out.append((str(addr["city"]), str(addr.get("state", ""))))
    return out


def pick_person(payload: dict, first: str, last: str,
                city: str, state: str) -> tuple[dict | None, int]:
    """Return (best exact-name person or None, count of exact-name matches).
    Preference: located in city > located in state > first exact match."""
    persons = [p for p in (payload.get("persons") or [])
               if isinstance(p, dict) and _name_matches(p, first, last)]
    if not persons:
        return None, 0
    for want_city, want_state in ((city, state), ("", state)):
        for person in persons:
            for pc, ps in _person_cities(person):
                if want_state and ps.lower() != want_state.lower():
                    continue
                if want_city and pc.lower() != want_city.lower():
                    continue
                return person, len(persons)
    return persons[0], len(persons)


def best_address(person: dict) -> str:
    """Most recently reported fullAddress on the record."""
    addrs = [a for a in (person.get("addresses") or [])
             if isinstance(a, dict) and a.get("fullAddress")]
    if not addrs:
        return ""
    addrs.sort(key=lambda a: str(a.get("lastReportedDate", "")), reverse=True)
    return str(addrs[0]["fullAddress"])


def extract_phones(person: dict, cap: int = 5) -> list[dict]:
    """Phones best-first: connected wireless, then wireless, then the rest;
    within a tier, most recently seen first."""
    def tier(ph: dict) -> int:
        ptype = str(ph.get("phoneType", "")).lower()
        wireless = any(w in ptype for w in ("wireless", "mobile", "cell"))
        if wireless and ph.get("isConnected"):
            return 0
        if wireless:
            return 1
        if ph.get("isConnected"):
            return 2
        return 3

    phones = [ph for ph in (person.get("phoneNumbers") or [])
              if isinstance(ph, dict) and ph.get("phoneNumber")]
    phones.sort(key=lambda ph: str(ph.get("phoneLastSeenDate", "")),
                reverse=True)
    phones.sort(key=tier)  # stable: keeps last-seen order inside each tier
    out = []
    for ph in phones[:cap]:
        out.append({
            "number": str(ph["phoneNumber"]),
            "type": str(ph.get("phoneType", "")),
            "carrier": str(ph.get("company", "")),
            "connected": bool(ph.get("isConnected")),
            "first_seen": str(ph.get("phoneFirstSeenDate", "")),
            "last_seen": str(ph.get("phoneLastSeenDate", "")),
        })
    return out


def lookup(adapter: HttpAdapter, full_name: str, city: str, state: str,
           cap: int) -> dict:
    parts = full_name.split()
    first, last = parts[0], parts[-1]
    payload = raw_search(adapter, first, last, city, state)
    person, candidates = pick_person(payload, first, last, city, state)
    if person is None:
        return {"query_name": full_name, "matched": False, "candidates": 0,
                "phones": []}
    cities = _person_cities(person)
    return {
        "query_name": full_name,
        "matched": True,
        "candidates": candidates,
        "name": _live_full_name(person.get("name") or {}) or full_name,
        "age": person.get("age") if isinstance(person.get("age"), int)
               else None,
        "city": cities[0][0] if cities else "",
        "state": cities[0][1] if cities else "",
        "address": best_address(person),
        "phones": extract_phones(person, cap=cap),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", action="append", required=True,
                    help="full name of a relative (repeatable)")
    ap.add_argument("--city", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--max-phones", type=int, default=5)
    ap.add_argument("--out", default="",
                    help="JSON output path (default results/relative-phones.json)")
    args = ap.parse_args()

    adapter = HttpAdapter()
    results = []
    for full_name in args.name:
        try:
            res = lookup(adapter, full_name, args.city, args.state,
                         args.max_phones)
        except Exception as exc:  # one failure must not lose the others
            res = {"query_name": full_name, "matched": False,
                   "candidates": 0, "phones": [], "error": str(exc)}
        results.append(res)
        print(f"== {full_name} ==")
        if res.get("error"):
            print(f"  ERROR: {res['error']}")
        elif not res["matched"]:
            print("  NO MATCH — no exact-name record returned")
        else:
            head = f"  {res['name']}"
            if res["age"] is not None:
                head += f", age {res['age']}"
            if res["city"]:
                head += f" — {res['city']}, {res['state']}"
            if res["candidates"] > 1:
                head += f"  [{res['candidates']} same-name candidates; best shown]"
            print(head)
            if res["address"]:
                print(f"  Address: {res['address']}")
            if not res["phones"]:
                print("  Phones: none on record")
            for ph in res["phones"]:
                bits = [ph["number"]]
                if ph["type"]:
                    bits.append(ph["type"])
                if ph["carrier"]:
                    bits.append(ph["carrier"])
                if ph["last_seen"]:
                    bits.append(f"last seen {ph['last_seen']}")
                print(f"    {' · '.join(bits)}")

    out_path = args.out
    if not out_path:
        out_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "results", "relative-phones.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump({"city": args.city, "state": args.state,
                   "results": results}, fh, indent=2)
    print(f"Saved: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
