"""Dormant discovery boundary.

Supplied-identifier verification does not import this module. Relative
search and live person lookup stay refused. There is no enable flag and no
network client in this module.
"""

from __future__ import annotations


class DiscoveryDisabled(RuntimeError):
    """Discovery cannot run from the offline verification lane."""


def discover(*_args: object, **_kwargs: object) -> None:
    raise DiscoveryDisabled(
        "Discovery is disconnected from verification and has no live activation switch."
    )


def search_relatives(*_args: object, **_kwargs: object) -> None:
    raise DiscoveryDisabled(
        "Relative search is disabled in this authorized scope."
    )
