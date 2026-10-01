"""The OpenAPI document and the operations in it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from helper.openapi_search_validator import SPEC_PATH

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
_PATH_PARAMETER = re.compile(r"\{([^}]+)\}")
_SERVER_VARIABLE = re.compile(r"\{[^}]+\}")


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    sdk: bool
    # The security schemes the spec accepts for it, by name. Empty: it needs no login.
    security: tuple[str, ...] = ()
    # What the spec puts between the host and `path`: `/api/v1`, or "" for a root-level path.
    prefix: str = ""

    @property
    def path_parameters(self) -> tuple[str, ...]:
        return tuple(_PATH_PARAMETER.findall(self.path))

    @property
    def label(self) -> str:
        """`<METHOD> <path>`: how Schemathesis names the operation in its reports."""
        return f"{self.method} {self.path}"


@lru_cache(maxsize=2)
def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    """The spec as written. Callers must not change it; it is shared."""
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=loader)


def resolve(spec: dict[str, Any], node: Any) -> Any:
    """Follow local `$ref`s until the node is not a reference."""
    seen: set[str] = set()
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if not ref.startswith("#/") or ref in seen:
            return {}
        seen.add(ref)
        node = spec
        for part in ref[2:].split("/"):
            node = (
                node.get(part.replace("~1", "/").replace("~0", "~"), {})
                if isinstance(node, dict)
                else {}
            )
    return node


def operation_definition(spec: dict[str, Any], operation: Operation) -> dict[str, Any]:
    return spec["paths"][operation.path][operation.method.lower()]


def operation_parameters(spec: dict[str, Any], operation: Operation) -> list[dict[str, Any]]:
    """The parameters of the operation and of its path, with references followed."""
    declared = [
        *(spec["paths"][operation.path].get("parameters") or []),
        *(operation_definition(spec, operation).get("parameters") or []),
    ]
    return [resolve(spec, parameter) for parameter in declared]


def _security(spec: dict[str, Any], definition: dict[str, Any]) -> tuple[str, ...]:
    requirements = definition.get("security", spec.get("security") or [])
    return tuple(sorted({name for requirement in requirements for name in requirement}))


def _prefix(*servers: Any) -> str:
    """The path of the first server URL of the nearest level that lists servers."""
    for level in servers:
        if level:
            url = _SERVER_VARIABLE.sub("", str(level[0].get("url") or ""))
            return urlsplit(url).path.rstrip("/")
    return ""


def all_operations(spec: dict[str, Any]) -> list[Operation]:
    operations: list[Operation] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            definition = item.get(method)
            if not isinstance(definition, dict):
                continue
            operation_id = definition.get("operationId")
            if not operation_id:
                raise ValueError(f"{method.upper()} {path} has no operationId in the spec")
            operations.append(
                Operation(
                    operation_id=operation_id,
                    method=method.upper(),
                    path=path,
                    sdk=bool(definition.get("x-pipeshub-sdk")),
                    security=_security(spec, definition),
                    prefix=_prefix(
                        definition.get("servers"), item.get("servers"), spec.get("servers")
                    ),
                )
            )
    return operations


def operations_in_scope(spec: dict[str, Any], include_path_regex: str) -> list[Operation]:
    pattern = re.compile(include_path_regex)
    return [operation for operation in all_operations(spec) if pattern.search(operation.path)]
