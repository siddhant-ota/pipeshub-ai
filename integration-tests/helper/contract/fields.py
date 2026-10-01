"""The query and JSON body fields of an operation, and which of them hold an ID.

Schemathesis fills an ID field with a random string. The backend then answers
"not found" or "invalid", and no valid request with that field ever gets a 2xx.
Nothing fails, so the gap is invisible. The suite must therefore give every ID
field a real value or waive it with a reason; `suite.load_suite` enforces that.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from helper.contract.spec import Operation, operation_definition, resolve

ARRAY_ITEM = "[*]"
# One element of an array, in a path that is split into segments.
ANY_ITEM = "*"

_ID_NAME = re.compile(r"(?:Ids?|IDs?|_ids?|Keys?|_keys?)$")
_ID_DESCRIPTION = re.compile(r"\b(?:uuid|objectid|ids?)\b", re.IGNORECASE)
_NOT_AN_ID_FORMAT = frozenset({"date-time", "date", "email", "uri", "binary"})
_COMBINATORS = ("allOf", "anyOf", "oneOf")
_MAX_DEPTH = 8
_JSON = "application/json"


@dataclass(frozen=True)
class RequestField:
    """A leaf field, named as in the suite file: `query.projectId`, `body.filters.kb[*]`."""

    name: str
    is_id: bool


def pointer_path(schema_pointer: str) -> tuple[str, ...]:
    """The property path a JSON Schema pointer leads to, with `*` for `items`.

    `/allOf/0/properties/filters/properties/kb/items/type` -> (`filters`, `kb`, `*`).
    """
    parts = [part for part in schema_pointer.split("/") if part]
    path: list[str] = []
    index = 0
    while index < len(parts):
        if parts[index] == "properties" and index + 1 < len(parts):
            path.append(parts[index + 1])
            index += 2
        else:
            if parts[index] == "items":
                path.append(ANY_ITEM)
            index += 1
    return tuple(path)


def field_name(location: str, path: tuple[str, ...]) -> str:
    """(`body`, (`filters`, `kb`, `*`)) -> `body.filters.kb[*]`."""
    name = location
    for segment in path:
        name += ARRAY_ITEM if segment == ANY_ITEM else f".{segment}"
    return name


def _is_leaf(schema: dict[str, Any]) -> bool:
    return not any(key in schema for key in ("properties", "items", *_COMBINATORS))


def _holds_an_id(name: str, schema: dict[str, Any]) -> bool:
    if schema.get("type") not in ("string", None):
        return False
    if schema.get("enum") is not None or schema.get("format") in _NOT_AN_ID_FORMAT:
        return False
    return (
        bool(_ID_NAME.search(name))
        or schema.get("format") == "uuid"
        or bool(_ID_DESCRIPTION.search(str(schema.get("description") or "")))
    )


def _inherit_description(parent: dict[str, Any], child: Any) -> Any:
    """A wrapper (`allOf`, an array) often carries the description of what it wraps."""
    if isinstance(child, dict) and "description" not in child and parent.get("description"):
        return {**child, "description": parent["description"]}
    return child


def _walk(
    spec: dict[str, Any], schema: Any, path: str, name: str, depth: int
) -> Iterator[RequestField]:
    schema = resolve(spec, schema)
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return
    for combinator in _COMBINATORS:
        for branch in schema.get(combinator) or []:
            branch = _inherit_description(schema, resolve(spec, branch))
            yield from _walk(spec, branch, path, name, depth + 1)
    for child, child_schema in (schema.get("properties") or {}).items():
        yield from _walk(spec, child_schema, f"{path}.{child}", child, depth + 1)
    if "items" in schema:
        items = _inherit_description(schema, resolve(spec, schema["items"]))
        yield from _walk(spec, items, f"{path}{ARRAY_ITEM}", name, depth + 1)
    if name and _is_leaf(schema):
        yield RequestField(path, _holds_an_id(name, schema))


def request_fields(spec: dict[str, Any], operation: Operation) -> list[RequestField]:
    """Every leaf field in the query parameters and the JSON body of `operation`."""
    definition = operation_definition(spec, operation)
    path_item = spec["paths"][operation.path]
    found: dict[str, RequestField] = {}

    def _add(fields: Iterator[RequestField]) -> None:
        for field in fields:
            # One name can be reached through several `oneOf` branches; an ID in any of them counts.
            known = found.get(field.name)
            found[field.name] = field if known is None or field.is_id else known

    for parameter in [*(path_item.get("parameters") or []), *(definition.get("parameters") or [])]:
        parameter = resolve(spec, parameter)
        if parameter.get("in") != "query":
            continue
        schema = _inherit_description(parameter, resolve(spec, parameter.get("schema") or {}))
        _add(_walk(spec, schema, f"query.{parameter['name']}", parameter["name"], 0))

    body = resolve(spec, definition.get("requestBody") or {})
    json_body = (body.get("content") or {}).get(_JSON) or {}
    _add(_walk(spec, json_body.get("schema"), "body", "", 0))

    return sorted(found.values(), key=lambda field: field.name)
