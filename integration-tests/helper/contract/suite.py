"""A suite: the operations of one area of the spec and how each one is tested.

The suite file sits next to the contract test that uses it. Loading it checks
it against the spec and reports every disagreement at once, so a spec change
that the suite does not cover fails here, before any request is sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from helper.contract.fields import request_fields
from helper.contract.spec import Operation, load_spec, operations_in_scope

PROFILE_FULL = "full"
PROFILE_NEGATIVE_ONLY = "negative_only"
PROFILE_SKIP = "skip"


class SuiteError(Exception):
    """The suite file and the spec do not agree."""


@dataclass(frozen=True)
class PlannedOperation:
    operation: Operation
    profile: str
    # Why the operation is not run in full; empty for PROFILE_FULL.
    reason: str = ""
    # Why no request to this operation can get a 2xx; empty when one is expected.
    no_success_reason: str = ""
    # path parameter name -> value key
    path_values: dict[str, str] = field(default_factory=dict)
    # request field (`body.filters.kb[*]`) -> value key
    field_values: dict[str, str] = field(default_factory=dict)
    # ID fields of this operation that keep a generated value
    waived_fields: tuple[str, ...] = ()

    @property
    def value_keys(self) -> set[str]:
        return {*self.path_values.values(), *self.field_values.values()}


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
    def value_keys(self) -> set[str]:
        return {
            key
            for planned in self.operations
            if planned.profile != PROFILE_SKIP
            for key in planned.value_keys
        }


def _path_values(operation: Operation, rules: dict[str, Any]) -> dict[str, str]:
    overrides = (rules.get("operations") or {}).get(operation.operation_id) or {}
    defaults: dict[str, str] = {}
    for rule in rules.get("defaults") or []:
        if operation.path.startswith(rule["path_prefix"]):
            defaults = rule.get("values") or {}
            break
    return {
        name: overrides.get(name) or defaults.get(name) or "" for name in operation.path_parameters
    }


def _unknown_operations(raw: dict[str, Any], known: set[str]) -> list[str]:
    referenced = {
        *(entry["operation"] for entry in raw.get("skip") or []),
        *(entry["operation"] for entry in raw.get("no_success_response") or []),
        *(entry["operation"] for entry in raw.get("created_resources") or []),
        *((raw.get("negative_only") or {}).get("operations") or []),
        *((raw.get("path_parameters") or {}).get("operations") or {}),
    }
    return sorted(referenced - known)


def load_suite(path: Path, spec: dict[str, Any] | None = None) -> Suite:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    spec = spec if spec is not None else load_spec()
    in_scope = operations_in_scope(spec, raw["include_path_regex"])
    by_id = {operation.operation_id: operation for operation in in_scope}
    problems: list[str] = []

    unknown = _unknown_operations(raw, set(by_id))
    if unknown:
        problems.append(f"operations that are not in scope of the spec: {', '.join(unknown)}")

    skip = {entry["operation"]: entry["reason"] for entry in raw.get("skip") or []}
    no_success = {
        entry["operation"]: entry["reason"] for entry in raw.get("no_success_response") or []
    }
    negative_only = raw.get("negative_only") or {}
    negative_ids = set(negative_only.get("operations") or [])
    values: dict[str, str] = raw.get("values") or {}
    waived = {entry["field"]: entry["reason"] for entry in raw.get("waived_id_fields") or []}

    seen_fields: set[str] = set()
    planned: list[PlannedOperation] = []
    for operation in in_scope:
        operation_id = operation.operation_id
        fields = request_fields(spec, operation)
        seen_fields.update(request_field.name for request_field in fields)
        if operation_id in skip:
            planned.append(PlannedOperation(operation, PROFILE_SKIP, skip[operation_id]))
            continue

        path_values = _path_values(operation, raw.get("path_parameters") or {})
        unmapped = sorted(name for name, key in path_values.items() if not key)
        if unmapped:
            problems.append(
                f"{operation_id} ({operation.label}): no value for path parameter(s) "
                f"{', '.join(unmapped)}"
            )
        undecided = sorted(
            request_field.name
            for request_field in fields
            if request_field.is_id
            and request_field.name not in values
            and request_field.name not in waived
        )
        if undecided:
            problems.append(
                f"{operation_id} ({operation.label}): ID field(s) with no entry in `values` "
                f"or `waived_id_fields`: {', '.join(undecided)}"
            )

        is_negative_only = operation_id in negative_ids
        planned.append(
            PlannedOperation(
                operation=operation,
                profile=PROFILE_NEGATIVE_ONLY if is_negative_only else PROFILE_FULL,
                reason=negative_only.get("reason", "") if is_negative_only else "",
                no_success_reason=no_success.get(operation_id, ""),
                path_values=path_values,
                field_values={
                    request_field.name: values[request_field.name]
                    for request_field in fields
                    if request_field.name in values
                },
                waived_fields=tuple(
                    request_field.name for request_field in fields if request_field.name in waived
                ),
            )
        )

    stale = sorted((set(values) | set(waived)) - seen_fields)
    if stale:
        problems.append(
            f"`values` or `waived_id_fields` name fields that no operation has: {', '.join(stale)}"
        )
    both = sorted(set(values) & set(waived))
    if both:
        problems.append(f"fields in both `values` and `waived_id_fields`: {', '.join(both)}")

    if problems:
        raise SuiteError(f"{path} does not agree with the spec:\n- " + "\n- ".join(problems))

    return Suite(
        name=raw["name"],
        include_path_regex=raw["include_path_regex"],
        operations=tuple(planned),
        created_resources=tuple(
            CreatedResource(by_id[entry["operation"]], entry["id_pointer"], entry["delete_path"])
            for entry in raw.get("created_resources") or []
        ),
    )
