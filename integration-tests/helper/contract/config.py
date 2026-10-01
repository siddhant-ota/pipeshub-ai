"""Decide what one run does with every operation, and write the Schemathesis config for it."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from helper.contract.suite import (
    PROFILE_NEGATIVE_ONLY,
    PROFILE_SKIP,
    PlannedOperation,
    Suite,
)
from helper.contract.values import ContractValues

CONTRACT_DIR = Path(__file__).resolve().parent
BASE_CONFIG_PATH = CONTRACT_DIR / "schemathesis.base.toml"
HOOKS_PATH = CONTRACT_DIR / "hooks.py"

# Environment of the Schemathesis process, read by hooks.py.
SUBSTITUTIONS_ENV = "CONTRACT_SUBSTITUTIONS"
# Set by `plan`, which talks to a stub and has nothing to log in to.
STATIC_AUTHORIZATION_ENV = "CONTRACT_STATIC_AUTHORIZATION"

# All valid and invalid cases are sent.
STATE_FULL = "full"
# Invalid cases only; the success response is not checked.
STATE_NEGATIVE_ONLY = "negative_only"
# The suite file skips the operation.
STATE_SKIPPED = "skipped"
# A value the operation needs could not be created.
STATE_VALUE_MISSING = "value_missing"
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
    # ID fields that keep a generated value; see results._names_nothing_real.
    waived_fields: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OperationRun:
        return cls(**{**data, "waived_fields": tuple(data.get("waived_fields") or ())})

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def is_sent(self) -> bool:
        return self.state in (STATE_FULL, STATE_NEGATIVE_ONLY)


def _state_for(
    planned: PlannedOperation, values: ContractValues, selected: set[str] | None
) -> tuple[str, str]:
    if selected is not None and planned.operation.operation_id not in selected:
        return STATE_DESELECTED, "Not selected for this run."
    if planned.profile == PROFILE_SKIP:
        return STATE_SKIPPED, planned.reason
    missing = sorted(key for key in planned.value_keys if key not in values.values)
    if missing:
        reasons = "; ".join(
            f"{key} ({values.missing.get(key, 'no fixture provides it')})" for key in missing
        )
        return STATE_VALUE_MISSING, f"Missing value: {reasons}"
    if planned.profile == PROFILE_NEGATIVE_ONLY:
        return STATE_NEGATIVE_ONLY, planned.reason
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
                waived_fields=planned.waived_fields,
            )
        )
    return runs


def build_config(suite: Suite, values: ContractValues, runs: list[OperationRun]) -> dict[str, Any]:
    config = tomllib.loads(BASE_CONFIG_PATH.read_text(encoding="utf-8"))
    config["hooks"] = str(HOOKS_PATH)

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
        if planned.path_values:
            block["parameters"] = {
                f"path.{name}": values.values[key] for name, key in planned.path_values.items()
            }
        if run.state == STATE_NEGATIVE_ONLY:
            block["generation"] = {"mode": "negative"}
            # The examples phase sends the spec's own examples, which are valid requests.
            block["phases"] = {"examples": {"enabled": False}}
        if len(block) > 1:
            blocks.append(block)
    config["operations"] = blocks
    return config


def build_substitutions(
    suite: Suite, values: ContractValues, runs: list[OperationRun]
) -> dict[str, dict[str, str]]:
    """Operation label -> request field -> value, for `hooks.py`."""
    sent = {run.operation_id for run in runs if run.is_sent}
    return {
        planned.operation.label: {
            field_name: values.values[key] for field_name, key in planned.field_values.items()
        }
        for planned in suite.operations
        if planned.operation.operation_id in sent and planned.field_values
    }


def write_config(config: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tomli_w.dumps(config).encode("utf-8"))


def write_substitutions(substitutions: dict[str, dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(substitutions, indent=2), encoding="utf-8")
