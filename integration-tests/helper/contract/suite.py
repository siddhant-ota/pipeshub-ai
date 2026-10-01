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

from helper.contract.fields import ANY_KEY, enumerated_path_parameters, request_fields
from helper.contract.spec import Operation, load_spec, operations_in_scope
from helper.contract.values import PATH

# Valid and invalid requests.
PROFILE_FULL = "full"
# Invalid requests, and of the valid ones only the examples that the spec gives.
PROFILE_EXAMPLES_ONLY = "examples_only"
# Invalid requests only.
PROFILE_NEGATIVE_ONLY = "negative_only"
PROFILE_SKIP = "skip"

# The token of the OAuth client that the integration-test fixtures use.
AUTH_OAUTH_CLIENT = "oauth_client"
# The session token of the test user, from a password login.
AUTH_SESSION = "session"
AUTH_NONE = "none"
# A bearer token that a fixture provides, for an operation that takes a special-purpose token.
AUTH_TOKEN = "token"
_LOGINS = (AUTH_OAUTH_CLIENT, AUTH_SESSION, AUTH_NONE)
# Security schemes of the spec that the two logins satisfy.
_OAUTH_SCHEME = "oauth2"
_SESSION_SCHEME = "bearerAuth"

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
    auth: str = AUTH_OAUTH_CLIENT
    # AUTH_TOKEN: the value key of the token.
    token_key: str = ""
    # For example `10/m`, for an operation with a stricter limit than the rest of the API.
    rate_limit: str = ""

    @property
    def value_keys(self) -> set[str]:
        keys = {*self.path_values.values(), *self.field_values.values()}
        return keys | {self.token_key} if self.token_key else keys


@dataclass(frozen=True)
class CreatedResource:
    """A resource that a successful call to `operation` leaves on the deployment."""

    operation: Operation
    id_pointer: str
    delete_path: str
    # How to log in to delete it: as for the operation that created it.
    auth: str = AUTH_OAUTH_CLIENT


@dataclass(frozen=True)
class Suite:
    name: str
    include_path_regex: str
    operations: tuple[PlannedOperation, ...]
    created_resources: tuple[CreatedResource, ...] = ()
    # What comes between the host and the paths of the suite: `/api/v1`, or "".
    api_prefix: str = ""
    # Replaces the rate limit of schemathesis.base.toml for the whole suite.
    rate_limit: str = ""
    # value key -> a value that the suite file itself gives
    constants: dict[str, str] = field(default_factory=dict)

    @property
    def value_keys(self) -> set[str]:
        return {
            key
            for planned in self.operations
            if planned.profile != PROFILE_SKIP
            for key in planned.value_keys
        }

    @property
    def fixture_keys(self) -> set[str]:
        """The value keys that a fixture must provide."""
        return self.value_keys - set(self.constants)

    @property
    def logins(self) -> set[str]:
        return {planned.auth for planned in self.operations if planned.profile != PROFILE_SKIP}


def _path_values(
    operation: Operation, rules: dict[str, Any], generated: frozenset[str]
) -> dict[str, str]:
    """Value key for each path parameter; "" for one that has none and needs one."""
    overrides = (rules.get("operations") or {}).get(operation.operation_id) or {}
    defaults: dict[str, str] = {}
    for rule in rules.get("defaults") or []:
        if operation.path.startswith(rule["path_prefix"]):
            defaults = rule.get("values") or {}
            break
    values = {
        name: overrides.get(name) or defaults.get(name) or "" for name in operation.path_parameters
    }
    return {name: key for name, key in values.items() if key or name not in generated}


def _profiles(raw: dict[str, Any], problems: list[str]) -> dict[str, tuple[str, str]]:
    """operationId -> (profile, reason) for every operation that is not run in full."""
    profiles: dict[str, tuple[str, str]] = {}
    for entry in raw.get("skip") or []:
        profiles[entry["operation"]] = (PROFILE_SKIP, entry["reason"])
    for profile in _REDUCED_PROFILES:
        for group in raw.get(profile) or []:
            for operation_id in group.get("operations") or []:
                if operation_id in profiles:
                    problems.append(f"{operation_id} is in `{profile}` and in another list")
                profiles[operation_id] = (profile, group["reason"])
    return profiles


def _fields_by_name(raw: dict[str, Any], key: str) -> dict[str, str]:
    return {entry["field"]: entry["reason"] for entry in raw.get(key) or []}


def _login(operation: Operation, override: Any, problems: list[str]) -> tuple[str, str]:
    """(way to log in, value key of the token) for one operation."""
    where = f"{operation.operation_id} ({operation.label})"
    if isinstance(override, dict) and set(override) == {AUTH_TOKEN} and override[AUTH_TOKEN]:
        return AUTH_TOKEN, str(override[AUTH_TOKEN])
    if override is not None:
        if override not in _LOGINS:
            problems.append(
                f"{where}: `auth` must be one of {', '.join(_LOGINS)} or "
                f"`{{{AUTH_TOKEN}: <value key>}}`, not {override!r}"
            )
        return str(override), ""
    if not operation.security:
        return AUTH_NONE, ""
    if _OAUTH_SCHEME in operation.security:
        return AUTH_OAUTH_CLIENT, ""
    if _SESSION_SCHEME in operation.security:
        return AUTH_SESSION, ""
    problems.append(
        f"{where}: the spec accepts only {', '.join(operation.security)}; say under `auth` "
        "how the test logs in, or skip the operation"
    )
    return AUTH_NONE, ""


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
    values_by_operation: dict[str, dict[str, str]] = raw.get("values_by_operation") or {}
    client_chosen = _fields_by_name(raw, _CLIENT_CHOSEN)
    without_fixture = _fields_by_name(raw, _WITHOUT_FIXTURE)
    logins: dict[str, Any] = raw.get("auth") or {}
    rate_limits: dict[str, str] = raw.get("operation_rate_limits") or {}

    referenced = {
        *profiles,
        *no_success,
        *values_by_operation,
        *logins,
        *rate_limits,
        *((raw.get("path_parameters") or {}).get("operations") or {}),
        *(entry["operation"] for entry in raw.get("created_resources") or []),
    }
    unknown = sorted(referenced - set(by_id))
    if unknown:
        problems.append(f"operations that are not in scope of the spec: {', '.join(unknown)}")
    prefixes = sorted({operation.prefix for operation in in_scope})
    if len(prefixes) > 1:
        problems.append(
            "the operations do not have one base path in the spec "
            f"({', '.join(repr(prefix) for prefix in prefixes)}); they need a suite each"
        )

    seen_fields: set[str] = set()
    planned: list[PlannedOperation] = []
    for operation in in_scope:
        operation_id = operation.operation_id
        fields = request_fields(spec, operation)
        names = {request_field.name for request_field in fields}
        seen_fields.update(names)
        own_values = values_by_operation.get(operation_id) or {}
        absent = sorted(set(own_values) - names)
        if absent:
            problems.append(
                f"{operation_id} ({operation.label}): `values_by_operation` names field(s) "
                f"it does not have: {', '.join(absent)}"
            )
        profile, reason = profiles.get(operation_id, (PROFILE_FULL, ""))
        if profile == PROFILE_SKIP:
            planned.append(PlannedOperation(operation, PROFILE_SKIP, reason))
            continue

        path_values = _path_values(
            operation,
            raw.get("path_parameters") or {},
            enumerated_path_parameters(spec, operation),
        )
        unmapped = sorted(name for name, key in path_values.items() if not key)
        if unmapped:
            problems.append(
                f"{operation_id} ({operation.label}): no value for path parameter(s) "
                f"{', '.join(unmapped)}"
            )
        decided = {*values, *own_values, *client_chosen, *without_fixture}
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
        auth, token_key = _login(operation, logins.get(operation_id), problems)

        planned.append(
            PlannedOperation(
                operation=operation,
                profile=profile,
                reason=reason,
                no_success_reason=no_success.get(operation_id, ""),
                path_values=path_values,
                field_values={
                    name: own_values.get(name) or values[name]
                    for name in sorted(names)
                    if name in own_values or name in values
                },
                fixtureless_fields=tuple(
                    request_field.name
                    for request_field in fields
                    if request_field.name in without_fixture
                    and request_field.name not in own_values
                ),
                auth=auth,
                token_key=token_key,
                rate_limit=str(rate_limits.get(operation_id) or ""),
            )
        )

    suite_wide = {*values, *client_chosen, *without_fixture}
    stale = sorted(suite_wide - seen_fields)
    if stale:
        problems.append(f"fields that no operation has: {', '.join(stale)}")
    for first, second in (
        (set(values), {*client_chosen, *without_fixture}),
        (set(client_chosen), set(without_fixture)),
    ):
        if first & second:
            problems.append(f"fields in more than one list: {', '.join(sorted(first & second))}")
    given = {*values, *(name for fields in values_by_operation.values() for name in fields)}
    free_form = sorted(name for name in given if ANY_KEY in name)
    if free_form:
        problems.append(
            "a field under a free-form key cannot get a value; list it under "
            f"`{_WITHOUT_FIXTURE}` instead: {', '.join(free_form)}"
        )
    in_the_path = sorted(name for name in given if name.startswith(f"{PATH}."))
    if in_the_path:
        problems.append(
            f"path parameters get their value under `path_parameters`: {', '.join(in_the_path)}"
        )

    if problems:
        raise SuiteError(f"{path} does not agree with the spec:\n- " + "\n- ".join(problems))

    auth_by_id = {entry.operation.operation_id: entry.auth for entry in planned}
    return Suite(
        name=raw["name"],
        include_path_regex=raw["include_path_regex"],
        operations=tuple(planned),
        created_resources=tuple(
            CreatedResource(
                by_id[entry["operation"]],
                entry["id_pointer"],
                entry["delete_path"],
                auth_by_id[entry["operation"]],
            )
            for entry in raw.get("created_resources") or []
        ),
        api_prefix=prefixes[0] if prefixes else "",
        rate_limit=str(raw.get("rate_limit") or ""),
        constants={key: str(value) for key, value in (raw.get("constants") or {}).items()},
    )
