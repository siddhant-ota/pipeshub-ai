"""Decide what one run does with every operation, and write the Schemathesis config for it."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from helper.contract.suite import (
    AUTH_OAUTH_CLIENT,
    AUTH_TOKEN,
    PROFILE_EXAMPLES_ONLY,
    PROFILE_NEGATIVE_ONLY,
    PROFILE_SKIP,
    PlannedOperation,
    Suite,
)
from helper.contract.values import PATH, ContractValues, ValueOrPool, parse_field

CONTRACT_DIR = Path(__file__).resolve().parent
BASE_CONFIG_PATH = CONTRACT_DIR / "schemathesis.base.toml"
HOOKS_PATH = CONTRACT_DIR / "hooks.py"

# Environment of the Schemathesis process, read by hooks.py.
SUBSTITUTIONS_ENV = "CONTRACT_SUBSTITUTIONS"
FILES_ENV = "CONTRACT_FILES"
HEADERS_ENV = "CONTRACT_HEADERS"
# The operations whose valid requests are limited; see hooks._is_valid_in_disguise.
LIMITED_ENV = "CONTRACT_LIMITED"
LOGINS_ENV = "CONTRACT_LOGINS"
# The tokens that fixtures provide. In the environment, not in a file: they are credentials.
TOKENS_ENV = "CONTRACT_TOKENS"
BASE_URL_ENV = "CONTRACT_BASE_URL"
# Set by `plan`, which talks to a stub and has nothing to log in to.
STATIC_AUTHORIZATION_ENV = "CONTRACT_STATIC_AUTHORIZATION"

# All valid and invalid cases are sent.
STATE_FULL = "full"
# All invalid cases; of the valid ones only the examples that the spec gives.
STATE_EXAMPLES_ONLY = "examples_only"
# Invalid cases only; the success response is not checked.
STATE_NEGATIVE_ONLY = "negative_only"
# The suite file skips the operation.
STATE_SKIPPED = "skipped"
# A value or a login that the operation needs is not there.
STATE_VALUE_MISSING = "value_missing"
# A fixture skipped: this deployment cannot give the operation what it needs.
STATE_UNAVAILABLE = "unavailable"
# Not selected for this run.
STATE_DESELECTED = "deselected"


@dataclass(frozen=True)
class OperationRun:
    """What one run does with one operation, and why."""

    operation_id: str
    method: str
    path: str
    sdk: bool
    state: str
    reason: str = ""
    no_success_reason: str = ""
    # Fields that must name something that exists and have no fixture value;
    # see results._names_nothing_real.
    fixtureless_fields: tuple[str, ...] = ()
    auth: str = AUTH_OAUTH_CLIENT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OperationRun:
        return cls(**{**data, "fixtureless_fields": tuple(data.get("fixtureless_fields") or ())})

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def is_sent(self) -> bool:
        return self.state in (STATE_FULL, STATE_EXAMPLES_ONLY, STATE_NEGATIVE_ONLY)


def _state_for(
    planned: PlannedOperation, values: ContractValues, selected: set[str] | None
) -> tuple[str, str]:
    if selected is not None and planned.operation.operation_id not in selected:
        return STATE_DESELECTED, "Not selected for this run."
    if planned.profile == PROFILE_SKIP:
        return STATE_SKIPPED, planned.reason
    if planned.auth in values.no_login:
        return STATE_VALUE_MISSING, f"No `{planned.auth}` login: {values.no_login[planned.auth]}"
    missing = sorted(key for key in planned.value_keys if key not in values.values)
    failed = [key for key in missing if key not in values.unavailable]
    if failed:
        reasons = "; ".join(
            f"{key} ({values.missing.get(key, 'no fixture provides it')})" for key in failed
        )
        return STATE_VALUE_MISSING, f"Missing value: {reasons}"
    # Only now: an operation with a broken fixture fails, also when another one skipped.
    if planned.auth in values.unavailable_login:
        return STATE_UNAVAILABLE, (
            f"No `{planned.auth}` login here: {values.unavailable_login[planned.auth]}"
        )
    if missing:
        reasons = "; ".join(dict.fromkeys(values.unavailable[key] for key in missing))
        return STATE_UNAVAILABLE, f"Not possible on this deployment: {reasons}"
    if planned.profile == PROFILE_NEGATIVE_ONLY:
        return STATE_NEGATIVE_ONLY, planned.reason
    if planned.profile == PROFILE_EXAMPLES_ONLY:
        return STATE_EXAMPLES_ONLY, planned.reason
    return STATE_FULL, ""


def plan_run(
    suite: Suite, values: ContractValues, selected: set[str] | None = None
) -> list[OperationRun]:
    """`selected` is a set of operation IDs, or None for the whole suite."""
    runs: list[OperationRun] = []
    for planned in suite.operations:
        state, reason = _state_for(planned, values, selected)
        operation = planned.operation
        runs.append(
            OperationRun(
                operation_id=operation.operation_id,
                method=operation.method,
                path=operation.path,
                sdk=operation.sdk,
                state=state,
                reason=reason,
                no_success_reason=planned.no_success_reason,
                fixtureless_fields=planned.fixtureless_fields,
                auth=planned.auth,
            )
        )
    return runs


def build_config(suite: Suite, runs: list[OperationRun]) -> dict[str, Any]:
    config = tomllib.loads(BASE_CONFIG_PATH.read_text(encoding="utf-8"))
    config["hooks"] = str(HOOKS_PATH)
    if suite.rate_limit:
        config["rate-limit"] = suite.rate_limit

    run_by_id = {run.operation_id: run for run in runs}
    blocks: list[dict[str, Any]] = []
    for planned in suite.operations:
        operation_id = planned.operation.operation_id
        run = run_by_id[operation_id]
        block: dict[str, Any] = {"include-operation-id": operation_id}
        if not run.is_sent:
            block["enabled"] = False
            blocks.append(block)
            continue
        if planned.rate_limit:
            block["rate-limit"] = planned.rate_limit
        if planned.request_timeout:
            block["request-timeout"] = planned.request_timeout
        if run.state in (STATE_NEGATIVE_ONLY, STATE_EXAMPLES_ONLY):
            # This limits the coverage phase to invalid requests. The examples phase still
            # sends the spec's own examples, which are valid requests. (A mode set for the
            # coverage phase alone has no effect: Schemathesis 4.28 builds the coverage cases
            # from the operation's generation settings, without the phase.)
            block["generation"] = {"mode": "negative"}
        if run.state == STATE_NEGATIVE_ONLY:
            block["phases"] = {"examples": {"enabled": False}}
        if len(block) > 1:
            blocks.append(block)
    config["operations"] = blocks
    return config


def passes(suite: Suite, runs: list[OperationRun]) -> list[set[str]]:
    """The operations to send, in the groups to send them in: first the others, then the `last`.

    Schemathesis sends operations in the order of the spec. An operation that removes what
    others need ("delete all ...") must come after them, so it gets a pass of its own.
    """
    last = {planned.operation.operation_id for planned in suite.operations if planned.last}
    sent = {run.operation_id for run in runs if run.is_sent}
    return [group for group in (sent - last, sent & last) if group]


def only(config: dict[str, Any], suite: Suite, operation_ids: set[str]) -> dict[str, Any]:
    """`config` with every operation switched off that is not in `operation_ids`."""
    blocks = {block["include-operation-id"]: block for block in config["operations"]}
    for planned in suite.operations:
        operation_id = planned.operation.operation_id
        if operation_id not in operation_ids:
            blocks[operation_id] = {"include-operation-id": operation_id, "enabled": False}
    return {**config, "operations": list(blocks.values())}


def _sent(suite: Suite, runs: list[OperationRun]) -> list[PlannedOperation]:
    sent = {run.operation_id for run in runs if run.is_sent}
    return [planned for planned in suite.operations if planned.operation.operation_id in sent]


def build_limited(runs: list[OperationRun]) -> list[str]:
    """The labels of the operations whose valid requests are limited."""
    return [run.label for run in runs if run.state in (STATE_NEGATIVE_ONLY, STATE_EXAMPLES_ONLY)]


def build_substitutions(
    suite: Suite, values: ContractValues, runs: list[OperationRun]
) -> dict[str, dict[str, ValueOrPool]]:
    """Operation label -> request field -> value, for `hooks.py`."""
    substitutions: dict[str, dict[str, ValueOrPool]] = {}
    for planned in _sent(suite, runs):
        fields = {
            **{f"{PATH}.{name}": key for name, key in planned.path_values.items()},
            **{
                name: key
                for name, key in planned.field_values.items()
                if name not in planned.file_fields
            },
        }
        if fields:
            substitutions[planned.operation.label] = {
                field_name: values.values[key] for field_name, key in fields.items()
            }
    return substitutions


def build_files(
    suite: Suite, values: ContractValues, runs: list[OperationRun]
) -> dict[str, dict[str, str]]:
    """Operation label -> form field of its multipart body -> path of the file to send in it."""
    return {
        planned.operation.label: {
            parse_field(name)[1][0]: str(values.values[planned.field_values[name]])
            for name in planned.file_fields
        }
        for planned in _sent(suite, runs)
        if planned.file_fields
    }


def build_headers(
    suite: Suite, values: ContractValues, runs: list[OperationRun]
) -> dict[str, dict[str, str]]:
    """Operation label -> the headers that the suite gives it."""
    return {
        planned.operation.label: {
            name: str(values.values[key]) for name, key in planned.header_values.items()
        }
        for planned in _sent(suite, runs)
        if planned.header_values
    }


def build_logins(suite: Suite, runs: list[OperationRun]) -> dict[str, str]:
    """Operation label -> how the request logs in, for `hooks.py`."""
    return {planned.operation.label: planned.auth for planned in _sent(suite, runs)}


def build_tokens(suite: Suite, values: ContractValues, runs: list[OperationRun]) -> dict[str, str]:
    """Operation label -> the token a fixture provides for it."""
    return {
        planned.operation.label: str(values.values[planned.token_key])
        for planned in _sent(suite, runs)
        if planned.auth == AUTH_TOKEN
    }


def write_config(config: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tomli_w.dumps(config).encode("utf-8"))


def write_json(content: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2), encoding="utf-8")
