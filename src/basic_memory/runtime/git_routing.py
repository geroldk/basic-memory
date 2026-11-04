"""Lifecycle-owned local routing without changing process environment or credentials."""

from collections.abc import Iterator
from contextlib import contextmanager

_owners: list[object] = []


def git_local_routing_active() -> bool:
    return bool(_owners)


@contextmanager
def local_git_routing(enabled: bool) -> Iterator[None]:
    owner = object()
    if enabled:
        _owners.append(owner)
    try:
        yield
    finally:
        if enabled:
            _owners.remove(owner)
