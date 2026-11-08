"""Relative phone lookup is disabled in this authorized scope.

The default research path does not import a live search. Calling these
entry points prints or raises a refusal and does not write an output file.
"""

from __future__ import annotations


class RelativeSearchDisabled(RuntimeError):
    """Relative search cannot run in this review scope."""


_MESSAGE = (
    "RELATIVE SEARCH DISABLED: relative expansion is off the default path. "
    "No request was sent and no output file was written."
)


def raw_search(*_args: object, **_kwargs: object) -> dict:
    raise RelativeSearchDisabled(_MESSAGE)


def lookup(*_args: object, **_kwargs: object) -> dict:
    raise RelativeSearchDisabled(_MESSAGE)


def main(_argv: list[str] | None = None) -> int:
    print(_MESSAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
