"""Run Schemathesis for a suite and collect what it found."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from helper.contract.baseline import load_baseline
from helper.contract.config import (
    BASE_URL_ENV,
    FILES_ENV,
    HEADERS_ENV,
    LOGINS_ENV,
    STATIC_AUTHORIZATION_ENV,
    SUBSTITUTIONS_ENV,
    TOKENS_ENV,
    OperationRun,
    build_config,
    build_files,
    build_headers,
    build_logins,
    build_substitutions,
    build_tokens,
    plan_run,
    write_config,
    write_json,
)
from helper.contract.redaction import redact_run_files
from helper.contract.report import write_plan, write_report
from helper.contract.results import OperationResult, collect
from helper.contract.sources import SECRET, FixtureRow
from helper.contract.spec import SPEC_PATH
from helper.contract.stub_server import stub_server
from helper.contract.suite import Suite
from helper.contract.values import ContractValues

INTEGRATION_TESTS_DIR = Path(__file__).resolve().parents[2]
# A suite is a folder named `contract` with these files, under this root.
SUITES_ROOT = INTEGRATION_TESTS_DIR / "response-validation"
SUITE_NAME = "suite.yaml"
BASELINE_NAME = "baseline.json"
# Where runs are written. CONTRACT_REPORTS_DIR moves it, for example to a CI artifact folder.
REPORTS_DIR = Path(
    os.getenv("CONTRACT_REPORTS_DIR") or INTEGRATION_TESTS_DIR / "reports" / "contract"
)
# One page for the last run of every suite.
INDEX_PATH = REPORTS_DIR / "index.md"

# 0: every check passed. 1: some failed. Anything else: Schemathesis could not run.
_COMPLETED_EXIT_CODES = (0, 1)
# `stop_reason` of a run in which every selected operation went through every phase.
_RAN_TO_THE_END = "completed"
_PLACEHOLDER = "0" * 24
# For a value that goes where the spec gives a format; the plain placeholder there would make
# every valid case of the operation an invalid one in the plan.
_PLACEHOLDER_BY_FORMAT = {
    "uuid": "00000000-0000-4000-8000-000000000000",
    "email": "contract-placeholder@example.com",
    "uri": "https://contract-placeholder.example.com/",
    "date-time": "2000-01-01T00:00:00Z",
}


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


def suite_paths() -> list[Path]:
    """The suite file of every contract suite in the repository."""
    return sorted(SUITES_ROOT.glob(f"**/contract/{SUITE_NAME}"))


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
        "schemathesis": version("schemathesis"),
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
    # `complete` in the summary only says that the engine exited by itself. It is also true when
    # the engine gave up, for example because the API stopped answering.
    stop_reason = json.loads(files.summary.read_text(encoding="utf-8")).get("stop_reason")
    if stop_reason != _RAN_TO_THE_END:
        raise RunnerError(
            f"Schemathesis stopped early ({stop_reason}), so its results are not complete. "
            f"Log: {files.log}"
        )


def _discard_previous_run(files: RunFiles) -> None:
    """Remove the results of the last run first, so that a run that fails to start cannot be
    judged, or cleaned up after, with them."""
    for stale in (files.manifest, files.events, files.summary, files.har):
        stale.unlink(missing_ok=True)


def _prepare(
    suite: Suite, values: ContractValues, files: RunFiles, selected: set[str] | None
) -> tuple[list[OperationRun], dict[str, Any], dict[str, str]]:
    """Plan the run and write its config. Returns the runs, the config and the hook environment."""
    runs = plan_run(suite, values, selected)
    config = build_config(suite, runs)
    write_config(config, files.config)
    substitutions = build_substitutions(suite, values, runs)
    secrets = {values.values[key] for key in values.secret if key in values.values}
    # For reading. The hooks get the values through the environment, where a credential
    # among them does not end up in a report folder.
    write_json(
        {
            label: {name: SECRET if value in secrets else value for name, value in fields.items()}
            for label, fields in substitutions.items()
        },
        files.substitutions,
    )
    env = {
        SUBSTITUTIONS_ENV: json.dumps(substitutions),
        FILES_ENV: json.dumps(build_files(suite, values, runs)),
        HEADERS_ENV: json.dumps(build_headers(suite, values, runs)),
        LOGINS_ENV: json.dumps(build_logins(suite, runs)),
        TOKENS_ENV: json.dumps(build_tokens(suite, values, runs)),
    }
    return runs, config, env


def execute(
    suite: Suite,
    values: ContractValues,
    *,
    base_url: str,
    baseline_path: Path | None = None,
    selected: set[str] | None = None,
    fixtures: list[FixtureRow] | None = None,
) -> ContractRun:
    """Send the suite's test cases to the deployment at `base_url` and judge the answers."""
    files = run_files(suite)
    _discard_previous_run(files)
    runs, config, env = _prepare(suite, values, files, selected)
    absent = sorted(
        path
        for fields in json.loads(env[FILES_ENV]).values()
        for path in fields.values()
        if not Path(path).is_file()
    )
    if absent:
        raise RunnerError(f"A fixture gave a file that does not exist: {', '.join(absent)}")
    api_url = f"{base_url}{suite.api_prefix}"
    write_json(
        {
            "meta": _meta(suite, api_url, config),
            "runs": [asdict(run) for run in runs],
            "fixtures": [asdict(row.with_values(values)) for row in fixtures or []],
        },
        files.manifest,
    )
    if any(run.is_sent for run in runs):
        try:
            _run_schemathesis(suite, files, api_url=api_url, env={**env, BASE_URL_ENV: base_url})
        finally:
            secrets = {str(values.values[key]) for key in values.secret if key in values.values}
            redact_run_files((files.events, files.har), secrets)
    return judge(suite, baseline_path)


def judge(suite: Suite, baseline_path: Path | None = None) -> ContractRun:
    """Read the last run of `suite` from disk and write its report. Sends nothing."""
    files = run_files(suite)
    if not files.manifest.exists():
        raise RunnerError(f"No run of suite {suite.name!r} found in {files.directory}.")
    saved = json.loads(files.manifest.read_text(encoding="utf-8"))
    runs = [OperationRun.from_dict(entry) for entry in saved["runs"]]
    operation_ids = {planned.operation.operation_id for planned in suite.operations}
    baseline = load_baseline(baseline_path, operation_ids) if baseline_path else set()
    results = collect(runs, files.events, baseline)
    fixtures = [FixtureRow.from_dict(row) for row in saved.get("fixtures") or []]
    write_report(results, saved["meta"], fixtures, files.directory)
    return ContractRun(suite, files, saved["meta"], tuple(results))


def plan(
    suite: Suite, selected: set[str] | None = None, fixtures: list[FixtureRow] | None = None
) -> Path:
    """List the test cases Schemathesis generates for the suite. Needs no deployment.

    The cases go to a local stub that answers `{}`, so no check is switched on:
    every response check would fail and say nothing.
    """
    files = run_files(suite, "plan")
    _discard_previous_run(files)
    values = ContractValues(
        values={
            **{
                key: _PLACEHOLDER_BY_FORMAT.get(suite.key_formats.get(key, ""), _PLACEHOLDER)
                for key in suite.fixture_keys
            },
            **suite.constants,
        }
    )
    runs, config, env = _prepare(suite, values, files, selected)
    # The stub needs no rate limit.
    for limited in (config, *config["operations"]):
        limited.pop("rate-limit", None)
    config["operations"] = [block for block in config["operations"] if len(block) > 1]
    config["checks"] = {"enabled": False}
    write_config(config, files.config)
    with stub_server() as url:
        _run_schemathesis(
            suite,
            files,
            api_url=f"{url}{suite.api_prefix}",
            env={**env, BASE_URL_ENV: url, STATIC_AUTHORIZATION_ENV: "Bearer plan"},
        )
    return write_plan(
        runs, files.events, _meta(suite, "local stub", config), fixtures or [], files.directory
    )
