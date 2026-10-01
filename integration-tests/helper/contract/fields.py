"""The query and JSON body fields of an operation, and which of them hold an ID.

Schemathesis fills an ID field with a random string. The backend then answers
"not found" or "invalid", and no valid request with that field ever gets a 2xx.
Nothing fails, so the gap is invisible. The suite must therefore decide on every
ID field: give it a real value, or say why it keeps a generated one.
`suite.load_suite` enforces that.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from helper.contract.spec import Operation, operation_definition, operation_parameters, resolve

ARRAY_ITEM = "[*]"
# One element of an array, in a path that is split into segments.
ANY_ITEM = "*"
# A value under a key the spec does not name (`additionalProperties`).
ANY_KEY = "{*}"

# `id`, `ids`, `key`, and names that end in `Id`, `Ids`, `_id`, `Key`, ...
_ID_NAME = re.compile(r"^(?:ids?|keys?)$|(?:Ids?|IDs?|_ids?|Keys?|_keys?)$")
_ID_DESCRIPTION = re.compile(r"\b(?:uuid|objectid|ids?)\b", re.IGNORECASE)
_NOT_AN_ID_FORMAT = frozenset({"date-time", "date", "email", "uri", "binary"})
_COMBINATORS = ("allOf", "anyOf", "oneOf")
# Deeper than any request body in the spec. A schema that goes past it is reported, not cut off.
_MAX_DEPTH = 16
# Bodies that Schemathesis builds from named fields, so a field of one can get a value.
_STRUCTURED_BODIES = (
    "application/json",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
)
_REF = "$ref"
FILE_FORMAT = "binary"


@dataclass(frozen=True)
class RequestField:
    """A leaf field, named as in the suite file: `query.projectId`, `body.filters.kb[*]`."""

    name: str
    is_id: bool
    # `format` of its schema: `uuid`, `binary` (a file of a multipart body), ... or "".
    format: str = ""

    @property
    def is_file(self) -> bool:
        return self.format == FILE_FORMAT


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
    if isinstance(schema.get("additionalProperties"), dict):
        return False
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
    spec: dict[str, Any], schema: Any, path: str, name: str, refs: frozenset[str]
) -> Iterator[RequestField]:
    """`refs` are the references followed on the way here; meeting one again is a cycle."""
    described_by = schema if isinstance(schema, dict) else {}
    if isinstance(schema, dict) and isinstance(schema.get(_REF), str):
        if schema[_REF] in refs:
            return
        refs = refs | {schema[_REF]}
        schema = _inherit_description(described_by, resolve(spec, schema))
    if not isinstance(schema, dict):
        return
    if path.count(".") + path.count(ARRAY_ITEM) > _MAX_DEPTH:
        raise ValueError(f"the schema nests deeper than {_MAX_DEPTH} levels at {path}")

    for combinator in _COMBINATORS:
        for branch in schema.get(combinator) or []:
            yield from _walk(spec, _inherit_description(schema, branch), path, name, refs)
    for child, child_schema in (schema.get("properties") or {}).items():
        yield from _walk(spec, child_schema, f"{path}.{child}", child, refs)
    if "items" in schema:
        items = _inherit_description(schema, schema["items"])
        yield from _walk(spec, items, f"{path}{ARRAY_ITEM}", name, refs)
    free_form = schema.get("additionalProperties")
    if isinstance(free_form, dict):
        yield from _walk(
            spec, _inherit_description(schema, free_form), f"{path}{ANY_KEY}", name, refs
        )
    if name and _is_leaf(schema):
        yield RequestField(path, _holds_an_id(name, schema), str(schema.get("format") or ""))


def _path_schemas(spec: dict[str, Any], operation: Operation) -> dict[str, dict[str, Any]]:
    return {
        parameter["name"]: resolve(spec, parameter.get("schema") or {})
        for parameter in operation_parameters(spec, operation)
        if parameter.get("in") == "path"
    }


def enumerated_path_parameters(spec: dict[str, Any], operation: Operation) -> frozenset[str]:
    """Path parameters whose values the spec lists. Schemathesis sends every one of them."""
    return frozenset(
        name
        for name, schema in _path_schemas(spec, operation).items()
        if schema.get("enum") is not None
    )


def path_parameter_formats(spec: dict[str, Any], operation: Operation) -> dict[str, str]:
    return {
        name: str(schema.get("format") or "")
        for name, schema in _path_schemas(spec, operation).items()
    }


def request_fields(spec: dict[str, Any], operation: Operation) -> list[RequestField]:
    """Every leaf field in the query parameters and the JSON or form body of `operation`."""
    definition = operation_definition(spec, operation)
    found: dict[str, RequestField] = {}

    def _add(fields: Iterator[RequestField]) -> None:
        for field in fields:
            # One name can be reached through several `oneOf` branches; an ID in any of them counts.
            known = found.get(field.name)
            found[field.name] = field if known is None or field.is_id else known

    for parameter in operation_parameters(spec, operation):
        if parameter.get("in") != "query":
            continue
        schema = _inherit_description(parameter, parameter.get("schema") or {})
        _add(_walk(spec, schema, f"query.{parameter['name']}", parameter["name"], frozenset()))

    body = resolve(spec, definition.get("requestBody") or {})
    for media_type in _STRUCTURED_BODIES:
        content = (body.get("content") or {}).get(media_type) or {}
        _add(_walk(spec, content.get("schema"), "body", "", frozenset()))

    return sorted(found.values(), key=lambda field: field.name)
