"""Real values for request fields: what the fixtures give, and how a request gets them.

Path parameters go through Schemathesis' own `parameters` config. It cannot
set a body field in its coverage phase, and a forced query value would be added
to every request, so `hooks.py` puts body and query values in with the
functions below. A value replaces a string that is already in the request; it
is never added, and the field under test in a negative case is left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from helper.contract.fields import ANY_ITEM, ARRAY_ITEM, pointer_path

BODY = "body"
QUERY = "query"


@dataclass
class ContractValues:
    """The values a run can use, and why any other value is missing."""

    values: dict[str, str] = field(default_factory=dict)
    # value key -> why the fixture that provides it failed
    missing: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Substitution:
    location: str
    path: tuple[str, ...]
    value: str

    @classmethod
    def for_field(cls, field_name: str, value: str) -> Substitution:
        location, path = parse_field(field_name)
        return cls(location, path, value)


@dataclass(frozen=True)
class Mutation:
    """What a negative test case made invalid, as Schemathesis reports it."""

    location: str
    # Query: the parameter name. Body: the media type.
    parameter: str
    # Body: where in the schema, for example `/properties/filters/properties/kb/items/type`.
    schema_pointer: str


def parse_field(field_name: str) -> tuple[str, tuple[str, ...]]:
    """`body.filters.kb[*]` -> (`body`, (`filters`, `kb`, `*`))."""
    location, _, rest = field_name.partition(".")
    if location not in (BODY, QUERY) or not rest:
        raise ValueError(f"A request field starts with `body.` or `query.`: {field_name!r}")
    path: list[str] = []
    for part in rest.split("."):
        name = part
        while name.endswith(ARRAY_ITEM):
            name = name[: -len(ARRAY_ITEM)]
        path.append(name)
        path.extend([ANY_ITEM] * ((len(part) - len(name)) // len(ARRAY_ITEM)))
    return location, tuple(path)


def is_under_test(substitution: Substitution, mutation: Mutation | None) -> bool:
    """True if the negative case made this very field, or what contains it, invalid."""
    if mutation is None or mutation.location != substitution.location:
        return False
    if substitution.location == QUERY:
        return mutation.parameter == substitution.path[0]
    mutated = pointer_path(mutation.schema_pointer)
    shared = min(len(mutated), len(substitution.path))
    return mutated[:shared] == substitution.path[:shared]


def _replace(node: Any, path: tuple[str, ...], value: str) -> tuple[Any, int]:
    if not path:
        return (value, 1) if isinstance(node, str) else (node, 0)
    head, rest = path[0], path[1:]
    if head == ANY_ITEM:
        if not isinstance(node, list):
            return node, 0
        replaced = [_replace(item, rest, value) for item in node]
        return [item for item, _ in replaced], sum(count for _, count in replaced)
    if not isinstance(node, dict) or head not in node:
        return node, 0
    child, count = _replace(node[head], rest, value)
    return ({**node, head: child}, count) if count else (node, 0)


def substitute(container: Any, substitution: Substitution) -> tuple[Any, int]:
    """Return `container` with the value put in, and how many strings it replaced."""
    return _replace(container, substitution.path, substitution.value)
