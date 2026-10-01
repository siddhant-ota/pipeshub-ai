"""Suite definition: which spec operations are in scope and how each one is run."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from env import CONTRACT_DIR, INTEGRATION_TESTS_DIR

REPO_ROOT = INTEGRATION_TESTS_DIR.parent
SPEC_PATH = REPO_ROOT / "backend/nodejs/apps/src/modules/api-docs/pipeshub-openapi.yaml"
SUITES_DIR = CONTRACT_DIR / "suites"

HTTP_METHODS = ("get", "post", "put", "patch", "delete")
_PATH_PARAM = re.compile(r"\{([^}]+)\}")

PROFILE_FULL = "full"
PROFILE_NEGATIVE_ONLY = "negative_only"
PROFILE_SKIP = "skip"


class SuiteError(Exception):
    """The suite file and the spec do not agree."""


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    tags: tuple[str, ...]
    sdk: bool
    streams: bool

    @property
    def path_parameters(self) -> tuple[str, ...]:
        return tuple(_PATH_PARAM.findall(self.path))

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True)
class PlannedOperation:
    operation: Operation
    profile: str
    reason: str = ""
    # path parameter name -> fixture key
    fixture_keys: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CreatedResource:
    """A resource that a successful call to `operation` leaves on the deployment."""

    operation: Operation
    id_pointer: str
    delete_path: str


@dataclass(frozen=True)
class Suite:
    name: str
    include_path_regex: str
    operations: tuple[PlannedOperation, ...]
    created_resources: tuple[CreatedResource, ...] = ()

    @property
    def fixture_keys(self) -> set[str]:
        return {
            key
            for planned in self.operations
            if planned.profile != PROFILE_SKIP
            for key in planned.fixture_keys.values()
        }


def load_spec(spec_path: Path = SPEC_PATH) -> dict[str, Any]:
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with open(spec_path, encoding="utf-8") as handle:
        return yaml.load(handle, Loader=loader)


def _operations_in_scope(spec: dict[str, Any], include_path_regex: str) -> list[Operation]:
    pattern = re.compile(include_path_regex)
    operations: list[Operation] = []
    for path, item in (spec.get("paths") or {}).items():
        if not pattern.search(path) or not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            definition = item.get(method)
            if not isinstance(definition, dict):
                continue
            operation_id = definition.get("operationId")
            if not operation_id:
                raise SuiteError(f"{method.upper()} {path} has no operationId in the spec")
            content_types = {
                content_type
                for response in (definition.get("responses") or {}).values()
                if isinstance(response, dict)
                for content_type in (response.get("content") or {})
            }
            operations.append(
                Operation(
                    operation_id=operation_id,
                    method=method.upper(),
                    path=path,
                    tags=tuple(definition.get("tags") or ()),
                    sdk=bool(definition.get("x-pipeshub-sdk")),
                    streams="text/event-stream" in content_types,
                )
            )
    return operations


def _fixture_keys_for(operation: Operation, rules: dict[str, Any]) -> dict[str, str]:
    overrides = (rules.get("operations") or {}).get(operation.operation_id) or {}
    defaults: dict[str, str] = {}
    for rule in rules.get("defaults") or []:
        if operation.path.startswith(rule["path_prefix"]):
            defaults = rule.get("values") or {}
            break
    return {
        name: overrides.get(name) or defaults.get(name) or ""
        for name in operation.path_parameters
    }


def load_suite(name: str, spec: dict[str, Any] | None = None) -> Suite:
    suite_path = SUITES_DIR / f"{name.replace('-', '_')}.yaml"
    if not suite_path.exists():
        available = ", ".join(sorted(p.stem.replace("_", "-") for p in SUITES_DIR.glob("*.yaml")))
        raise SuiteError(f"No suite named {name!r}. Available: {available}")
    raw = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
    spec = spec if spec is not None else load_spec()

    in_scope = _operations_in_scope(spec, raw["include_path_regex"])
    known_ids = {operation.operation_id for operation in in_scope}

    skip = {entry["operation"]: entry["reason"] for entry in raw.get("skip") or []}
    negative_only = raw.get("negative_only") or {}
    negative_ids = set(negative_only.get("operations") or [])
    rules = raw.get("path_parameters") or {}

    created = raw.get("created_resources") or []

    referenced = (
        set(skip)
        | negative_ids
        | set(rules.get("operations") or {})
        | {entry["operation"] for entry in created}
    )
    unknown = sorted(referenced - known_ids)
    if unknown:
        raise SuiteError(
            f"{suite_path.name} names operations that are not in scope of the spec: {', '.join(unknown)}"
        )

    planned: list[PlannedOperation] = []
    for operation in in_scope:
        if operation.operation_id in skip:
            planned.append(
                PlannedOperation(operation, PROFILE_SKIP, skip[operation.operation_id])
            )
            continue
        fixture_keys = _fixture_keys_for(operation, rules)
        unmapped = sorted(name for name, key in fixture_keys.items() if not key)
        if unmapped:
            raise SuiteError(
                f"{operation.operation_id} ({operation.label}) has no fixture for "
                f"path parameter(s): {', '.join(unmapped)}"
            )
        if operation.operation_id in negative_ids:
            planned.append(
                PlannedOperation(
                    operation,
                    PROFILE_NEGATIVE_ONLY,
                    negative_only.get("reason", ""),
                    fixture_keys,
                )
            )
        else:
            planned.append(PlannedOperation(operation, PROFILE_FULL, "", fixture_keys))

    by_id = {operation.operation_id: operation for operation in in_scope}
    return Suite(
        name=raw["name"],
        include_path_regex=raw["include_path_regex"],
        operations=tuple(planned),
        created_resources=tuple(
            CreatedResource(by_id[entry["operation"]], entry["id_pointer"], entry["delete_path"])
            for entry in created
        ),
    )
