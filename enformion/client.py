"""Enformion research client — Nexus packet-agent lane.

Credential discipline (Omerta):
- Credentials are a name + password pair. They are read ONLY from a local
  keys file (default ~/.config/enformion/keys.env, expected perms 600) or
  from the ENFORMION_KEYS_FILE env var pointing at one.
- Values are never printed, logged, or returned by any function here.
- Keys file format (two lines):
      ENFORMION_AP_NAME=<value>
      ENFORMION_AP_PASSWORD=<value>

The HTTP adapter's endpoint/header layout was captured 2026-10-17 from
the account's own API documentation inside the portal (full notes in
portal-survey-2026-10-17.md, same folder). The mock adapter remains the
offline test path.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field


class NotConfigured(RuntimeError):
    pass


@dataclass
class Phone:
    number: str
    label: str = ""            # "Wireless" / "Landline" / "" as reported
    carrier: str = ""
    first_reported: str = ""


@dataclass
class Relative:
    name: str
    relation: str = ""         # "relative" preferred over "associate"
    phones: list[Phone] = field(default_factory=list)


@dataclass
class PersonRecord:
    first: str
    last: str
    age: int | None
    city: str
    state: str
    relatives: list[Relative] = field(default_factory=list)


def load_credentials() -> tuple[str, str]:
    """Return (ap_name, ap_password) from the keys file. Never logs values."""
    path = os.environ.get(
        "ENFORMION_KEYS_FILE",
        os.path.expanduser("~/.config/enformion/keys.env"),
    )
    if not os.path.exists(path):
        raise NotConfigured(f"keys file not found: {path}")
    if os.stat(path).st_mode & 0o077:
        raise NotConfigured(f"keys file {path} must be chmod 600")
    name = password = ""
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("ENFORMION_AP_NAME="):
                name = line.split("=", 1)[1].strip()
            elif line.startswith("ENFORMION_AP_PASSWORD="):
                password = line.split("=", 1)[1].strip()
    if not name or not password:
        raise NotConfigured("keys file is missing ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD")
    return name, password


class MockAdapter:
    """Deterministic sample data so the pipeline is testable end to end
    before the live API shape is wired in."""

    SAMPLE = [
        PersonRecord(
            first="Marcus", last="Delane", age=31, city="Orlando", state="FL",
            relatives=[
                Relative("Tanya Delane", "relative", [
                    Phone("407-555-0114", "Wireless", "T-Mobile", "2019-03"),
                    Phone("407-555-0115", "Wireless", "T-Mobile", "2021-06"),
                    Phone("407-555-0116", "Landline", "AT&T", "2015-01"),
                    Phone("407-555-0117", "Wireless", "Verizon", "2023-02"),
                ]),
                Relative("Jerome Delane", "relative", [
                    Phone("321-555-0143", "Wireless", "AT&T", "2020-11"),
                ]),
                Relative("Paula Rendon", "associate", [
                    Phone("407-555-0177", "", "", "2022-05"),
                ]),
            ],
        ),
        # Same name, wrong age — must never verify.
        PersonRecord(first="Marcus", last="Delane", age=47, city="Orlando", state="FL"),
    ]

    def search_person(self, first: str, last: str, city: str, state: str) -> list[PersonRecord]:
        return [r for r in self.SAMPLE
                if r.first.lower() == first.lower() and r.last.lower() == last.lower()]


def _full_name(parts: dict) -> str:
    return " ".join(
        str(parts.get(k, "")).strip()
        for k in ("FirstName", "MiddleName", "LastName")
        if str(parts.get(k, "")).strip()
    )


def parse_response(payload, want_city: str = "", want_state: str = "") -> list[PersonRecord]:
    """Map an Enformion Person Search response into PersonRecords.

    Shape (from the account's API docs, 2026-10-17): each result carries a
    Person block (name parts, Age, Locations[] of City/State), Addresses[],
    PhoneNumbers[] (the subject's own — the packet contract keeps numbers
    on relatives, so these are not mapped), RelativesSummary[] and
    AssociatesSummary[] (identity + DOB only — a relative's numbers require
    a follow-up Person Search on that relative; see the survey file).
    Accepts a bare list of results or a dict wrapping one.
    """
    if isinstance(payload, dict):
        results = None
        for key in ("Results", "Persons", "Matches", "SearchResults", "PersonSearchResults"):
            if isinstance(payload.get(key), list):
                results = payload[key]
                break
        if results is None:
            results = [payload] if "Person" in payload else []
    elif isinstance(payload, list):
        results = payload
    else:
        return []

    records: list[PersonRecord] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        person = item.get("Person") or {}
        name = person.get("Name") or {}
        age = person.get("Age")
        if not isinstance(age, int) or isinstance(age, bool):
            age = None

        locations = [loc for loc in (person.get("Locations") or []) if isinstance(loc, dict)]
        chosen = None
        for loc in locations:
            if want_city and str(loc.get("City", "")).lower() == want_city.lower():
                chosen = loc
                break
        if chosen is None and locations:
            chosen = locations[0]
        city = str(chosen.get("City", "")) if chosen else ""
        state = str(chosen.get("State", "")) if chosen else ""
        if not city:
            for addr in item.get("Addresses") or []:
                if isinstance(addr, dict) and addr.get("City"):
                    city = str(addr["City"])
                    state = str(addr.get("State", ""))
                    break

        relatives: list[Relative] = []
        for rel in item.get("RelativesSummary") or []:
            if isinstance(rel, dict):
                rname = _full_name(rel.get("Name") or rel)
                if rname:
                    relatives.append(Relative(name=rname, relation="relative", phones=[]))
        for assoc in item.get("AssociatesSummary") or []:
            if isinstance(assoc, dict):
                aname = _full_name(assoc.get("Name") or assoc)
                if aname:
                    relatives.append(Relative(name=aname, relation="associate", phones=[]))

        records.append(PersonRecord(
            first=str(name.get("FirstName", "")),
            last=str(name.get("LastName", "")),
            age=age, city=city, state=state,
            relatives=relatives,
        ))
    return records


class HttpAdapter:
    """Live Enformion API adapter — shape captured 2026-10-17 from the
    account's own API documentation (portal-survey-2026-10-17.md)."""

    BASE_URL = "https://devapi.enformion.com"
    SEARCH_PATH = "/PersonSearch"
    SEARCH_TYPE = "Person"   # detailed results for paying accounts
    # Include tokens are written without spaces (docs' own example uses
    # "Addresses" / "PhoneNumbers"); includes are account-managed and the
    # rep can adjust them if a live call pushes back.
    INCLUDES = ["Addresses", "PhoneNumbers", "RelativesSummary",
                "AssociatesSummary", "DateOfBirth"]

    def __init__(self) -> None:
        self._name, self._password = load_credentials()

    def build_request(self, first: str, last: str, city: str, state: str) -> dict:
        body: dict = {"FirstName": first, "LastName": last,
                      "Includes": list(self.INCLUDES),
                      "Page": 1, "ResultsPerPage": 10}
        line2 = ", ".join(p for p in (city, state) if p)
        if line2:
            body["Addresses"] = [{"AddressLine2": line2}]
        return body

    def search_person(self, first: str, last: str, city: str, state: str) -> list[PersonRecord]:
        import uuid

        body = json.dumps(self.build_request(first, last, city, state)).encode()
        req = urllib.request.Request(
            self.BASE_URL + self.SEARCH_PATH, data=body, method="POST",
            headers={
                "galaxy-ap-name": self._name,
                "galaxy-ap-password": self._password,
                "galaxy-search-type": self.SEARCH_TYPE,
                "galaxy-client-session-id": uuid.uuid4().hex,
                "content-type": "application/json",
                "accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Enformion API HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Enformion API unreachable: {exc.reason}") from None
        return parse_response(payload, want_city=city, want_state=state)


def get_adapter(mock: bool = False):
    return MockAdapter() if mock else HttpAdapter()
