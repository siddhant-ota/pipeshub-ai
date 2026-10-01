"""Build the Schemathesis config for one run from the base file, the suite and the fixtures."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w
from suite import (
    CONTRACT_DIR,
    PROFILE_NEGATIVE_ONLY,
    PROFILE_SKIP,
    PlannedOperation,
    Suite,
)

BASE_CONFIG_PATH = CONTRACT_DIR / "schemathesis.base.toml"
HOOKS_PATH = CONTRACT_DIR / "hooks.py"

STATE_TESTED = "tested"
STATE_NEGATIVE_ONLY = "negative_only"
STATE_SKIPPED = "skipped"
STATE_FIXTURE_MISSING = "fixture_missing"
STATE_FILTERED = "filtered"


@dataclass(frozen=True)
class OperationRun:
    """What one run does with one operation, and why."""

    operation_id: str
    method: str
    path: str
    sdk: bool
    streams: bool
    state: str
    reason: str = ""

    @property
    def is_sent(self) -> bool:
        return self.state in (STATE_TESTED, STATE_NEGATIVE_ONLY)


def _state_for(
    planned: PlannedOperation,
    fixtures: dict[str, str],
    *,
    read_only: bool,
    only: set[str],
) -> tuple[str, str]:
    operation = planned.operation
    if only and operation.operation_id not in only:
        return STATE_FILTERED, "Not selected with --only."
    if planned.profile == PROFILE_SKIP:
        return STATE_SKIPPED, planned.reason
    if read_only and operation.method != "GET":
        return STATE_FILTERED, "Not a GET operation (--read-only)."
    missing = sorted(key for key in planned.fixture_keys.values() if key not in fixtures)
    if missing:
        return STATE_FIXTURE_MISSING, f"Fixture missing: {', '.join(missing)}"
    if planned.profile == PROFILE_NEGATIVE_ONLY:
        return STATE_NEGATIVE_ONLY, planned.reason
    return STATE_TESTED, ""


def plan_run(
    suite: Suite,
    fixtures: dict[str, str],
    *,
    read_only: bool = False,
    only: set[str] | None = None,
) -> list[OperationRun]:
    runs: list[OperationRun] = []
    for planned in suite.operations:
        state, reason = _state_for(planned, fixtures, read_only=read_only, only=only or set())
        operation = planned.operation
        runs.append(
            OperationRun(
                operation_id=operation.operation_id,
                method=operation.method,
                path=operation.path,
                sdk=operation.sdk,
                streams=operation.streams,
                state=state,
                reason=reason,
            )
        )
    return runs


def build_config(
    suite: Suite,
    fixtures: dict[str, str],
    runs: list[OperationRun],
    *,
    fuzz: bool = False,
    llm_examples: bool = False,
) -> dict[str, Any]:
    config = tomllib.loads(BASE_CONFIG_PATH.read_text(encoding="utf-8"))
    config["hooks"] = str(HOOKS_PATH)
    if fuzz:
        config.setdefault("phases", {}).setdefault("fuzzing", {})["enabled"] = True

    state_by_id = {run.operation_id: run for run in runs}
    blocks: list[dict[str, Any]] = []
    for planned in suite.operations:
        operation_id = planned.operation.operation_id
        run = state_by_id[operation_id]
        block: dict[str, Any] = {"include-operation-id": operation_id}
        if not run.is_sent:
            block["enabled"] = False
            blocks.append(block)
            continue
        if planned.fixture_keys:
            block["parameters"] = {
                f"path.{name}": fixtures[key] for name, key in planned.fixture_keys.items()
            }
        if run.state == STATE_NEGATIVE_ONLY:
            block["generation"] = {"mode": "negative"}
            # The examples phase sends the spec's own examples, which are valid requests.
            if not llm_examples:
                block["phases"] = {"examples": {"enabled": False}}
        if len(block) > 1:
            blocks.append(block)
    config["operations"] = blocks
    return config


def write_config(config: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tomli_w.dumps(config).encode("utf-8"))
