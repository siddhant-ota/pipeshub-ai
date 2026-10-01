"""Run Schemathesis for a suite and collect what it found."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import schemathesis

from helper.contract.baseline import load_baseline
from helper.contract.config import (
    STATIC_AUTHORIZATION_ENV,
    SUBSTITUTIONS_ENV,
    OperationRun,
    build_config,
    build_substitutions,
    plan_run,
    write_config,
    write_substitutions,
)
from helper.contract.events import API_PREFIX
from helper.contract.report import write_plan, write_report
from helper.contract.results import OperationResult, collect
from helper.contract.spec import SPEC_PATH
from helper.contract.stub_server import stub_server
from helper.contract.suite import Suite
from helper.contract.values import ContractValues

INTEGRATION_TESTS_DIR = Path(__file__).resolve().parents[2]
REPORTS_DIR = INTEGRATION_TESTS_DIR / "reports" / "contract"

# 0: every check passed. 1: some failed. Anything else: Schemathesis could not run.
_COMPLETED_EXIT_CODES = (0, 1)
_PLACEHOLDER = "0" * 24


class RunnerError(Exception):
    """Schemathesis did not complete, so there are no results to judge."""


@dataclass(frozen=True)
class RunFiles:
    directory: Path

    @property
    def config(self) -> Path:
        return self.directory / "schemathesis.toml"

    @property
    def substitutions(self) -> Path:
        return self.directory / "substitutions.json"

    @property
    def manifest(self) -> Path:
        return self.directory / "manifest.json"

    @property
    def events(self) -> Path:
        return self.directory / "events.ndjson"

    @property
    def summary(self) -> Path:
        return self.directory / "schemathesis.json"

    @property
    def har(self) -> Path:
        return self.directory / "requests.har"

    @property
    def log(self) -> Path:
        return self.directory / "schemathesis.log"

    @property
    def report(self) -> Path:
        return self.directory / "report.md"


@dataclass(frozen=True)
class ContractRun:
    suite: Suite
    files: RunFiles
    meta: dict[str, Any]
    results: tuple[OperationResult, ...]

    def result_for(self, operation_id: str) -> OperationResult:
        return next(result for result in self.results if result.run.operation_id == operation_id)


def run_files(suite: Suite, kind: str = "run") -> RunFiles:
    directory = REPORTS_DIR / suite.name / kind
    directory.mkdir(parents=True, exist_ok=True)
    return RunFiles(directory)


def _spec_commit() -> str:
    try:
        return subprocess.run(
            ["git", "log", "-1", "--format=%h", "--", str(SPEC_PATH)],
            cwd=SPEC_PATH.parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _meta(suite: Suite, target: str, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "suite": suite.name,
        "target": target,
        "time": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "schemathesis": schemathesis.__version__,
        "seed": config.get("seed"),
        "spec_commit": _spec_commit(),
    }


def _schemathesis_command() -> str:
    command = Path(sys.executable).parent / "schemathesis"
    if not command.exists():
        raise RunnerError(
            f"{command} does not exist. Install the integration-test dependencies: uv pip install -e ."
        )
    return str(command)


def _run_schemathesis(suite: Suite, files: RunFiles, *, api_url: str, env: dict[str, str]) -> None:
    command = [
        _schemathesis_command(),
        "--config-file",
        str(files.config),
        "run",
        str(SPEC_PATH),
        "--url",
        api_url,
        "--include-path-regex",
        suite.include_path_regex,
        # Without it a scenario stops at its first failing case and the rest go unreported.
        "--continue-on-failure",
        "--report",
        "ndjson,json,har",
        "--report-ndjson-path",
        str(files.events),
        "--report-json-path",
        str(files.summary),
        "--report-har-path",
        str(files.har),
        "--no-color",
    ]
    for stale in (files.events, files.summary, files.har):
        stale.unlink(missing_ok=True)
    with open(files.log, "w", encoding="utf-8") as log:
        code = subprocess.run(
            command,
            cwd=files.directory,
            env={**os.environ, **env},
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        ).returncode
    if code not in _COMPLETED_EXIT_CODES:
        tail = "\n".join(files.log.read_text(encoding="utf-8").splitlines()[-30:])
        raise RunnerError(f"Schemathesis exited with code {code}. Log: {files.log}\n{tail}")
    summary = json.loads(files.summary.read_text(encoding="utf-8"))
    if not summary.get("complete"):
        raise RunnerError(
            f"Schemathesis stopped early ({summary.get('stop_reason')}). Log: {files.log}"
        )


def _prepare(
    suite: Suite, values: ContractValues, files: RunFiles, selected: set[str] | None
) -> tuple[list[OperationRun], dict[str, Any]]:
    runs = plan_run(suite, values, selected)
    config = build_config(suite, values, runs)
    write_config(config, files.config)
    write_substitutions(build_substitutions(suite, values, runs), files.substitutions)
    return runs, config


def execute(
    suite: Suite,
    values: ContractValues,
    *,
    base_url: str,
    baseline_path: Path | None = None,
    selected: set[str] | None = None,
) -> ContractRun:
    """Send the suite's test cases to the deployment at `base_url` and judge the answers."""
    files = run_files(suite)
    runs, config = _prepare(suite, values, files, selected)
    meta = _meta(suite, f"{base_url}{API_PREFIX}", config)
    files.manifest.write_text(
        json.dumps({"meta": meta, "runs": [asdict(run) for run in runs]}, indent=2),
        encoding="utf-8",
    )
    if any(run.is_sent for run in runs):
        _run_schemathesis(
            suite,
            files,
            api_url=f"{base_url}{API_PREFIX}",
            env={SUBSTITUTIONS_ENV: str(files.substitutions)},
        )
    return judge(suite, baseline_path)


def judge(suite: Suite, baseline_path: Path | None = None) -> ContractRun:
    """Read the last run of `suite` from disk and write its report. Sends nothing."""
    files = run_files(suite)
    if not files.manifest.exists():
        raise RunnerError(f"No run of suite {suite.name!r} found in {files.directory}.")
    saved = json.loads(files.manifest.read_text(encoding="utf-8"))
    runs = [OperationRun(**entry) for entry in saved["runs"]]
    baseline = load_baseline(baseline_path) if baseline_path else set()
    results = collect(runs, files.events, baseline)
    write_report(results, saved["meta"], files.directory)
    return ContractRun(suite, files, saved["meta"], tuple(results))


def plan(suite: Suite, selected: set[str] | None = None) -> Path:
    """List the test cases Schemathesis generates for the suite. Needs no deployment.

    The cases go to a local stub that answers `{}`, so no check is switched on:
    every response check would fail and say nothing.
    """
    files = run_files(suite, "plan")
    values = ContractValues(values=dict.fromkeys(suite.value_keys, _PLACEHOLDER))
    runs, config = _prepare(suite, values, files, selected)
    config.pop("rate-limit", None)
    config["checks"] = {"enabled": False}
    write_config(config, files.config)
    with stub_server() as url:
        _run_schemathesis(
            suite,
            files,
            api_url=f"{url}{API_PREFIX}",
            env={
                SUBSTITUTIONS_ENV: str(files.substitutions),
                STATIC_AUTHORIZATION_ENV: "Bearer plan",
            },
        )
    return write_plan(runs, files.events, _meta(suite, "local stub", config), files.directory)
