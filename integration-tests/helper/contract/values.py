"""Real values for request fields: what the fixtures give, and how a request gets them.

`hooks.py` puts the values into each request with the functions below, just
before it is sent. Schemathesis has its own setting for this, but it replaces
the value in every request, also in the one that tests that very parameter.
Here the field under test in an invalid request is left alone, and every other
field gets its value, so that the request is invalid in one way only. A query or
body value replaces a generated value of its own type; it is never added.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from helper.contract.fields import ANY_ITEM, ANY_KEY, ARRAY_ITEM, pointer_path

BODY = "body"
QUERY = "query"
PATH = "path"
_LOCATIONS = (BODY, QUERY, PATH)
# In a value: replaced by a number that is different in each request, for a name that must
# be unique. `contract-team-{case}` gives `contract-team-1`, `contract-team-2`, ...
CASE_NUMBER = "{case}"

# A value from a fixture is text. A constant of the suite file can be a number or a boolean.
Value = str | int | float | bool
# A fixture can also give several values for one key: each request of an operation takes
# the next one. For an operation that uses its object up (add a user to a group: the next
# request needs a group that the user is not in yet).
ValueOrPool = Value | list[str]


@dataclass
class ContractValues:
    """The values a run can use, and why any other value is missing."""

    values: dict[str, ValueOrPool] = field(default_factory=dict)
    # value key -> why the fixture that provides it failed
    missing: dict[str, str] = field(default_factory=dict)
    # value key -> why this deployment cannot give it: its fixture skipped
    unavailable: dict[str, str] = field(default_factory=dict)
    # way to log in (`session`, ...) -> why this run cannot use it
    no_login: dict[str, str] = field(default_factory=dict)
    # way to log in -> why this deployment does not have it: its fixture skipped
    unavailable_login: dict[str, str] = field(default_factory=dict)
    # Value keys whose values are credentials; no file of the run shows them.
    secret: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Substitution:
    location: str
    path: tuple[str, ...]
    value: ValueOrPool

    @classmethod
    def for_field(cls, field_name: str, value: ValueOrPool) -> Substitution:
        location, path = parse_field(field_name)
        return cls(location, path, value)

    def for_case(self, number: int, turn: int = 0) -> Substitution:
        """The substitution for one request.

        `number` is different in each request of the run and replaces `{case}`.
        `turn` counts the requests of the operation and picks the value from a pool.
        """
        value = self.value
        if isinstance(value, list):
            value = value[turn % len(value)]
        if isinstance(value, str) and CASE_NUMBER in value:
            value = value.replace(CASE_NUMBER, str(number))
        return self if value is self.value else Substitution(self.location, self.path, value)


@dataclass(frozen=True)
class Mutation:
    """What a negative test case made invalid, as Schemathesis reports it."""

    location: str
    # Path and query: the parameter name. Body: the media type.
    parameter: str
    # Body: where in the schema, for example `/properties/filters/properties/kb/items/type`.
    schema_pointer: str


def _segments(part: str) -> list[str]:
    """`kb[*]` -> [`kb`, `*`]; `roles{*}` -> [`roles`, `{*}`]."""
    suffixes: list[str] = []
    while part.endswith((ARRAY_ITEM, ANY_KEY)):
        suffix = ARRAY_ITEM if part.endswith(ARRAY_ITEM) else ANY_KEY
        suffixes.insert(0, ANY_ITEM if suffix == ARRAY_ITEM else ANY_KEY)
        part = part[: -len(suffix)]
    return [part, *suffixes]


def parse_field(field_name: str) -> tuple[str, tuple[str, ...]]:
    """`body.filters.kb[*]` -> (`body`, (`filters`, `kb`, `*`))."""
    location, _, rest = field_name.partition(".")
    if location not in _LOCATIONS or not rest:
        raise ValueError(
            f"A request field starts with `body.`, `query.` or `path.`: {field_name!r}"
        )
    return location, tuple(segment for part in rest.split(".") for segment in _segments(part))


def is_under_test(substitution: Substitution, mutation: Mutation | None) -> bool:
    """True if the negative case made this very field invalid.

    Not if it made something around the field invalid (a property that is
    missing next to it, an unknown one, an array with too many items): the
    field then gets its value like in any other request. Otherwise the API
    could reject the request for the generated ID, and the test would pass
    without showing that the API saw the invalid part.
    """
    if mutation is None or mutation.location != substitution.location:
        return False
    if substitution.location in (QUERY, PATH):
        return mutation.parameter == substitution.path[0]
    # A value under a free-form key has no schema pointer of its own to compare with.
    return pointer_path(mutation.schema_pointer) == tuple(
        segment for segment in substitution.path if segment != ANY_KEY
    )


def _same_kind(generated: Any, value: ValueOrPool) -> bool:
    """A value replaces only a generated value of its own type: a wrong type is left as it is."""
    if isinstance(value, bool) or isinstance(generated, bool):
        return isinstance(value, bool) and isinstance(generated, bool)
    if isinstance(value, str):
        return isinstance(generated, str)
    return isinstance(generated, int | float)


def _replace(node: Any, path: tuple[str, ...], value: ValueOrPool) -> tuple[Any, int]:
    if not path:
        return (value, 1) if _same_kind(node, value) else (node, 0)
    head, rest = path[0], path[1:]
    if head == ANY_ITEM:
        if not isinstance(node, list):
            return node, 0
        replaced = [_replace(item, rest, value) for item in node]
        return [item for item, _ in replaced], sum(count for _, count in replaced)
    if head == ANY_KEY:
        if not isinstance(node, dict):
            return node, 0
        under_keys = {key: _replace(item, rest, value) for key, item in node.items()}
        count = sum(count for _, count in under_keys.values())
        return ({key: item for key, (item, _) in under_keys.items()}, count) if count else (node, 0)
    if not isinstance(node, dict) or head not in node:
        return node, 0
    child, count = _replace(node[head], rest, value)
    return ({**node, head: child}, count) if count else (node, 0)


def substitute(container: Any, substitution: Substitution) -> tuple[Any, int]:
    """Return `container` with the value put in, and how many values it replaced."""
    return _replace(container, substitution.path, substitution.value)
