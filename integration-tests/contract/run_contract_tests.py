#!/usr/bin/env python3
"""Contract tests: check that the PipesHub API behaves as its OpenAPI spec says.

    python contract/run_contract_tests.py plan       # list the generated tests; needs no PipesHub
    python contract/run_contract_tests.py fixtures   # create fixture data on the deployment
    python contract/run_contract_tests.py run        # run the tests and write the report card
    python contract/run_contract_tests.py report     # write the report card again from the last run
    python contract/run_contract_tests.py cleanup    # delete the fixture data

See README.md in this directory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import schemathesis
from config_builder import OperationRun, build_config, plan_run, write_config
from env import (
    INTEGRATION_TESTS_DIR,
    STATIC_TOKEN_ENV,
    api_url,
    base_url,
    load_env,
    log_in,
)
from fixtures import (
    Fixtures,
    create_fixtures,
    delete_created_by_tests,
    delete_fixtures,
    placeholder_fixtures,
)
from report import GRADE_ORDER, collect, render_plan, write_report
from stub_server import stub_server
from suite import SPEC_PATH, Suite, SuiteError, load_suite

DEFAULT_SUITE = "enterprise-search"
# 0: all checks passed. 1: some failed. Anything else: Schemathesis itself could not run.
_SCHEMATHESIS_OK = (0, 1)


def _out_dir(suite: Suite) -> Path:
    path = INTEGRATION_TESTS_DIR / "reports" / "contract" / suite.name
    path.mkdir(parents=True, exist_ok=True)
    return path


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
        "commit": _spec_commit(),
    }


def _schemathesis_binary() -> str:
    candidate = Path(sys.executable).parent / "schemathesis"
    if not candidate.exists():
        raise RuntimeError(
            "schemathesis is not installed in this environment. "
            "Run: uv pip install -r contract/requirements.txt"
        )
    return str(candidate)


def _run_schemathesis(
    suite: Suite,
    *,
    config_path: Path,
    url: str,
    ndjson_path: Path,
    har_path: Path | None,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> int:
    command = [
        _schemathesis_binary(),
        "--config-file",
        str(config_path),
        "run",
        str(SPEC_PATH),
        "--url",
        url,
        "--include-path-regex",
        suite.include_path_regex,
        "--continue-on-failure",
        "--report",
        "ndjson,har" if har_path else "ndjson",
        "--report-ndjson-path",
        str(ndjson_path),
        "--no-color",
    ]
    if har_path:
        command += ["--report-har-path", str(har_path)]
    with open(log_path, "w", encoding="utf-8") as log:
        code = subprocess.run(
            command,
            cwd=INTEGRATION_TESTS_DIR,
            env={**os.environ, **(env or {})},
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        ).returncode
    if code not in _SCHEMATHESIS_OK:
        tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-30:])
        raise RuntimeError(f"Schemathesis exited with code {code}. See {log_path}\n{tail}")
    return code


def _print_states(runs: list[OperationRun]) -> None:
    for run in runs:
        if not run.is_sent:
            print(f"  not sent  {run.method} {run.path}: {run.reason}")


def cmd_plan(suite: Suite, args: argparse.Namespace) -> int:
    out = _out_dir(suite)
    fixtures = placeholder_fixtures(suite.fixture_keys)
    runs = plan_run(suite, fixtures, read_only=args.read_only, only=set(args.only))
    config = build_config(suite, fixtures, runs, fuzz=False)
    # The stub answers `{}` to everything, so a response check would fail and tell nothing.
    config.pop("rate-limit", None)
    config["checks"] = {"enabled": False, "not_a_server_error": {"enabled": True}}
    config_path = out / "plan.schemathesis.toml"
    write_config(config, config_path)

    ndjson_path = out / "plan.ndjson"
    with stub_server() as url:
        _run_schemathesis(
            suite,
            config_path=config_path,
            url=f"{url}/api/v1",
            ndjson_path=ndjson_path,
            har_path=None,
            log_path=out / "plan.log",
            env={STATIC_TOKEN_ENV: "plan"},
        )
    plan_path = out / "plan.md"
    plan_path.write_text(
        render_plan(runs, ndjson_path, _meta(suite, "stub server", config)), encoding="utf-8"
    )
    print(f"Plan: {plan_path}")
    print(f"All generated cases: {ndjson_path}")
    return 0


def _check_deployment() -> None:
    url = f"{api_url()}/health"
    try:
        resp = requests.get(url, timeout=15)
    except requests.RequestException as exc:
        raise RuntimeError(f"Cannot reach PipesHub at {url}: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"{url} returned HTTP {resp.status_code}")
    log_in()


def _load_or_create_fixtures(path: Path, *, fresh: bool) -> Fixtures:
    if path.exists() and not fresh:
        print(f"Using fixture data from {path}")
        return Fixtures.load(path)
    print("Creating fixture data...")
    fixtures = create_fixtures()
    fixtures.save(path)
    for group, error in fixtures.errors.items():
        print(f"  fixture group {group} failed: {error}")
    print(f"  {len(fixtures.values)} fixture values saved to {path}")
    return fixtures


def _confirm_target(args: argparse.Namespace) -> None:
    """Never write to a deployment unless the person who runs the command said so."""
    message = f"This command creates, changes and deletes data on {base_url()}."
    if args.yes:
        print(message)
        return
    if not sys.stdin.isatty():
        raise RuntimeError(f"{message} Pass --yes to confirm.")
    if input(f"{message} Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        raise RuntimeError("Stopped. Nothing was sent.")


def cmd_fixtures(suite: Suite, args: argparse.Namespace) -> int:
    load_env()
    _confirm_target(args)
    _check_deployment()
    fixtures = _load_or_create_fixtures(_out_dir(suite) / "fixtures.json", fresh=True)
    return 1 if fixtures.errors else 0


def cmd_run(suite: Suite, args: argparse.Namespace) -> int:
    load_env()
    _confirm_target(args)
    _check_deployment()
    out = _out_dir(suite)
    fixtures = _load_or_create_fixtures(out / "fixtures.json", fresh=args.fresh_fixtures)

    runs = plan_run(suite, fixtures.values, read_only=args.read_only, only=set(args.only))
    _print_states(runs)
    config = build_config(
        suite, fixtures.values, runs, fuzz=args.fuzz, llm_examples=args.llm_examples
    )
    config_path = out / "schemathesis.toml"
    write_config(config, config_path)
    meta = _meta(suite, api_url(), config)
    (out / "run.json").write_text(
        json.dumps({"meta": meta, "runs": [asdict(run) for run in runs]}, indent=2),
        encoding="utf-8",
    )

    sent = sum(run.is_sent for run in runs)
    print(f"Running {sent} of {len(runs)} operations against {base_url()} ...")
    _run_schemathesis(
        suite,
        config_path=config_path,
        url=api_url(),
        ndjson_path=out / "events.ndjson",
        har_path=out / "requests.har",
        log_path=out / "schemathesis.log",
    )
    return cmd_report(suite, args)


def cmd_report(suite: Suite, args: argparse.Namespace) -> int:
    out = _out_dir(suite)
    run_path = out / "run.json"
    if not run_path.exists():
        raise RuntimeError(f"No run found in {out}. Run the `run` command first.")
    saved = json.loads(run_path.read_text(encoding="utf-8"))
    runs = [OperationRun(**entry) for entry in saved["runs"]]
    results = collect(runs, out / "events.ndjson")
    write_report(results, saved["meta"], out)

    grades = {grade: sum(result.grade == grade for result in results) for grade in GRADE_ORDER}
    print("Grades: " + ", ".join(f"{grade} {count}" for grade, count in grades.items() if count))
    print(f"Report: {out / 'report.md'}")
    return 0


def cmd_cleanup(suite: Suite, args: argparse.Namespace) -> int:
    load_env()
    out = _out_dir(suite)
    fixtures_path = out / "fixtures.json"
    events_path = out / "events.ndjson"
    if not fixtures_path.exists() and not events_path.exists():
        print("Nothing to delete.")
        return 0
    _confirm_target(args)
    fixtures = Fixtures.load(fixtures_path) if fixtures_path.exists() else Fixtures()

    failures: list[str] = []
    # Before the fixtures: a conversation that a test created sits under a fixture agent.
    if events_path.exists():
        found, failures = delete_created_by_tests(suite, fixtures, events_path)
        print(f"Resources created by test cases: {found}")
    failures += delete_fixtures(fixtures)
    print(f"Fixtures: {len(fixtures.created)}")
    for failure in failures:
        print(f"  not deleted: {failure}")
    if failures:
        return 1
    fixtures_path.unlink(missing_ok=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", default=DEFAULT_SUITE, help=f"Suite name (default: {DEFAULT_SUITE})")
    commands = parser.add_subparsers(dest="command", required=True)

    def _add(name: str, handler: Any, *, selection: bool = False) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        sub.set_defaults(handler=handler)
        if selection:
            sub.add_argument("--read-only", action="store_true", help="Send GET operations only")
            sub.add_argument(
                "--only",
                action="append",
                default=[],
                metavar="OPERATION_ID",
                help="Send only this operation (repeat for more)",
            )
        return sub

    def _add_writer(name: str, handler: Any, *, selection: bool = False) -> argparse.ArgumentParser:
        sub = _add(name, handler, selection=selection)
        sub.add_argument(
            "--yes",
            action="store_true",
            help="Confirm that the deployment in PIPESHUB_BASE_URL may be changed",
        )
        return sub

    _add("plan", cmd_plan, selection=True)
    _add_writer("fixtures", cmd_fixtures)
    run = _add_writer("run", cmd_run, selection=True)
    run.add_argument("--fresh-fixtures", action="store_true", help="Create new fixture data first")
    run.add_argument("--fuzz", action="store_true", help="Also run the random fuzzing phase")
    run.add_argument(
        "--llm-examples",
        action="store_true",
        help="For negative-only operations, also send the spec's examples. Each one calls the LLM.",
    )
    _add("report", cmd_report)
    _add_writer("cleanup", cmd_cleanup)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        suite = load_suite(args.suite)
        return args.handler(suite, args)
    except (SuiteError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
