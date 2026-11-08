"""Offline Enformion research client.

The default adapter is an in-memory synthetic fixture. Live HTTP, credential
reads, and relative expansion are disconnected. ``HttpAdapter`` refuses
before it can build a request, open a socket, or read a keys file. There is
no switch that turns the live lane back on.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class LiveLookupDisabled(RuntimeError):
    """A live lookup or credential read was refused."""


@dataclass
class Phone:
    number: str
    label: str = ""
    carrier: str = ""
    first_reported: str = ""


@dataclass
class Relative:
    name: str
    relation: str = ""
    phones: list[Phone] = field(default_factory=list)


@dataclass
class PersonRecord:
    first: str
    last: str
    age: int | None
    city: str
    state: str
    relatives: list[Relative] = field(default_factory=list)
    subject_phones: list[str] = field(default_factory=list)


def load_credentials() -> tuple[str, str]:
    """Credential reads are disconnected in this review scope."""
    raise LiveLookupDisabled(
        "credential reads are disconnected in this review scope"
    )


class MockAdapter:
    """Synthetic roster used by the offline default path.

    The sample includes a relative so tests can prove the default workflow
    drops relative output. These identifiers are fictional.
    """

    SAMPLE = [
        PersonRecord(
            first="Casey",
            last="Exampleton",
            age=40,
            city="Exampleville",
            state="EX",
            relatives=[
                Relative(
                    "Riley Exampleton",
                    "relative",
                    [Phone("555-0100", "Wireless", "Example Wireless", "2019-01")],
                )
            ],
        ),
        PersonRecord(
            first="Casey",
            last="Exampleton",
            age=99,
            city="Exampleville",
            state="EX",
        ),
        PersonRecord(
            first="Casey",
            last="Exampleton",
            age=40,
            city="Exampleville",
            state="ZZ",
        ),
    ]

    def search_person(self, first: str, last: str, city: str, state: str) -> list[PersonRecord]:
        del city, state  # the consistency check, not this fixture, applies location
        return [
            record for record in self.SAMPLE
            if record.first.casefold() == first.casefold()
            and record.last.casefold() == last.casefold()
        ]


class HttpAdapter:
    """Dormant live adapter. Every entry point refuses."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise LiveLookupDisabled(
            "HttpAdapter is disconnected. This scope has no live activation switch."
        )

    def search_person(self, *_args: object, **_kwargs: object) -> list[PersonRecord]:
        raise LiveLookupDisabled(
            "Person search is disconnected. No live request was sent."
        )


def get_adapter(mock: bool = True) -> MockAdapter:
    """Return the offline fixture adapter.

    ``mock=False`` does not fall back to the network. It refuses.
    """
    if not mock:
        raise LiveLookupDisabled(
            "live Enformion adapter is disconnected; no fallback call is made"
        )
    return MockAdapter()
