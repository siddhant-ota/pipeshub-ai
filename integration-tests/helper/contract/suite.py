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

from helper.contract.fields import ANY_KEY, request_fields
from helper.contract.spec import Operation, load_spec, operations_in_scope

# Valid and invalid requests.
PROFILE_FULL = "full"
# Invalid requests, and of the valid ones only the examples that the spec gives.
PROFILE_EXAMPLES_ONLY = "examples_only"
# Invalid requests only.
PROFILE_NEGATIVE_ONLY = "negative_only"
PROFILE_SKIP = "skip"

# Suite keys that give an operation a profile other than "full".
_REDUCED_PROFILES = (PROFILE_NEGATIVE_ONLY, PROFILE_EXAMPLES_ONLY)
# Suite keys that say an ID field keeps the value Schemathesis generates.
_CLIENT_CHOSEN = "client_chosen_ids"
_WITHOUT_FIXTURE = "ids_without_fixture"


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
    # Fields that must name something that exists, and for which no fixture gives a value.
    fixtureless_fields: tuple[str, ...] = ()

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


def _profiles(raw: dict[str, Any], problems: list[str]) -> dict[str, tuple[str, str]]:
    """operationId -> (profile, reason) for every operation that is not run in full."""
    profiles: dict[str, tuple[str, str]] = {}
    for entry in raw.get("skip") or []:
        profiles[entry["operation"]] = (PROFILE_SKIP, entry["reason"])
    for profile in _REDUCED_PROFILES:
        section = raw.get(profile) or {}
        for operation_id in section.get("operations") or []:
            if operation_id in profiles:
                problems.append(f"{operation_id} is in `{profile}` and in another list")
            profiles[operation_id] = (profile, section.get("reason", ""))
    return profiles


def _fields_by_name(raw: dict[str, Any], key: str) -> dict[str, str]:
    return {entry["field"]: entry["reason"] for entry in raw.get(key) or []}


def load_suite(path: Path, spec: dict[str, Any] | None = None) -> Suite:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    spec = spec if spec is not None else load_spec()
    in_scope = operations_in_scope(spec, raw["include_path_regex"])
    by_id = {operation.operation_id: operation for operation in in_scope}
    problems: list[str] = []

    profiles = _profiles(raw, problems)
    no_success = {
        entry["operation"]: entry["reason"] for entry in raw.get("no_success_response") or []
    }
    values: dict[str, str] = raw.get("values") or {}
    client_chosen = _fields_by_name(raw, _CLIENT_CHOSEN)
    without_fixture = _fields_by_name(raw, _WITHOUT_FIXTURE)
    decided = {*values, *client_chosen, *without_fixture}

    referenced = {
        *profiles,
        *no_success,
        *((raw.get("path_parameters") or {}).get("operations") or {}),
        *(entry["operation"] for entry in raw.get("created_resources") or []),
    }
    unknown = sorted(referenced - set(by_id))
    if unknown:
        problems.append(f"operations that are not in scope of the spec: {', '.join(unknown)}")

    seen_fields: set[str] = set()
    planned: list[PlannedOperation] = []
    for operation in in_scope:
        operation_id = operation.operation_id
        fields = request_fields(spec, operation)
        seen_fields.update(request_field.name for request_field in fields)
        profile, reason = profiles.get(operation_id, (PROFILE_FULL, ""))
        if profile == PROFILE_SKIP:
            planned.append(PlannedOperation(operation, PROFILE_SKIP, reason))
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
            if request_field.is_id and request_field.name not in decided
        )
        if undecided:
            problems.append(
                f"{operation_id} ({operation.label}): ID field(s) with no entry in `values`, "
                f"`{_CLIENT_CHOSEN}` or `{_WITHOUT_FIXTURE}`: {', '.join(undecided)}"
            )

        planned.append(
            PlannedOperation(
                operation=operation,
                profile=profile,
                reason=reason,
                no_success_reason=no_success.get(operation_id, ""),
                path_values=path_values,
                field_values={
                    request_field.name: values[request_field.name]
                    for request_field in fields
                    if request_field.name in values
                },
                fixtureless_fields=tuple(
                    request_field.name
                    for request_field in fields
                    if request_field.name in without_fixture
                ),
            )
        )

    stale = sorted(decided - seen_fields)
    if stale:
        problems.append(f"fields that no operation has: {', '.join(stale)}")
    for first, second in (
        (set(values), {*client_chosen, *without_fixture}),
        (set(client_chosen), set(without_fixture)),
    ):
        if first & second:
            problems.append(f"fields in more than one list: {', '.join(sorted(first & second))}")
    free_form = sorted(name for name in values if ANY_KEY in name)
    if free_form:
        problems.append(
            "`values` cannot set a field under a free-form key; list it under "
            f"`{_WITHOUT_FIXTURE}` instead: {', '.join(free_form)}"
        )

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
